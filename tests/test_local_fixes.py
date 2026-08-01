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
