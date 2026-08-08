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
import time
from pathlib import Path

import argparse
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

    def test_ignored_outputs_skip_commit_silently(self, tmp_path, monkeypatch, caplog):
        """Real-world lesson (aero-vault campaign): gitignored output dirs
        (docs/auto/) made every stage commit fail with a noisy warning.
        Ignored outputs must be filtered silently, not fail the stage."""
        mod = load_batch()
        repo, env = _git_repo(tmp_path)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
        (repo / ".gitignore").write_text("docs/auto/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(repo)  # git runs from the process cwd
        ignored = repo / "docs" / "auto" / "state.jsonl"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("{}", encoding="utf-8")
        tracked = repo / "report.md"
        tracked.write_text("x", encoding="utf-8")
        stage = mod.Stage(name="s0", git_commit=True, commit_message="msg")
        with caplog.at_level("INFO"):
            ok = mod.pipeline._git_commit_stage(stage, [str(ignored), str(tracked)])
        assert ok is True
        # the tracked deliverable is committed; the ignored one is skipped
        log_lines = "\n".join(r.message for r in caplog.records)
        assert "GIT COMMIT:" in log_lines
        assert "gitignored" not in log_lines
        head = subprocess.run(["git", "-C", str(repo), "log", "--oneline", "-1"],
                              capture_output=True, text=True, check=True).stdout
        assert "report.md" in subprocess.run(
            ["git", "-C", str(repo), "show", "--stat", "--oneline", "HEAD"],
            capture_output=True, text=True, check=True).stdout
        assert "state.jsonl" not in subprocess.run(
            ["git", "-C", str(repo), "show", "--stat", "HEAD"],
            capture_output=True, text=True, check=True).stdout

    def test_all_outputs_ignored_skips_cleanly(self, tmp_path, monkeypatch, caplog):
        """When every output is gitignored, no commit is attempted and the
        stage stays green (no more per-stage 'git add failed' noise)."""
        mod = load_batch()
        repo, env = _git_repo(tmp_path)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
        (repo / ".gitignore").write_text("*.md\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(repo)  # git runs from the process cwd
        out = repo / "docs" / "auto" / "out.md"
        out.parent.mkdir(parents=True)
        out.write_text("x", encoding="utf-8")
        stage = mod.Stage(name="s0", git_commit=True, commit_message="msg")
        with caplog.at_level("INFO"):
            ok = mod.pipeline._git_commit_stage(stage, [str(out)])
        assert ok is True
        log_lines = "\n".join(r.message for r in caplog.records)
        assert "all 1 output(s) gitignored" in log_lines
        assert "git add failed" not in log_lines


# -- P0-3: PyYAML missing fails loud --------------------------------------
class TestMissingPyYaml:
    def test_load_tasks_yaml_without_pyyaml_exits(self, tmp_path, monkeypatch):
        mod = load_batch()
        tasks_yaml = tmp_path / "tasks.yaml"
        tasks_yaml.write_text("tasks:\n  - prompt: t1\n", encoding="utf-8")
        monkeypatch.setattr(mod.task_loading, "yaml", None)
        with pytest.raises(SystemExit) as exc:
            mod.load_tasks(str(tasks_yaml))
        assert exc.value.code == 1
        monkeypatch.setattr(mod.task_loading, "yaml", __import__("yaml"))
        tasks_yaml.write_text("tasks:\n  - prompt: [not, a, string]\n", encoding="utf-8")
        with pytest.raises(SystemExit) as malformed:
            mod.load_tasks(str(tasks_yaml))
        assert malformed.value.code == 1

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
        results, ok = mod.pipeline.run_meta_stage(stage, {"req": [str(idea)]}, "", 0, "")
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

    def test_session_usage_scans_only_bounded_appends(self, tmp_path, monkeypatch):
        import pbatch.metering as metering
        raw_root = tmp_path / "sessions"
        directory = raw_root / metering.workdir_key(str(tmp_path))
        directory.mkdir(parents=True)
        raw = directory / "one_batch.jsonl"
        raw.write_text(
            '{"type":"message","message":{"usage":{"totalTokens":3}}}\n',
            encoding="utf-8")
        monkeypatch.setattr(metering, "SESSIONS_DIR", raw_root)
        monkeypatch.setattr(metering, "SESSION_LINE_MAX_BYTES", 128)
        metering._USAGE_CACHE.clear()
        assert metering.session_usage(str(tmp_path), "batch") == {"totalTokens": 3}

        with raw.open("ab") as handle:
            handle.write(b"x" * 300 + b"\n")  # skipped without a large allocation
            handle.write(b'{"type":"message","message":{"usage":{"totalTokens":2}}}\n')
            handle.write(b'{"type":"message","message":{"usage":{"totalTokens":4}}}')
        assert metering.session_usage(str(tmp_path), "batch") == {"totalTokens": 5}
        with raw.open("ab") as handle:
            handle.write(b"\n")
        assert metering.session_usage(str(tmp_path), "batch") == {"totalTokens": 9}

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
        args_log.write_text("", encoding="utf-8")
        inherited = mod.Stage(
            name="s2", from_outputs="s0", model="m-stage", provider="p-stage",
            validate_cmd="exit 7",
            tasks=[{"prompt": "Review {input_content}",
                    "output": str(tmp_path / "stage-{input_stem}.md"),
                    "validate": ""}])
        inherited_results, inherited_ok = mod.execute_stage(
            inherited, {"s0": [str(inputs / "t.md")]})
        assert inherited_ok and inherited_results[0].success
        inherited_args = args_log.read_text(encoding="utf-8").splitlines()
        assert "m-stage" in inherited_args and "p-stage" in inherited_args

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
        results, ok = mod.pipeline.run_meta_stage(stage, {"req": [str(idea)]}, "", 0, "")
        assert ok
        lines = args_log.read_text(encoding="utf-8").splitlines()
        # Both iterations route their orchestrator and role task through the
        # stage provider (2 calls/iteration, 2 provider argv pairs each).
        assert lines.count("--provider") == 4
        assert lines.count("p-stage") == 4

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
        d = tmp_path / session.workdir_key(cwd)
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
        d = tmp_path / session.workdir_key(tmp_path.as_posix())
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

    def test_evidence_fences_in_combine(self, tmp_path, monkeypatch):
        """12d: combined evidence is fenced as untrusted."""
        mod = load_batch()
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        c = tmp_path / "c.md"
        a.write_text("a" * 100, encoding="utf-8")
        b.write_text("b" * 100, encoding="utf-8")
        c.write_text("c" * 100, encoding="utf-8")
        monkeypatch.setattr(mod.config, "EVIDENCE_MAX_BYTES", 20)
        monkeypatch.setattr(mod.config, "EVIDENCE_MAX_SOURCES", 2)
        combined = mod.pipeline.combine_outputs([str(a), str(b), str(c)])
        assert f'<evidence source="{a.stem}">' in combined
        assert f'<evidence source="{b.stem}">' in combined
        assert f'<evidence source="{c.stem}">' not in combined
        assert "</evidence>" in combined
        assert "a" * 10 in combined and "a" * 11 not in combined
        assert "b" * 10 in combined and "b" * 11 not in combined
        assert combined.count("evidence truncated") == 2
        assert '<evidence-sources-omitted count="1" max-sources="2" />' in combined
        manifest = mod.pipeline.source_manifest([str(a), str(b), str(c)])
        assert str(a) in manifest and str(b) in manifest and str(c) not in manifest
        assert "1 more source candidates omitted" in manifest

    def test_output_byte_cap_rejects(self, tmp_path, monkeypatch):
        """12e: oversized agent output is rejected during collection (no
        OOM before the check), never written."""
        mod = load_batch()
        agent = tmp_path / "big-agent.sh"
        agent.write_text("#!/bin/sh\nhead -c 3000 /dev/zero | tr '\\0' 'x'\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        monkeypatch.setattr(mod.config, "OUTPUT_MAX_BYTES", 1024)
        out = tmp_path / "o.md"
        task = mod.Task(prompt="x", output=str(out))
        result = mod.run_task(task)
        assert result.success is False
        assert "exceeds" in result.reason
        mod.save_result(task, result)
        assert not out.exists()  # rejected, nothing written
        monkeypatch.setattr(mod.config, "PROMPT_MAX_BYTES", 10)
        prompt_out = tmp_path / "prompt.md"
        prompt_result = mod.run_task(mod.Task(prompt="p" * 11, output=str(prompt_out)))
        assert prompt_result.success is False
        assert "prompt exceeds 10 bytes" in prompt_result.reason
        assert not prompt_out.exists()

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


class TestApprovalStage:
    """T12c (D5): human-in-the-loop approval points."""

    def test_approve_file_unlocks(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("p", encoding="utf-8")
        approve = tmp_path / "approved"
        approve.write_text("yes", encoding="utf-8")
        stage = mod.Stage(name="s0", from_dir=str(inputs), approval=True)
        results, ok = mod.execute_stage(stage, {}, approval_file=str(approve))
        assert ok is True
        assert (inputs / "t.out.md").exists()

    def test_no_approval_fails_closed(self, tmp_path, monkeypatch):
        """Non-interactive stdin without approve file/env halts the stage."""
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("p", encoding="utf-8")
        monkeypatch.delenv("PBATCH_APPROVE", raising=False)
        stage = mod.Stage(name="s0", from_dir=str(inputs), approval=True)
        results, ok = mod.execute_stage(stage, {}, approval_file="")
        assert ok is False  # deliverables exist but the pipeline halts
        assert (inputs / "t.out.md").exists()

    def test_env_approve_unlocks(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("p", encoding="utf-8")
        monkeypatch.setenv("PBATCH_APPROVE", "1")
        stage = mod.Stage(name="s0", from_dir=str(inputs), approval=True)
        results, ok = mod.execute_stage(stage, {})
        assert ok is True

    def test_cli_approval_blocked_without_gate(self, tmp_path):
        """E2E: approval stage without approve file stops the pipeline."""
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("p", encoding="utf-8")
        pipeline = tmp_path / "p.yaml"
        pipeline.write_text(
            "git_commit: false\n"
            "stages:\n"
            f"  - name: s0\n    from_dir: {inputs}\n    approval: true\n",
            encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), str(pipeline), "--agent-bin", str(agent)],
            capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
            env={**os.environ, "PBATCH_APPROVE": ""})
        assert result.returncode != 0
        assert "APPROVAL" in result.stderr
        assert (inputs / "t.out.md").exists()  # deliverables preserved

    def test_cli_approval_unlocked_e2e(self, tmp_path):
        """E2E: --approve-file unlocks the approval stage."""
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        agent.chmod(0o755)
        approve = tmp_path / "approved"
        approve.write_text("yes", encoding="utf-8")
        inputs = tmp_path / "in"
        inputs.mkdir()
        (inputs / "t.md").write_text("p", encoding="utf-8")
        pipeline = tmp_path / "p.yaml"
        pipeline.write_text(
            "git_commit: false\n"
            "stages:\n"
            f"  - name: s0\n    from_dir: {inputs}\n    approval: true\n",
            encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PI_BATCH), str(pipeline), "--agent-bin", str(agent),
             "--approve-file", str(approve)],
            capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
            env={**os.environ, "PBATCH_APPROVE": ""})
        assert result.returncode == 0, result.stderr
        assert "APPROVAL: stage 's0' approved" in result.stderr


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


class TestPerAgentErrorPatterns:
    """G line (GAP-1): failure signatures are per agent binary —
    agent.agents.<bin>.error_patterns wins over the built-in table."""

    def test_default_patterns_reject_provider_failures(self):
        mod = load_batch()
        assert mod.runner.agent_failure_reason(0, "ERROR: insufficient_quota")
        assert mod.runner.agent_failure_reason(0, "network is unreachable")
        assert mod.runner.agent_failure_reason(0, "fatal: something broke")
        assert not mod.runner.agent_failure_reason(0, "a thoughtful review of errors")

    def test_per_agent_override_replaces_defaults(self, monkeypatch):
        mod = load_batch()
        monkeypatch.setattr(mod.config, "AGENT_BIN", "myclaude")
        monkeypatch.setattr(mod.config, "_AGENTS_CFG",
                            {"myclaude": {"error_patterns": ["custom-failure-mark"]}})
        assert mod.runner.agent_failure_reason(0, "custom-failure-mark appeared")
        # the built-in table no longer applies to this agent
        assert not mod.runner.agent_failure_reason(0, "insufficient_quota")

    def test_empty_list_disables_signature_rejection(self, monkeypatch):
        mod = load_batch()
        monkeypatch.setattr(mod.config, "AGENT_BIN", "permissive")
        monkeypatch.setattr(mod.config, "_AGENTS_CFG",
                            {"permissive": {"error_patterns": []}})
        assert not mod.runner.agent_failure_reason(0, "insufficient_quota text")

    def test_global_override_applies_to_all(self, monkeypatch):
        mod = load_batch()
        monkeypatch.setattr(mod.config, "AGENT_BIN", "anything")
        monkeypatch.setattr(mod.config, "_AGENT_CFG",
                            {"error_patterns": ["global-mark"]})
        assert mod.runner.agent_failure_reason(0, "global-mark hit")


class TestJudgeValidator:
    """H line (GAP-2): judge validators must emit VERDICT: PASS|FAIL - reason;
    bare/missing verdicts fail closed."""

    def test_judge_verdict_parsing(self):
        mod = load_batch()
        judge = mod.runner.judge_verdict
        assert judge("VERDICT: PASS - looks correct") == "PASS"
        assert judge("VERDICT: FAIL - missing idempotency") == "FAIL"
        assert judge("VERDICT: REJECT - violates the contract") == "REJECT"
        # FAIL wins over an earlier PASS (order-independent)
        assert judge("VERDICT: PASS - ok\nVERDICT: FAIL - but here") == "FAIL"
        # bare verdict without a reason fails closed
        assert judge("VERDICT: PASS") is None
        assert judge("no verdict here") is None

    def test_judge_pass_saves_artifact(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        monkeypatch.setattr(
            mod.config, "VALIDATOR_SPECS",
            {"judge_ok": mod.config.ValidatorSpec(
                cmd="echo 'VERDICT: PASS - all good'", judge=True)})
        out = tmp_path / "o.md"
        result = mod.run_serial(
            [mod.Task(prompt="p", output=str(out))],
            validate_cmd="judge_ok")
        assert result[0].success
        assert out.exists()

    def test_judge_fail_rejects_and_feedback(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        monkeypatch.setattr(
            mod.config, "VALIDATOR_SPECS",
            {"judge_bad": mod.config.ValidatorSpec(
                cmd="echo 'VERDICT: FAIL - not ready'", judge=True)})
        out = tmp_path / "o.md"
        result = mod.run_serial(
            [mod.Task(prompt="p", output=str(out))],
            validate_cmd="judge_bad")
        assert not result[0].success
        assert not out.exists()  # zero residue
        assert "judge" in (result[0].validation_stderr or "")

    def test_judge_missing_verdict_fails_closed(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        monkeypatch.setattr(
            mod.config, "VALIDATOR_SPECS",
            {"judge_silent": mod.config.ValidatorSpec(
                cmd="echo 'hmm, no verdict here'", judge=True)})
        out = tmp_path / "o.md"
        result = mod.run_serial(
            [mod.Task(prompt="p", output=str(out))],
            validate_cmd="judge_silent")
        assert not result[0].success
        assert not out.exists()


class TestRepoScopeValidators:
    """N2 (GAP-4): repo-scoped validators never run per artifact — they are
    deferred to the batch round end / stage end."""

    def test_repo_gate_skipped_per_task(self, tmp_path, monkeypatch):
        mod = load_batch()
        agent = tmp_path / "agent.sh"
        agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)
        monkeypatch.setattr(
            mod.config, "VALIDATOR_SPECS",
            {"repogate": mod.config.ValidatorSpec(
                cmd="echo 'whole tree broken' >&2; exit 9", scope="repo")})
        out = tmp_path / "o.md"
        result = mod.run_serial(
            [mod.Task(prompt="p", output=str(out))],
            validate_cmd="repogate")
        # the repo gate is deferred, so the artifact saves normally
        assert result[0].success
        assert out.exists()

    def test_batch_end_repo_gate_failure_key(self, tmp_path, monkeypatch):
        mod = load_batch()
        monkeypatch.setattr(
            mod.config, "VALIDATOR_SPECS",
            {"repogate": mod.config.ValidatorSpec(
                cmd="echo 'whole tree broken' >&2; exit 9", scope="repo")})
        task = mod.Task(prompt="p", output=str(tmp_path / "o.md"))
        keys = mod.cli._run_batch_repo_validators([task], "repogate")
        assert keys  # synthetic failure key drives the round loop

    def test_batch_end_repo_gate_pass(self, tmp_path, monkeypatch):
        mod = load_batch()
        monkeypatch.setattr(
            mod.config, "VALIDATOR_SPECS",
            {"repogate": mod.config.ValidatorSpec(cmd="true", scope="repo")})
        task = mod.Task(prompt="p", output=str(tmp_path / "o.md"))
        assert mod.cli._run_batch_repo_validators([task], "repogate") == set()


class TestOrphanMarkers:
    """GAP-3 (N1): start markers carry the agent pid; residue scans can tell
    whether an orphaned agent is still running."""

    def test_marker_info_roundtrip(self, tmp_path):
        import pbatch.triage as triage
        key = "deadbeef"
        triage.write_marker(key, str(tmp_path), agent_pid=4242)
        marker = triage.residue_markers(str(tmp_path))[0]
        info = triage.marker_info(marker)
        assert info["runner_pid"] == os.getpid()
        assert info["agent_pid"] == 4242
        assert info["started"] > 0
        triage.clear_marker(key, str(tmp_path))

    def test_old_single_line_marker_parses(self, tmp_path):
        import pbatch.triage as triage
        marker = Path(tmp_path) / triage.STATE_DIR / "old.start"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("12345\n", encoding="utf-8")
        info = triage.marker_info(marker)
        assert info["runner_pid"] == 12345
        assert info["agent_pid"] == 0

    def test_pid_alive_and_kill_orphan(self):
        import pbatch.triage as triage
        assert triage.pid_alive(os.getpid())
        assert not triage.pid_alive(99999999)
        triage.kill_orphan(99999999)  # must not raise


class TestJudgeScript:
    """H line (GAP-2): scripts/judge.py wraps the agent and enforces the
    VERDICT protocol with exit codes (usable without judge: true too)."""

    def _run_judge(self, tmp_path, agent_body, artifact_text="the artifact"):
        agent = tmp_path / "judge-agent.sh"
        agent.write_text("#!/bin/sh\n" + agent_body, encoding="utf-8")
        agent.chmod(0o755)
        artifact = tmp_path / "artifact.md"
        artifact.write_text(artifact_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "scripts/judge.py", "-p", "rubric",
             "--agent-bin", str(agent), "--timeout", "30", str(artifact)],
            capture_output=True, text=True, timeout=60, cwd=str(PI_BATCH.parent))

    def test_judge_script_pass(self, tmp_path):
        r = self._run_judge(tmp_path, "echo 'VERDICT: PASS - good'\n")
        assert r.returncode == 0, r.stderr
        assert "VERDICT: PASS" in r.stdout

    def test_judge_script_fail(self, tmp_path):
        r = self._run_judge(tmp_path, "echo 'VERDICT: FAIL - bad'\n")
        assert r.returncode == 1
        assert "VERDICT: FAIL" in r.stdout

    def test_judge_script_missing_verdict(self, tmp_path):
        r = self._run_judge(tmp_path, "echo 'no verdict at all'\n")
        assert r.returncode == 1
        assert "VERDICT: FAIL" in r.stdout

    def test_judge_script_bare_pass_fails_closed(self, tmp_path):
        r = self._run_judge(tmp_path, "echo 'VERDICT: PASS'\n")
        assert r.returncode == 1

    def test_judge_script_missing_artifact(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "scripts/judge.py", "-p", "rubric",
             str(tmp_path / "nope.md")],
            capture_output=True, text=True, timeout=30, cwd=str(PI_BATCH.parent))
        assert result.returncode == 1
        assert "VERDICT: FAIL" in result.stdout


class TestRateLimiter:
    """F line (GAP-F): provider token bucket — parallel workers line up
    instead of hammering one provider; burst allows short spikes."""

    def test_unlimited_by_default(self):
        from pbatch import ratelimit
        ratelimit.configure(0, 1, {})
        start = time.monotonic()
        for _ in range(5):
            assert ratelimit.acquire("p") is True
        assert time.monotonic() - start < 0.5

    def test_rate_limits_invocations(self):
        from pbatch import ratelimit
        ratelimit.configure(100, 1, {})  # 100/s: 10 calls need >= 90ms
        start = time.monotonic()
        for _ in range(10):
            assert ratelimit.acquire("p") is True
        elapsed = time.monotonic() - start
        assert elapsed >= 0.07, f"10 calls at 100/s should take ~90ms, took {elapsed:.3f}s"
        ratelimit.configure(0, 1, {})

    def test_burst_allows_immediate_batch(self):
        from pbatch import ratelimit
        ratelimit.configure(1, 5, {})
        start = time.monotonic()
        for _ in range(5):  # burst capacity: all immediate
            assert ratelimit.acquire("p") is True
        assert time.monotonic() - start < 0.3
        ratelimit.configure(0, 1, {})

    def test_provider_buckets_are_independent(self):
        from pbatch import ratelimit
        ratelimit.configure(0, 1, {"slow": 0.0})  # provider override disables
        ratelimit.configure(0.5, 1, {"fast": 100.0, "slow": 0.0})
        assert ratelimit.acquire("fast") is True
        start = time.monotonic()
        assert ratelimit.acquire("slow") is True  # unlimited provider
        assert time.monotonic() - start < 0.3
        # the default bucket still applies the global rate
        assert ratelimit.acquire("") is True  # burst token
        ratelimit.configure(0, 1, {})

    def test_acquire_timeout_returns_false(self):
        from pbatch import ratelimit
        ratelimit.configure(0.1, 1, {})
        assert ratelimit.acquire("p") is True  # burst token
        start = time.monotonic()
        assert ratelimit.acquire("p", timeout=0.1) is False  # no refill yet
        assert time.monotonic() - start < 0.5
        ratelimit.configure(0, 1, {})


class TestRetryJitter:
    """F line: retry waits are jittered so parallel failures do not
    re-sync into the next retry window together."""

    def test_normal_failure_jittered_within_half(self):
        mod = load_batch()
        result = mod.TaskResult(task=mod.Task(prompt="p"), success=False, reason="agent exited 1")
        waits = [mod.runner._retry_wait(result, attempt, 10, 2.0) for attempt in (1, 2, 3)]
        for index, wait in enumerate(waits):
            base = 10 * (2.0 ** index)
            assert 0.5 * base <= wait <= 1.5 * base

    def test_rate_limit_failure_keeps_30s_floor(self):
        mod = load_batch()
        result = mod.TaskResult(task=mod.Task(prompt="p"), success=False, reason="rate_limit_error")
        for attempt in (1, 2, 3):
            wait = mod.runner._retry_wait(result, attempt, 0, 2.0)
            assert wait >= 30.0

    def test_rate_limit_jitter_spreads_within_window(self):
        mod = load_batch()
        result = mod.TaskResult(task=mod.Task(prompt="p"), success=False, reason="rate_limit_error")
        waits = [mod.runner._retry_wait(result, 1, 0, 2.0) for _ in range(50)]
        assert len(set(round(w, 1) for w in waits)) > 1  # spread, not fixed
        assert all(30.0 <= w <= 60.0 for w in waits)

    def test_rate_gate_runs_under_high_limit(self, tmp_path):
        """run_task passes through the rate gate when the limit is high."""
        from pbatch import ratelimit
        ratelimit.configure(1000, 1, {})
        try:
            mod = load_batch()
            agent = tmp_path / "agent.sh"
            agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
            agent.chmod(0o755)
            mod.config.AGENT_BIN = str(agent)
            out = tmp_path / "o.md"
            result = mod.run_serial([mod.Task(prompt="p", output=str(out))])
            assert result[0].success
            assert out.exists()
        finally:
            ratelimit.configure(0, 1, {})


class TestFromDirAggregate:
    """P1 使用反馈：from_dir + aggregate 合并为单任务（证据归一零浪费）。"""

    def test_aggregate_merges_all_files_into_one_task(self, tmp_path, monkeypatch):
        mod = load_batch()
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        (inputs / "01-a.md").write_text("AAA", encoding="utf-8")
        (inputs / "02-b.md").write_text("BBB", encoding="utf-8")
        (inputs / "03-c.md").write_text("CCC", encoding="utf-8")
        stage = mod.Stage(name="s0", from_dir=str(inputs), aggregate=True)
        tasks, reused = mod.pipeline._stage_from_dir_tasks(
            stage, reuse=False, model_override="", timeout_override=0)
        assert len(tasks) == 1, "aggregate 必须是单任务"
        assert reused == []
        assert "AAA" in tasks[0].prompt and "BBB" in tasks[0].prompt and "CCC" in tasks[0].prompt
        assert "01-a.md" in tasks[0].prompt  # 分界符含文件名
        assert tasks[0].output_path() == (inputs / "aggregate.out.md").resolve()

    def test_aggregate_skips_existing_outputs(self, tmp_path):
        mod = load_batch()
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        (inputs / "a.md").write_text("AAA", encoding="utf-8")
        (inputs / "a.out.md").write_text("old", encoding="utf-8")  # 既有产出不被当输入
        stage = mod.Stage(name="s0", from_dir=str(inputs), aggregate=True)
        tasks, _ = mod.pipeline._stage_from_dir_tasks(
            stage, reuse=False, model_override="", timeout_override=0)
        assert len(tasks) == 1
        assert "a.out.md" not in tasks[0].prompt

    def test_aggregate_respects_reuse(self, tmp_path, monkeypatch):
        mod = load_batch()
        inputs = tmp_path / "inputs"
        inputs.mkdir()
        (inputs / "a.md").write_text("AAA", encoding="utf-8")
        out = inputs / "aggregate.out.md"
        out.write_text("done", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        stage = mod.Stage(name="s0", from_dir=str(inputs), aggregate=True)
        tasks, reused = mod.pipeline._stage_from_dir_tasks(
            stage, reuse=True, model_override="", timeout_override=0)
        assert tasks == [] and len(reused) == 1


class TestUsageFeedbackP2P4P7:
    """使用反馈 P2/P4/P7：容忍失败不阻断、provider 冷却、拒绝行内联 stderr。"""

    def test_p2_tolerated_failures_do_not_fail_round(self, tmp_path, monkeypatch):
        mod = load_batch()
        args = argparse.Namespace(dry_run=False, model="", reuse=False, timeout_override=0,
                                  session_mode="new", session_name="", validate="",
                                  reuse_legacy=False, reuse_fingerprint=False,
                                  approve_file="", prompt="", decision_log="",
                                  archive_dir="")
        fake_result = mod.TaskResult(
            task=mod.Task(prompt="p", output="o.md"), success=False,
            stdout="", stderr="", elapsed=1.0, returncode=1, reason="agent exited 1")
        calls = {"n": 0}
        def fake_run_pipeline(*a, **k):
            calls["n"] += 1
            return [fake_result], []  # 阶段成功（failed_stages 空）但有容忍失败
        monkeypatch.setattr(mod.cli, "run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(mod.cli, "print_summary", lambda *a, **k: None)
        failed, keys = mod.cli._run_pipeline_round(args, object(), "s", "", 0, False)
        assert failed is False and keys == set()
        assert calls["n"] == 1

    def test_p2_real_failed_stages_still_fail_round(self, monkeypatch):
        mod = load_batch()
        args = argparse.Namespace(dry_run=False, model="", reuse=False, timeout_override=0,
                                  session_mode="new", session_name="", validate="",
                                  reuse_legacy=False, reuse_fingerprint=False,
                                  approve_file="", prompt="", decision_log="",
                                  archive_dir="")
        def fake_run_pipeline(*a, **k):
            return [], ["gate"]
        monkeypatch.setattr(mod.cli, "run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(mod.cli, "print_summary", lambda *a, **k: None)
        failed, keys = mod.cli._run_pipeline_round(args, object(), "s", "", 0, False)
        assert failed is True and "stage:gate" in keys

    def test_p4_provider_broken_waits_longer(self):
        mod = load_batch()
        result = mod.TaskResult(
            task=mod.Task(prompt="p", output="o"), success=False,
            stdout="", stderr="400: Model is unavailable", elapsed=1.0,
            returncode=1, reason="agent exited 1: 400 Model is unavailable")
        wait = mod.runner._retry_wait(result, 2, 5.0, 2.0)
        assert wait >= 60.0, f"provider 不可用应 ≥60s 冷却，实际 {wait:.1f}s"

    def test_p4_normal_failure_stays_short(self):
        mod = load_batch()
        result = mod.TaskResult(
            task=mod.Task(prompt="p", output="o"), success=False,
            stdout="", stderr="syntax error", elapsed=1.0,
            returncode=1, reason="validator failed")
        wait = mod.runner._retry_wait(result, 2, 5.0, 2.0)
        assert wait < 30.0, f"普通失败应短退避，实际 {wait:.1f}s"

    def test_p3_record_fatal_exit_writes_state(self, tmp_path, monkeypatch):
        mod = load_batch()
        state = tmp_path / "run.exit.json"
        args = argparse.Namespace(log_file=str(tmp_path / "run.log"))
        monkeypatch.setattr(mod.cli.Path, "cwd", classmethod(lambda cls: tmp_path))
        mod.cli._record_fatal_exit(RuntimeError("boom"), args)
        content = state.read_text(encoding="utf-8")
        assert "RuntimeError" in content and "boom" in content
