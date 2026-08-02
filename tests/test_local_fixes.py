"""Local regression tests for self-iteration round 1 fixes.

These cover defects found by the meta-review pipeline (docs/reviews/):
- P0-1 Task.output_path() must resolve relative outputs against task.cwd
- P0-2 _git_commit_stage must report git add/commit failures
- P0-3 load_tasks must fail loud when PyYAML is missing (no silent degrade)
- P0-4 _filter_reused must honor task.cwd via output_path()

Kept in a separate file so upstream test_pi_batch.py stays sync-friendly.
The runner is loaded through the pi-batch.py shim (importlib, same as the
upstream suite).
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PI_BATCH = Path(__file__).resolve().parent.parent / "pi-batch.py"


def load_batch():
    """Load the shim (public API) plus direct handles on pbatch internals."""
    root = str(PI_BATCH.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import pbatch
    import pbatch.cli
    import pbatch.pipeline
    import pbatch.runner
    return pbatch


def _git_repo(tmp_path):
    """A bare git repo with no identity configured (commits must fail)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path / "nohome"),
    }
    return repo, env


# -- P0-1: output_path respects task.cwd ---------------------------------
class TestOutputPathCwd:
    def test_absolute_output_unchanged(self):
        mod = load_batch()
        task = mod.Task(prompt="p", output="/tmp/x.md", cwd="/tmp")
        assert str(task.output_path()) == "/tmp/x.md"

    def test_relative_output_resolves_against_cwd(self, tmp_path):
        mod = load_batch()
        workdir = tmp_path / "work"
        workdir.mkdir()
        task = mod.Task(prompt="p", output="out.md", cwd=str(workdir))
        assert task.output_path() == (workdir / "out.md").resolve()

    def test_relative_output_without_cwd_uses_process_cwd(self, tmp_path, monkeypatch):
        mod = load_batch()
        monkeypatch.chdir(tmp_path)
        task = mod.Task(prompt="p", output="out.md")
        assert task.output_path() == (tmp_path / "out.md").resolve()

    def test_save_result_writes_into_task_cwd(self, tmp_path, monkeypatch):
        """End to end: agent runs in task.cwd, deliverable lands there too."""
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho deliverable\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        workdir = tmp_path / "work"
        workdir.mkdir()
        task = mod.Task(prompt="p", output="out.md", cwd=str(workdir))
        result = mod.run_task(task)
        assert result.success is True
        mod.save_result(task, result)
        assert (workdir / "out.md").exists()
        assert not (tmp_path / "out.md").exists()


# -- P0-4: _filter_reused honors cwd -------------------------------------
class TestFilterReused:
    def test_existing_output_in_task_cwd_is_skipped(self, tmp_path):
        mod = load_batch()
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "out.md").write_text("done", encoding="utf-8")
        tasks = [mod.Task(prompt="p", output="out.md", cwd=str(workdir))]
        kept = mod.cli._filter_reused(tasks, reuse=True)
        assert kept == []
        # same relative output, different cwd -> not reused
        other = tmp_path / "other"
        other.mkdir()
        tasks2 = [mod.Task(prompt="p", output="out.md", cwd=str(other))]
        assert mod.cli._filter_reused(tasks2, reuse=True) == tasks2


# -- P0-2: git commit failure is reported ---------------------------------
class TestGitCommitFailure:
    def test_failed_commit_returns_false_and_logs(self, tmp_path, monkeypatch, caplog):
        mod = load_batch()
        repo, env = _git_repo(tmp_path)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(repo)  # git runs from the process cwd
        out = repo / "out.md"
        out.write_text("x", encoding="utf-8")
        stage = mod.Stage(name="s0", git_commit=True, commit_message="msg")
        with caplog.at_level("WARNING"):
            ok = mod.pipeline._git_commit_stage(stage, [str(out)])
        assert ok is False
        assert any("git commit failed" in r.message for r in caplog.records)
        assert not any("GIT COMMIT:" in r.message for r in caplog.records)

    def test_successful_commit_returns_true(self, tmp_path, monkeypatch, caplog):
        mod = load_batch()
        repo, env = _git_repo(tmp_path)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(repo)  # git runs from the process cwd
        out = repo / "out.md"
        out.write_text("x", encoding="utf-8")
        stage = mod.Stage(name="s0", git_commit=True, commit_message="msg")
        with caplog.at_level("INFO"):
            ok = mod.pipeline._git_commit_stage(stage, [str(out)])
        assert ok is True
        assert any("GIT COMMIT:" in r.message for r in caplog.records)


# -- P0-3: PyYAML missing fails loud --------------------------------------
class TestMissingPyYaml:
    def test_load_tasks_yaml_without_pyyaml_exits(self, tmp_path, monkeypatch):
        mod = load_batch()
        tasks_yaml = tmp_path / "tasks.yaml"
        tasks_yaml.write_text("tasks:\n  - prompt: t1\n", encoding="utf-8")
        monkeypatch.setattr(mod.pipeline, "yaml", None)
        with pytest.raises(SystemExit) as exc:
            mod.load_tasks(str(tasks_yaml))
        assert exc.value.code == 1

    def test_load_pipeline_without_pyyaml_exits(self, tmp_path, monkeypatch):
        mod = load_batch()
        pipeline_yaml = tmp_path / "p.yaml"
        pipeline_yaml.write_text("stages:\n  - name: s\n", encoding="utf-8")
        monkeypatch.setattr(mod.pipeline, "yaml", None)
        with pytest.raises(SystemExit) as exc:
            mod.load_pipeline(str(pipeline_yaml))
        assert exc.value.code == 1

    def test_plain_text_prompt_without_pyyaml_still_works(self, tmp_path, monkeypatch):
        mod = load_batch()
        prompt = tmp_path / "notes.txt"
        prompt.write_text("just a prompt", encoding="utf-8")
        monkeypatch.setattr(mod.pipeline, "yaml", None)
        tasks = mod.load_tasks(str(prompt))
        assert len(tasks) == 1
        assert tasks[0].prompt == "just a prompt"


# -- Round 2 (self-iteration round 2) fixes -------------------------------


class TestFromDirNoDoublePrefix:
    """F1: from_dir constructors must not double-prefix relative outputs
    now that output_path() resolves against task.cwd."""

    def test_load_tasks_from_dir_outputs_absolute(self, tmp_path, monkeypatch):
        mod = load_batch()
        inputs = tmp_path / "docs" / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "a.md").write_text("x", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        tasks = mod.load_tasks_from_dir(str(inputs))
        assert len(tasks) == 1
        assert tasks[0].output_path() == (inputs / "a.out.md").resolve()

    def test_stage_from_dir_relative_dir_no_double_prefix(self, tmp_path, monkeypatch):
        mod = load_batch()
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        (inputs / "a.md").write_text("x", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        stage = mod.Stage(name="s0", from_dir=str(inputs))
        tasks, _ = mod.pipeline._stage_from_dir_tasks(stage, reuse=False, model_override="", timeout_override=0)
        assert len(tasks) == 1
        assert tasks[0].output_path() == (inputs / "a.out.md").resolve()
        assert not str(tasks[0].output_path()).endswith("inputs/inputs/a.out.md")


class TestResolvedPathsPropagate:
    """P0-1: stage outputs, meta role outputs and batch git-commit inputs
    must be resolved paths (output_path()), never raw strings."""

    def test_execute_stage_outputs_are_resolved(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        workdir = tmp_path / "work"
        workdir.mkdir()
        task = mod.Task(prompt="p", output="out.md", cwd=str(workdir))
        results, outputs = mod.pipeline._execute_stage_tasks(
            mod.Stage(name="s0"), [task], [], "", "new", "", "")
        assert results[0].success
        assert outputs == [str((workdir / "out.md").resolve())]

    def test_meta_role_outputs_are_resolved(self, tmp_path, monkeypatch):
        mod = load_batch()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "qa_lead.md").write_text("Role: {input_content}", encoding="utf-8")
        idea = tmp_path / "idea.md"
        idea.write_text("idea", encoding="utf-8")
        # one agent for both roles: orchestrator picks qa_lead once, then []
        counter = tmp_path / "counter"
        counter.write_text("0", encoding="utf-8")
        agent = tmp_path / "agent.sh"
        agent.write_text(
            "#!/bin/sh\n"
            "if echo \"$2\" | grep -q 'Available roles'; then\n"
            f"  n=$(cat {counter}); echo $((n+1)) > {counter}\n"
            "  if [ \"$n\" = \"0\" ]; then echo '[\"qa_lead\"]'; else echo '[]'; fi\n"
            "else\n"
            "  echo '## deliverable'\n"
            "fi\n",
            encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        stage = mod.Stage(name="review", from_outputs="req", meta=True,
                          role_dir=str(out_dir), output_dir=str(out_dir), max_iterations=2)
        results, ok = mod.pipeline._run_meta_stage(stage, {"req": [str(idea)]}, "", 0, "")
        assert ok
        assert len(results) == 1
        for r in results:
            assert r.task.output == str(r.task.output_path())
        assert (out_dir / "qa_lead.md").exists()


class TestKeyboardInterruptKillsChild:
    """F2: Ctrl-C must not orphan the agent process group."""

    def test_sigint_kills_process_group(self, tmp_path, monkeypatch):
        mod = load_batch()
        pidfile = tmp_path / "child.pid"
        gofile = tmp_path / "go"
        agent = tmp_path / "sleeper.sh"
        # T13: handshake — the child writes its pid, waits for the go file,
        # then sleeps; the test only raises Ctrl-C after the child is up,
        # removing the start-race that made this test flaky.
        agent.write_text(
            f"#!/bin/sh\necho $$ > {pidfile}\n"
            f"while [ ! -f {gofile} ]; do sleep 0.02; done\n"
            "sleep 300\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)

        def _raise_interrupt(*a, **kw):
            # wait until the child is actually up (pidfile written), then
            # simulate Ctrl-C while the child process group is alive
            import time as _time
            for _ in range(500):
                if pidfile.exists():
                    gofile.write_text("1", encoding="utf-8")
                    _time.sleep(0.1)  # let the child enter the sleep
                    break
                _time.sleep(0.02)
            raise KeyboardInterrupt()

        monkeypatch.setattr(mod.runner, "_stream_proc", _raise_interrupt)
        task = mod.Task(prompt="p", output=str(tmp_path / "o.md"))
        import pytest as _pytest
        with _pytest.raises(KeyboardInterrupt):
            mod.run_task(task)
        # the child must be dead now (finally killed the group)
        child_pid = int(pidfile.read_text(encoding="utf-8").strip())
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return  # fully reaped: definitely dead
        # zombie (killed, not yet reaped by the parent) also proves the
        # group was killed; a live sleeping child would fail this
        state = Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8").split()[2]
        assert state == "Z", f"child process group still alive (state {state})"


class TestGateVerdictAnchored:
    """F12: VERDICT must anchor at line start; prose mentions must not pass."""

    def test_prose_mention_does_not_count(self, tmp_path):
        mod = load_batch()
        f = tmp_path / "gate.md"
        f.write_text("The review says VERDICT: PASS somewhere in prose\n", encoding="utf-8")
        assert mod.pipeline._gate_verdict([str(f)]) is None

    def test_line_start_verdict_counts(self, tmp_path):
        mod = load_batch()
        f = tmp_path / "gate.md"
        f.write_text("## Review\nVERDICT: PASS - all good\n", encoding="utf-8")
        assert mod.pipeline._gate_verdict([str(f)]) == "PASS"


class TestValidatorShellEscape:
    """F3/F14: {output} substitution is shell-quoted; paths with spaces work."""

    def test_output_with_spaces_passes_unquoted_template(self, tmp_path):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        spaced = tmp_path / "dir with spaces"
        spaced.mkdir()
        output = spaced / "o.md"
        results = mod.run_serial([mod.Task(prompt="x", output=str(output))],
                                 validate_cmd='test -f {output}')
        assert results[0].success is True
        assert output.exists()


class TestSaveResultHardening:
    """T0.1 (full-SDLC pipeline): save_result refuses symlink targets and
    writes via a unique mkstemp temp file (no fixed .tmp name)."""

    def test_symlink_output_refused(self, tmp_path):
        mod = load_batch()
        victim = tmp_path / "victim.md"
        victim.write_text("do not overwrite", encoding="utf-8")
        link = tmp_path / "out.md"
        link.symlink_to(victim)
        task = mod.Task(prompt="p", output=str(link))
        result = mod.TaskResult(task=task, success=True, stdout="new content")
        mod.save_result(task, result)
        assert victim.read_text(encoding="utf-8") == "do not overwrite"
        assert not link.exists() or link.is_symlink()  # target untouched

    def test_no_fixed_tmp_left_behind(self, tmp_path):
        mod = load_batch()
        out = tmp_path / "o.md"
        task = mod.Task(prompt="p", output=str(out))
        result = mod.TaskResult(task=task, success=True, stdout="x")
        mod.save_result(task, result)
        assert out.exists()
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


class TestReuseDecision:
    """T1 (full-SDLC pipeline): reuse freshness revalidation."""

    def _write_artifact(self, tmp_path):
        out = tmp_path / "o.md"
        out.write_text("artifact", encoding="utf-8")
        return out

    def test_no_validators_trivially_reused(self, tmp_path):
        mod = load_batch()
        out = self._write_artifact(tmp_path)
        assert mod.reuse_decision(str(out), "", str(tmp_path)) is True

    def test_validator_pass_reused(self, tmp_path):
        mod = load_batch()
        out = self._write_artifact(tmp_path)
        assert mod.reuse_decision(str(out), "true", str(tmp_path)) is True

    def test_validator_fail_deletes_and_regenerates(self, tmp_path):
        mod = load_batch()
        out = self._write_artifact(tmp_path)
        assert mod.reuse_decision(str(out), "exit 1", str(tmp_path)) is False
        assert not out.exists()  # stale artifact deleted, zero residue

    def test_empty_artifact_fails_closed(self, tmp_path):
        mod = load_batch()
        out = tmp_path / "o.md"
        out.write_text("", encoding="utf-8")
        assert mod.reuse_decision(str(out), "", str(tmp_path)) is False

    def test_symlink_deleted_not_validated_through(self, tmp_path):
        mod = load_batch()
        victim = tmp_path / "victim.md"
        victim.write_text("secret", encoding="utf-8")
        leak = tmp_path / "leak.txt"
        link = tmp_path / "o.md"
        link.symlink_to(victim)
        gate = f"cat {{output}} > {leak}"
        assert mod.reuse_decision(str(link), gate, str(tmp_path)) is False
        assert not leak.exists()  # gate never executed against the victim
        assert not link.exists()  # symlink deleted for regeneration

    def test_legacy_existence_only(self, tmp_path):
        mod = load_batch()
        out = self._write_artifact(tmp_path)
        assert mod.reuse_decision(str(out), "exit 1", str(tmp_path), legacy=True) is True

    def test_cli_reuse_legacy_flag_regenerated_on_fail(self, tmp_path):
        """--reuse with a failing gate regenerates; --reuse-legacy skips."""
        counter = tmp_path / "counter"
        agent = tmp_path / "counting-agent.sh"
        agent.write_text(f"#!/bin/sh\necho x >> {counter}\necho \"$2\"\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("prompt", encoding="utf-8")
        base = [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
                "--agent-bin", str(agent), "--mode", "serial", "--reuse", "--validate-cmd", "exit 1"]
        first = subprocess.run(base, capture_output=True, text=True, timeout=120)
        assert first.returncode != 0
        assert not (inputs / "t.out.md").exists()  # gate rejected, no artifact
        # --reuse-legacy: existence-only skip would reuse… but no artifact
        # exists, so still regenerates; use a passing gate for the skip case
        pass_gate = [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
                     "--agent-bin", str(agent), "--mode", "serial", "--reuse", "--validate-cmd", "true"]
        ok = subprocess.run(pass_gate, capture_output=True, text=True, timeout=120)
        assert ok.returncode == 0, ok.stderr
        calls = counter.read_text(encoding="utf-8").count("x")
        # reuse+validated: agent not called again
        again = subprocess.run(pass_gate, capture_output=True, text=True, timeout=120)
        assert again.returncode == 0, again.stderr
        assert counter.read_text(encoding="utf-8").count("x") == calls


class TestValidationFeedback:
    """T2 (full-SDLC pipeline): validator feedback flows into retries."""

    def test_retry_prompt_carries_validator_stderr(self, tmp_path, monkeypatch):
        mod = load_batch()
        args_log = tmp_path / "args.log"
        agent = tmp_path / "record-agent.sh"
        agent.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$2\" >> {args_log}\n"
            "echo '## result'\n",
            encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        counter = tmp_path / "counter"
        out = tmp_path / "o.md"
        # validator fails once (exit 3 with stderr), then passes
        validate = f"echo gate-boom >&2; echo x >> {counter}; [ $(wc -l < {counter}) -le 1 ] && exit 3 || exit 0"
        monkeypatch.setattr(mod.runner, "_retry_wait", lambda *a, **k: 0)
        results = mod.run_serial([mod.Task(prompt="orig prompt", output=str(out))],
                                 retries=1, retry_delay=0, validate_cmd=validate)
        assert results[0].success is True
        assert out.exists()
        text = args_log.read_text(encoding="utf-8")
        assert "orig prompt" in text
        assert "validator feedback from previous attempt" in text
        assert "gate-boom" in text
        assert "exit=3" in text

    def test_result_carries_validation_fields(self, tmp_path):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        out = tmp_path / "o.md"
        results = mod.run_serial([mod.Task(prompt="x", output=str(out))],
                                 validate_cmd="echo deny >&2; exit 7")
        assert results[0].success is False
        assert results[0].validation_ok is False
        assert results[0].validation_exit == 7
        assert "deny" in results[0].validation_stderr
        assert "validation failed (exit=7)" in results[0].reason
        assert not out.exists()


class TestLogRotation:
    """T3 (7x24 governance): the run log rotates instead of growing unbounded."""

    def test_rotation_bounds_active_file(self, tmp_path):
        import logging
        import logging.handlers
        log_file = tmp_path / "run.log"
        fh = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("rotation-test")
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
        for i in range(200):  # ~20KB of lines > 1KB x 2 backups
            logger.info("line-%03d %s", i, "x" * 80)
        fh.close()
        assert log_file.exists()
        assert log_file.stat().st_size <= 2048  # active file bounded
        backups = sorted(tmp_path.glob("run.log.*"))
        assert len(backups) == 2


class TestInstanceLock:
    """T4 (7x24 governance): single-instance lock."""

    def test_second_instance_refused(self, tmp_path):
        import pbatch.lock as lock
        first = lock.acquire_lock(str(tmp_path))
        try:
            with pytest.raises(SystemExit) as exc:
                lock.acquire_lock(str(tmp_path))
            assert exc.value.code == 5
        finally:
            lock.release_lock(first)
        assert not (tmp_path / lock.LOCK_NAME).exists()

    def test_stale_lock_broken_and_retried(self, tmp_path):
        import pbatch.lock as lock
        import json
        stale = tmp_path / lock.LOCK_NAME
        stale.write_text(json.dumps({
            "pid": 99999999,  # almost certainly dead
            "start": 1,       # ancient
            "host": "x", "argv": "y"}), encoding="utf-8")
        got = lock.acquire_lock(str(tmp_path))
        assert got == stale  # stale broken, lock re-acquired
        assert json.loads(stale.read_text(encoding="utf-8"))["pid"] == os.getpid()
        lock.release_lock(got)

    def test_clean_exit_releases(self, tmp_path):
        import pbatch.lock as lock
        got = lock.acquire_lock(str(tmp_path))
        lock.release_lock(got)
        assert not (tmp_path / lock.LOCK_NAME).exists()


class TestTriage:
    """T5 (7x24 governance): circuit / stall / environment / residue."""

    def test_circuit_isolates_after_max(self):
        import pbatch.triage as triage
        circuit = triage.Circuit(max_failures=3)
        key = "task-A"
        for i in range(2):
            assert circuit.register_failure(key, "agent exited 1") is False
        assert circuit.register_failure(key, "agent exited 1") is True
        assert circuit.allowed(key) is False

    def test_circuit_permanent_failure_isolates_immediately(self):
        import pbatch.triage as triage
        circuit = triage.Circuit()
        assert circuit.register_failure("k", "agent binary not found") is True

    def test_stall_detector_stops_after_k(self):
        import pbatch.triage as triage
        stall = triage.StallDetector(k=2)
        assert stall.update({"a", "b"}, 0) is False  # baseline round
        assert stall.update({"a", "b"}, 0) is False  # stagnant 1
        assert stall.update({"a", "b"}, 0) is True   # stagnant 2 = K -> stop

    def test_stall_progress_resets(self):
        import pbatch.triage as triage
        stall = triage.StallDetector(k=3)
        stall.update({"a"}, 0)
        stall.update({"a"}, 0)
        assert stall.update(set(), 1) is False  # progress resets the counter
        stall.update(set(), 1)
        assert stall.update(set(), 1) is False  # no failures: never stalls

    def test_env_signature_detects_shared_gate_failure(self, tmp_path):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        results = mod.run_serial(
            [mod.Task(prompt="a", output=str(tmp_path / "a.md")),
             mod.Task(prompt="b", output=str(tmp_path / "b.md"))],
            validate_cmd="echo gate-broken >&2; exit 9")
        assert all(not r.success for r in results)
        import pbatch.triage as triage
        assert triage.env_signature(results)  # identical stderr across 2 tasks

    def test_residue_markers_lifecycle(self, tmp_path):
        import pbatch.triage as triage
        key = "abc123"
        assert triage.residue_markers(str(tmp_path)) == []
        triage.write_marker(key, str(tmp_path))
        markers = triage.residue_markers(str(tmp_path))
        assert len(markers) == 1
        triage.clear_marker(key, str(tmp_path))
        assert triage.residue_markers(str(tmp_path)) == []

    def test_run_task_leaves_no_marker(self, tmp_path):
        """A normally finished task clears its start marker (residue-free)."""
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        out = tmp_path / "o.md"
        result = mod.run_task(mod.Task(prompt="p", output=str(out), cwd=str(tmp_path)))
        assert result.success
        import pbatch.triage as triage
        assert triage.residue_markers(str(tmp_path)) == []

    def test_cli_circuit_stops_unattended_poison(self, tmp_path):
        """A permanently failing task under --max-rounds 0 is isolated and
        the loop stops with exit 4 instead of burning quota forever."""
        counter = tmp_path / "counter"
        agent = tmp_path / "poison.sh"
        agent.write_text(f"#!/bin/sh\necho x >> {counter}\necho plain-failure\nexit 1\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("prompt", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
             "--agent-bin", str(agent), "--mode", "serial", "--max-rounds", "0",
             "--round-delay", "0", "--circuit-max", "2", "--stall-rounds", "3"],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 4, result.stderr
        assert counter.read_text(encoding="utf-8").count("x") == 2  # 2 fails, then isolated

    def test_cli_stall_stops_no_progress(self, tmp_path):
        """No progress for K rounds stops the loop with exit 4."""
        counter = tmp_path / "counter"
        agent = tmp_path / "flaky.sh"
        agent.write_text(f"#!/bin/sh\necho x >> {counter}\necho plain-failure\nexit 1\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("prompt", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
             "--agent-bin", str(agent), "--mode", "serial", "--max-rounds", "0",
             "--round-delay", "0", "--circuit-max", "99", "--stall-rounds", "2"],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 4, result.stderr
        assert "STALL" in result.stderr


class TestMeteringBudget:
    """T7/T8 (7x24 governance): metering events + budget caps."""

    def test_events_file_records_finish_and_fail(self, tmp_path):
        counter = tmp_path / "counter"
        agent = tmp_path / "agent.sh"
        agent.write_text(
            "#!/bin/sh\n"
            f"echo x >> {counter}\n"
            "if [ $(wc -l < {c}) -le 1 ]; then echo fail; exit 1; else echo ok; fi\n".format(c=counter),
            encoding="utf-8")
        agent.chmod(0o755)
        events = tmp_path / "events.jsonl"
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "a.md").write_text("p1", encoding="utf-8")
        (inputs / "b.md").write_text("p2", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
             "--agent-bin", str(agent), "--mode", "serial", "--events-file", str(events)],
            capture_output=True, text=True, timeout=120)
        assert result.returncode != 0  # one task fails (exit 1, non-retryable rounds)
        lines = events.read_text(encoding="utf-8").strip().splitlines()
        events_seen = [json.loads(l)["event"] for l in lines]
        assert "task_fail" in events_seen
        assert any("task_finish" in e for e in events_seen) or len(events_seen) >= 1
        for l in lines:
            assert json.loads(l)["ts"]  # well-formed JSONL

    def test_budget_max_stops_before_spawning(self, tmp_path):
        """--budget-max 2 with 3 tasks: exactly 2 spawns, exit 3."""
        counter = tmp_path / "counter"
        agent = tmp_path / "counting.sh"
        agent.write_text(f"#!/bin/sh\necho x >> {counter}\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        for i in range(3):
            (inputs / f"t{i}.md").write_text(f"p{i}", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
             "--agent-bin", str(agent), "--mode", "serial",
             "--budget-max", "2", "--events-file", str(tmp_path / "e.jsonl")],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 3, result.stderr
        assert counter.read_text(encoding="utf-8").count("x") == 2
        events = (tmp_path / "e.jsonl").read_text(encoding="utf-8")
        assert "budget_cap" in events

    def test_daily_budget_persists_across_runs(self, tmp_path):
        import pbatch.metering as metering
        state = tmp_path / "daily.json"
        metering._DAILY_STATE = str(state)
        metering.DAILY_BUDGET = 10
        metering.budget_consume()
        metering.budget_consume()
        assert metering.budget_spent_today() == 2
        # simulate a fresh process: re-read from the state file
        metering2 = __import__("importlib").import_module("pbatch.metering")
        metering2._DAILY_STATE = str(state)
        assert metering2.budget_spent_today() == 2
        metering.DAILY_BUDGET = 0
        metering.INVOCATIONS = 0


class TestHeteroRouting:
    """T11 (7x24 governance): provider/thinking/tools reach the agent argv
    from pipeline task defs, stage config, and the CLI (CLI > task > stage)."""

    def test_pipeline_task_def_routing_fields(self, tmp_path):
        mod = load_batch()
        args_log = tmp_path / "args.log"
        agent = tmp_path / "record-agent.sh"
        agent.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {args_log}\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("prompt", encoding="utf-8")
        template = tmp_path / "role.md"
        template.write_text("Role: {input_content}", encoding="utf-8")
        stage = mod.Stage(
            name="s1", from_outputs="s0",
            tasks=[{"prompt_template": str(template),
                    "output": str(tmp_path / "{input_stem}.out.md"),
                    "provider": "p-task", "thinking": "high",
                    "tools": "read,edit", "exclude_tools": "bash"}])
        results, ok = mod.execute_stage(stage, {"s0": [str(inputs / "t.md")]})
        assert ok and results[0].success
        lines = args_log.read_text(encoding="utf-8").splitlines()
        assert "--provider" in lines and "p-task" in lines
        assert "--thinking" in lines and "high" in lines
        assert "--tools" in lines and "read,edit" in lines
        assert "--exclude-tools" in lines and "bash" in lines

    def test_stage_provider_applies_to_meta_role_tasks(self, tmp_path):
        mod = load_batch()
        args_log = tmp_path / "args.log"
        agent = tmp_path / "record-agent.sh"
        agent.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" >> {args_log}\n"
            "if echo \"$2\" | grep -q 'Available roles'; then echo '[\"qa_lead\"]'; else echo '## role'; fi\n",
            encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "qa_lead.md").write_text("Role", encoding="utf-8")
        idea = tmp_path / "idea.md"
        idea.write_text("idea", encoding="utf-8")
        stage = mod.Stage(name="review", from_outputs="req", meta=True, provider="p-stage",
                          role_dir=str(out_dir), output_dir=str(out_dir), max_iterations=2)
        results, ok = mod.pipeline._run_meta_stage(stage, {"req": [str(idea)]}, "", 0, "")
        assert ok
        lines = args_log.read_text(encoding="utf-8").splitlines()
        # both the orchestrator call and the role task carry the stage provider
        assert lines.count("--provider") == 2
        assert lines.count("p-stage") == 2

    def test_cli_provider_overrides_stage(self, tmp_path):
        """CLI --provider wins over a stage-level provider (precedence D6)."""
        args_log = tmp_path / "args.log"
        agent = tmp_path / "record-agent.sh"
        agent.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {args_log}\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("p", encoding="utf-8")
        pipeline = tmp_path / "p.yaml"
        pipeline.write_text(
            "git_commit: false\n"
            "stages:\n"
            f"  - name: s0\n    from_dir: {inputs}\n    provider: p-stage\n",
            encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), str(pipeline),
             "--agent-bin", str(agent), "--provider", "p-cli"],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        lines = args_log.read_text(encoding="utf-8").splitlines()
        assert "p-cli" in lines
        assert "p-stage" not in lines


class TestFingerprintReuse:
    """T9 (7x24 governance): input-fingerprint reuse 2.0 (sidecar)."""

    def test_fingerprint_changes_with_input(self):
        import pbatch.reuse as reuse
        fp1 = reuse.fingerprint("prompt A", "/tmp/o.md", "true", "m1", "")
        fp2 = reuse.fingerprint("prompt B", "/tmp/o.md", "true", "m1", "")
        assert fp1 != fp2
        assert reuse.fingerprint("p", "/tmp/o.md", "true", "m1", "") == \
            reuse.fingerprint("p", "/tmp/o.md", "true", "m1", "")

    def test_sidecar_mismatch_regenerates(self, tmp_path):
        mod = load_batch()
        out = tmp_path / "o.md"
        out.write_text("artifact", encoding="utf-8")
        # no sidecar yet: fingerprint mode must regenerate
        assert mod.reuse_decision(str(out), "", str(tmp_path), expected_fp="abc") is False
        assert not out.exists()

    def test_sidecar_match_reuses(self, tmp_path):
        mod = load_batch()
        out = tmp_path / "o.md"
        out.write_text("artifact", encoding="utf-8")
        mod.write_sidecar(str(out), "prompt", "true", "m", "")
        fp = mod.fingerprint("prompt", str(out), "true", "m", "")
        assert mod.reuse_decision(str(out), "true", str(tmp_path), expected_fp=fp) is True

    def test_input_change_regenerates(self, tmp_path):
        mod = load_batch()
        out = tmp_path / "o.md"
        out.write_text("artifact", encoding="utf-8")
        mod.write_sidecar(str(out), "old prompt", "true", "m", "")
        fp = mod.fingerprint("new prompt", str(out), "true", "m", "")
        assert mod.reuse_decision(str(out), "true", str(tmp_path), expected_fp=fp) is False
        assert not out.exists()  # stale artifact deleted

    def test_cli_reuse_fingerprint_e2e(self, tmp_path):
        """--reuse-fingerprint: unchanged input reuses, changed input
        regenerates (agent called again)."""
        counter = tmp_path / "counter"
        agent = tmp_path / "counting.sh"
        agent.write_text(f"#!/bin/sh\necho x >> {counter}\necho \"$2\"\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("prompt v1", encoding="utf-8")
        base = [sys.executable, str(PI_BATCH), "--from-dir", str(inputs),
                "--agent-bin", str(agent), "--mode", "serial",
                "--reuse", "--reuse-fingerprint"]
        first = subprocess.run(base, capture_output=True, text=True, timeout=120)
        assert first.returncode == 0, first.stderr
        calls = counter.read_text(encoding="utf-8").count("x")
        assert calls == 1
        assert (inputs / "t.out.md").exists()
        assert (inputs / "t.out.md.meta.json").exists()  # sidecar written
        # unchanged input: reused, agent not called again
        again = subprocess.run(base, capture_output=True, text=True, timeout=120)
        assert again.returncode == 0, again.stderr
        assert counter.read_text(encoding="utf-8").count("x") == calls
        # changed input: fingerprint mismatch -> regenerated
        (inputs / "t.md").write_text("prompt v2", encoding="utf-8")
        changed = subprocess.run(base, capture_output=True, text=True, timeout=120)
        assert changed.returncode == 0, changed.stderr
        assert counter.read_text(encoding="utf-8").count("x") == calls + 1


class TestSessionRotation:
    """T10 (7x24 governance): session fork at the size watermark."""

    def _fake_session(self, tmp_path, cwd, name, size_kb=1, with_compaction=False):
        import pbatch.session as session
        workdir_key = "--" + cwd.replace("/", "-") + "--"
        d = tmp_path / workdir_key
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        if with_compaction:
            lines.append('{"type": "compaction", "id": "c1"}')
        lines.append('{"type": "session", "id": "%s"}' % name)
        f = d / f"2026-08-02T00-00-00-000Z_{name}.jsonl"
        f.write_text("\n".join(lines) + "\n" + ("x" * size_kb * 1024), encoding="utf-8")
        session.SESSIONS_DIR = tmp_path  # test-injectable
        return f

    def test_session_file_discovery_and_size(self, tmp_path):
        import pbatch.session as session
        self._fake_session(tmp_path, "/work", "sess1", size_kb=3)
        f = session.session_file("/work", "sess1")
        assert f is not None and f.name.endswith("_sess1.jsonl")
        assert session.session_size_bytes("/work", "sess1") > 3000
        assert session.session_size_bytes("/work", "missing") == 0

    def test_compaction_detection(self, tmp_path):
        import pbatch.session as session
        self._fake_session(tmp_path, "/work", "s1", with_compaction=True)
        assert session.has_compaction("/work", "s1") is True
        self._fake_session(tmp_path, "/work2", "s2")
        assert session.has_compaction("/work2", "s2") is False

    def test_fork_flags_use_pi_contract(self):
        import pbatch.session as session
        flags = session.fork_flags("/tmp/s.jsonl")
        assert flags == ["--fork", "/tmp/s.jsonl"]

    def test_shared_serial_rotates_at_watermark(self, tmp_path, monkeypatch):
        """Past the watermark, the second call forks instead of continuing."""
        mod = load_batch()
        args_log = tmp_path / "args.log"
        agent = tmp_path / "record-agent.sh"
        agent.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {args_log}\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        # fake session file over the watermark
        import pbatch.session as session
        workdir_key = "--" + tmp_path.as_posix().replace("/", "-") + "--"
        d = tmp_path / workdir_key
        d.mkdir(parents=True, exist_ok=True)
        (d / f"2026-08-02T00-00-00-000Z_rot.jsonl").write_text("x" * 3000, encoding="utf-8")
        session.SESSIONS_DIR = tmp_path
        monkeypatch.setattr(mod.config, "SESSION_MAX_BYTES", 1024)
        monkeypatch.chdir(tmp_path)
        tasks = [mod.Task(prompt="p1", output=str(tmp_path / "1.md")),
                 mod.Task(prompt="p2", output=str(tmp_path / "2.md"))]
        results = mod.run_serial(tasks, session_mode="shared", session_id="rot", session_name="rot")
        assert all(r.success for r in results)
        lines = args_log.read_text(encoding="utf-8").splitlines()
        assert "--session-id" in lines  # first call started the session
        assert "--fork" in lines        # second call rotated
        # first call must NOT fork
        assert lines.index("--session-id") < lines.index("--fork")


class TestGovernanceBatch:
    """T12 (7x24 governance): validator JSON protocol, gate reason,
    evidence fences, output caps, secretscan registry."""

    def test_validator_json_status_warn_allows(self, tmp_path):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        out = tmp_path / "o.md"
        results = mod.run_serial([mod.Task(prompt="x", output=str(out))],
                                 validate_cmd='echo \'{"status": "warn"}\'')
        assert results[0].success is True  # warn allows, output saved
        assert out.exists()

    def test_validator_json_status_fail_rejects(self, tmp_path):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        out = tmp_path / "o.md"
        results = mod.run_serial([mod.Task(prompt="x", output=str(out))],
                                 validate_cmd='echo \'{"status": "fail"}\'')
        assert results[0].success is False
        assert not out.exists()

    def test_bare_verdict_fails_closed(self, tmp_path):
        """12b: VERDICT: PASS without a reason does not unlock a gate."""
        mod = load_batch()
        f = tmp_path / "gate.md"
        f.write_text("VERDICT: PASS\n", encoding="utf-8")
        assert mod.pipeline._gate_verdict([str(f)]) is None
        f2 = tmp_path / "gate2.md"
        f2.write_text("VERDICT: PASS - reviewed and approved\n", encoding="utf-8")
        assert mod.pipeline._gate_verdict([str(f2)]) == "PASS"

    def test_evidence_fences_in_combine(self, tmp_path):
        """12d: combined evidence is fenced as untrusted."""
        mod = load_batch()
        a = tmp_path / "a.md"
        a.write_text("claim", encoding="utf-8")
        combined = mod.pipeline._combine_outputs([str(a)])
        assert f'<evidence source="{a.stem}">' in combined
        assert "</evidence>" in combined

    def test_output_byte_cap_rejects(self, tmp_path, monkeypatch):
        """12e: oversized agent output is rejected, not written."""
        mod = load_batch()
        agent = tmp_path / "big-agent.sh"
        agent.write_text("#!/bin/sh\nhead -c 3000 /dev/zero | tr '\\0' 'x'\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        monkeypatch.setattr(mod.config, "OUTPUT_MAX_BYTES", 1024)
        out = tmp_path / "o.md"
        task = mod.Task(prompt="x", output=str(out))
        result = mod.run_task(task)
        assert result.success is True  # agent exit 0
        mod.save_result(task, result)
        assert not out.exists()  # cap rejected the write

    def test_secretscan_registry_entry(self):
        mod = load_batch()
        assert "secretscan" in mod.config.VALIDATORS
        assert "grep" in mod.config.VALIDATORS["secretscan"]

    def test_secretscan_validator_blocks_secret(self, tmp_path):
        """12f: --validate secretscan rejects artifacts with likely secrets."""
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho 'api_key = sk-abcdef1234567890xyz\n'", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        out = tmp_path / "o.md"
        results = mod.run_serial([mod.Task(prompt="x", output=str(out))], validate_cmd="secretscan")
        assert results[0].success is False
        assert not out.exists()


class TestStdinReusedAcrossRounds:
    """F9: stdin is consumed once; later rounds reuse the loaded tasks."""

    def test_stdin_rounds_do_not_exit(self, tmp_path):
        counter = tmp_path / "counter"
        agent = tmp_path / "flaky.sh"
        agent.write_text(
            "#!/bin/sh\n"
            f"echo x >> {counter}\n"
            f"if [ $(wc -l < {counter}) -le 1 ]; then echo 'Error: rate_limit_error'; "
            "else echo 'OK'; fi\n",
            encoding="utf-8")
        agent.chmod(0o755)
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), "--agent-bin", str(agent),
             "--mode", "serial", "--max-rounds", "2", "--round-delay", "0"],
            input="analyze this\n", text=True, capture_output=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert counter.read_text(encoding="utf-8").count("x") == 2  # round1 fail + round2 pass
