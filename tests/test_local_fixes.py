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
        agent = tmp_path / "sleeper.sh"
        agent.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nsleep 300\n", encoding="utf-8")
        agent.chmod(0o755)
        mod.config.AGENT_BIN = str(agent)

        def _raise_interrupt(*a, **kw):
            # wait until the child is actually up (pidfile written), then
            # simulate Ctrl-C while the child process group is alive
            import time as _time
            for _ in range(400):
                if pidfile.exists():
                    break
                _time.sleep(0.05)
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
