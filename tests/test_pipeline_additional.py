"""Additional fail-closed pipeline lifecycle contracts."""

from __future__ import annotations

import threading

import pytest

from pbatch import meta as meta_mod
from pbatch import pipeline as pipeline_mod
from pbatch.models import Stage, Task, TaskResult
from pbatch.tui_native import (
    NativePipelineControlChannel, NativePipelineObserver)
from pbatch.pipeline_schema import validate_pipeline


def test_pipeline_optional_native_sink_observes_stage_lifecycle(monkeypatch):
    events = []
    pipeline = pipeline_mod.Pipeline(
        stages=[pipeline_mod.Stage(name="build")], name="demo")

    def fake_stage(*args, **kwargs):
        return False, []

    monkeypatch.setattr(pipeline_mod, "_run_pipeline_stage", fake_stage)
    results, failed = pipeline_mod.run_pipeline(
        pipeline, event_sink=events.append)

    assert results == [] and failed == []
    plan = next(event for event in events if event.kind == "plan.updated")
    assert plan.payload["source"] == "deterministic-preflight"
    assert plan.payload["items"][0]["id"] == "build"
    assert [event.kind for event in events] == [
        "turn.queued", "turn.started", "pipeline.started", "plan.updated",
        "actor.spawned", "stage.started", "stage.completed", "actor.completed",
        "pipeline.checkpoint", "pipeline.completed", "turn.completed",
    ]
    assert [event.seq for event in events] == list(range(1, len(events) + 1))


def test_native_approval_channel_unblocks_pipeline_stage(tmp_path, monkeypatch):
    events = []
    channel = NativePipelineControlChannel()
    approval_seen = threading.Event()

    def fake_stage(stage, *_args, **_kwargs):
        accepted = pipeline_mod._check_approval(stage, ["out.md"], "")
        return not accepted, []

    monkeypatch.setattr(pipeline_mod, "_run_pipeline_stage", fake_stage)

    def sink(event):
        events.append(event)
        if event.kind == "approval.requested":
            approval_seen.set()

    outcome = {}
    pipeline = pipeline_mod.Pipeline(
        stages=[pipeline_mod.Stage(name="review", approval=True,
                                   cwd=str(tmp_path))], name="demo")

    def run():
        outcome["value"] = pipeline_mod.run_pipeline(
            pipeline, event_sink=sink, approval_channel=channel)

    worker = threading.Thread(target=run)
    worker.start()
    assert approval_seen.wait(2)
    request = next(event for event in events
                   if event.kind == "approval.requested")
    payload = dict(request.payload, workspace_id=request.workspace_id,
                   session_id=request.session_id, run_id=request.run_id,
                   turn_id=request.turn_id, actor_id=request.actor_id,
                   approval_type="pipeline_stage", decision="allow")
    assert channel.resolve(payload) is True
    worker.join(2)
    assert not worker.is_alive()
    assert outcome["value"] == ([], [])
    assert events[-1].kind == "turn.completed"


def test_meta_role_batch_emits_typed_child_actor_lifecycle(tmp_path, monkeypatch):
    events = []
    observer = NativePipelineObserver(events.append, str(tmp_path))
    observer.start("demo", 1)
    observer.stage_started("review", 1, 1)
    task = Task("review", output="review.md", cwd=str(tmp_path))
    result = TaskResult(task, True, returncode=0)
    all_results = []
    monkeypatch.setattr(meta_mod, "current_native_observer", lambda: observer)
    monkeypatch.setattr(
        meta_mod, "_run_role_batch",
        lambda *_args: all_results.append(result) or [])

    assert meta_mod._run_observed_role_batch(
        Stage(name="review", cwd=str(tmp_path)),
        [("security_engineer", task)], "", "session", False, False,
        False, all_results, [], [], 1) == []

    spawned = next(event for event in events
                   if event.kind == "actor.spawned"
                   and event.payload.get("role") == "pipeline.meta-role")
    completed = next(event for event in events
                     if event.kind == "actor.completed"
                     and event.actor_id == spawned.actor_id)
    assert spawned.payload["parent_actor_id"] == "stage_1_review"
    assert completed.payload["success"] is True


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
