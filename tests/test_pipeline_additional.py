"""Additional fail-closed pipeline lifecycle contracts."""

from __future__ import annotations

import pytest

from pbatch import pipeline as pipeline_mod
from pbatch.models import Stage
from pbatch.pipeline_schema import validate_pipeline


def test_pipeline_schema_allows_zero_meta_timeout_to_inherit_default():
    data = {"stages": [
        {"name": "source", "from_prompt": "p", "output": "source.md"},
        {"name": "meta", "meta": True, "from_outputs": "source",
         "output_dir": "reviews", "meta_timeout": 0},
    ]}
    assert validate_pipeline(data) == []


def test_pipeline_schema_rejects_non_string_environment_values():
    data = {"stages": [{
        "name": "source", "from_prompt": "p", "output": "source.md",
        "tasks": [{"prompt": "work", "env": {"COUNT": 2}}],
    }]}
    assert any("env must map strings to strings" in error
               for error in validate_pipeline(data))


def test_failed_stage_never_attempts_git_commit(tmp_path, monkeypatch):
    calls = []
    stage = Stage(name="failed", git_commit=True, cwd=str(tmp_path))
    monkeypatch.setattr(pipeline_mod, "_git_commit_stage",
                        lambda *_args: calls.append(True) or True)
    monkeypatch.setattr(pipeline_mod, "_run_stage_commands", lambda _stage: True)
    monkeypatch.setattr(pipeline_mod, "run_stage_repo_validators",
                        lambda *_args: (True, []))

    _, ok = pipeline_mod._finalize_stage(stage, [], [], False, "")

    assert ok is False
    assert calls == []


@pytest.mark.parametrize(("content", "should_commit"), [
    ("VERDICT: PASS - evidence verified\n", True),
    ("VERDICT: FAIL - tests failed\n", False),
])
def test_gate_commit_waits_for_passing_verdict(
        tmp_path, monkeypatch, content, should_commit):
    gate_file = tmp_path / "gate.md"
    gate_file.write_text(content, encoding="utf-8")
    stage = Stage(name="gate", gate=True, git_commit=True, cwd=str(tmp_path))
    stage_outputs, commits = {}, []

    def fake_execute(current, outputs, *_args, **_kwargs):
        outputs[current.name] = [str(gate_file)]
        return [], True

    monkeypatch.setattr(pipeline_mod, "execute_stage", fake_execute)
    monkeypatch.setattr(pipeline_mod, "_git_commit_stage",
                        lambda *_args: commits.append(True) or True)
    stop, _ = pipeline_mod._run_pipeline_stage(
        stage, stage_outputs, [], "", False, 0, "new", "test-session",
        "", False, False, "", "", "")

    assert bool(commits) is should_commit
    assert stop is (not should_commit)
