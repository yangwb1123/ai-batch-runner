"""Native pipeline RuntimeEvent observation contracts."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from pbatch import config
from pbatch.pipeline import run_pipeline
from pbatch.models import Pipeline, Stage, Task
from pbatch.pipeline_native import execute_stage_tasks
from pbatch.tui_native import (
    NativePipelineApprovalChannel, NativePipelineAttachment,
    NativePipelineControlChannel, NativePipelineObserver)
from pbatch.pipeline_native import deterministic_preflight_plan
from pbatch.tui_pipeline_checkpoint import PipelineCheckpointProjection


def test_pipeline_checkpoint_projection_rejects_inconsistent_boundaries():
    events = []
    observer = NativePipelineObserver(events.append, "/tmp/project")
    observer.pipeline_checkpoint("build", 1, 1, True, task_count=1,
                                 successful_tasks=1)
    projection = PipelineCheckpointProjection()
    projection.validate(events[0])
    bad = replace(events[0], payload={**events[0].payload,
                                      "next_stage_index": 1})
    with pytest.raises(ValueError, match="counts are inconsistent"):
        projection.validate(bad)


def test_pipeline_checkpoint_projection_rejects_stage_order_regression():
    events = []
    observer = NativePipelineObserver(events.append, "/tmp/project")
    observer.pipeline_checkpoint("first", 1, 2, True)
    observer.pipeline_checkpoint("second", 1, 2, True)
    projection = PipelineCheckpointProjection()
    projection.consume(events[0])
    with pytest.raises(ValueError, match="stage order regressed"):
        projection.validate(events[1])


def test_deterministic_preflight_plan_contains_only_explicit_pipeline_bounds(tmp_path):
    stage = Stage(
        name="review", cwd=str(tmp_path), from_dir="inputs",
        output="docs/review.md", output_dir="docs/roles",
        validate_cmd="quality", gate=True, approval=True,
        commands=["pytest -q"], verify_experiment=["./smoke.sh"],
        tasks=[{"output": "docs/task.md", "validate": "schema"}],
    )
    pipeline = Pipeline(
        stages=[stage], decision_log="docs/DECISIONS.md",
        archive_dir="docs/archive", name="demo")

    plan = deterministic_preflight_plan(
        pipeline, validate_cmd="fallback", retries=2, timeout_override=90)

    assert plan["source"] == "deterministic-preflight"
    assert plan["items"] == [{
        "id": "review", "title": "review", "state": "pending",
        "actor": "", "dependencies": [], "index": 1,
    }]
    assert plan["read_scope"] == ["inputs"]
    assert plan["write_scope"] == [
        "docs/review.md", "docs/roles", "docs/task.md",
        "docs/DECISIONS.md", "docs/archive"]
    assert plan["commands"] == ["pytest -q", "./smoke.sh"]
    assert plan["validators"] == ["review: quality", "review: schema"]
    assert plan["gates"] == ["gate:review", "approval:review"]
    assert plan["budget"] == {
        "retries": "2", "task_timeout_s": "90", "command_timeout_s": "600"}
    assert "network_intent" not in plan
    assert "external_tools" not in plan


def test_native_observer_emits_ordered_bounded_pipeline_facts():
    events = []
    observer = NativePipelineObserver(events.append, "/tmp/project")
    observer.start("demo", 1)
    observer.plan_updated({"source": "test", "items": []})
    observer.stage_started("build", 1, 1)
    task = SimpleNamespace(
        effective_agent=lambda: "pi", output="docs/result.md", validate="quick")
    item_id = observer.task_started("build", 1, 1, task)
    observer.validation_started(item_id, task)
    observer.task_completed(
        item_id, task,
        SimpleNamespace(success=True, returncode=0, reason="", validation_ok=True,
                         validation_exit=0))
    observer.validation_completed(item_id, task,
                                  SimpleNamespace(validation_ok=True,
                                                 validation_exit=0))
    observer.stage_completed("build", 1, 1, True, 1)
    observer.pipeline_checkpoint("build", 1, 1, True, task_count=1,
                                 successful_tasks=1)
    observer.gate_verdict("gate", "PASS", artifact_count=1)
    observer.finish(True)

    assert [event.kind for event in events] == [
        "turn.queued", "turn.started", "pipeline.started", "plan.updated",
        "actor.spawned", "stage.started", "actor.spawned", "task.started",
        "validation.started",
        "task.completed", "actor.completed", "validation.completed",
        "stage.completed", "actor.completed", "pipeline.checkpoint",
        "gate.verdict",
        "pipeline.completed", "turn.completed",
    ]
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[2].payload["stage_count"] == 1
    plan_event = next(item for item in events if item.kind == "plan.updated")
    assert plan_event.payload["plan_id"].startswith("plan_")
    assert len(plan_event.payload["plan_digest"]) == 64
    assert events[4].actor_id == "stage_1_build"
    assert events[7].payload["parent_actor_id"] == "stage_1_build"
    assert events[7].extensions["plan_digest"] == plan_event.payload["plan_digest"]
    checkpoint = next(item for item in events if item.kind == "pipeline.checkpoint")
    assert checkpoint.payload["resume_boundary"] == "next_stage"
    assert checkpoint.payload["completed_stages"] == 1
    assert events[-1].payload["state"] == "SUCCEEDED"
    assert all(len(event.to_jsonl().encode()) <= 128 * 1024 for event in events)


def test_native_tui_attachment_accepts_only_scoped_read_only_native_events():
    accepted = []
    attachment = NativePipelineAttachment("/tmp/project", accepted.append)
    observer = NativePipelineObserver(
        attachment, "/tmp/project", session_id="ses", run_id="run",
        turn_id="turn")

    observer.start("demo", 0)
    assert len(accepted) == 3
    assert attachment.events_accepted == 3

    with pytest.raises(ValueError, match="workspace"):
        attachment(replace(accepted[0], workspace_id="/tmp/other"))
    assert attachment.closed


def test_native_tui_attachment_close_does_not_wait_for_consumer():
    event_source = []
    NativePipelineObserver(event_source.append, "/tmp/project").start("demo", 0)
    close_finished = threading.Event()
    close_thread = None
    attachment = None

    def close_attachment():
        attachment.close()
        close_finished.set()

    def consume(_event):
        nonlocal close_thread
        close_thread = threading.Thread(target=close_attachment)
        close_thread.start()
        assert close_finished.wait(timeout=2)

    attachment = NativePipelineAttachment("/tmp/project", consume)
    attachment(event_source[0])
    close_thread.join(timeout=2)
    assert not close_thread.is_alive()
    assert attachment.closed
    assert attachment.events_accepted == 1
    with pytest.raises(RuntimeError, match="closed"):
        attachment(event_source[1])


def test_native_approval_channel_requires_exact_run_scope():
    events = []
    requested = threading.Event()

    def sink(event):
        events.append(event)
        if event.kind == "approval.requested":
            requested.set()

    channel = NativePipelineApprovalChannel(ttl=1)
    observer = NativePipelineObserver(
        sink, "/tmp/project", session_id="session", run_id="run",
        turn_id="turn", actor_id="pipeline")
    channel.bind(observer)
    observer.bind_approval_channel(channel)
    observer.start("demo", 1)
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("accepted", channel.request(
            "review", ["docs/review.md"])))
    worker.start()
    assert requested.wait(1)
    event = next(item for item in events if item.kind == "approval.requested")
    payload = event.payload
    wrong = dict(payload, workspace_id="/tmp/other", session_id="session",
                 run_id="run", turn_id="turn", actor_id="pipeline")
    assert channel.resolve(wrong) is False
    decision = dict(payload, workspace_id="/tmp/project", session_id="session",
                    run_id="run", turn_id="turn", actor_id="pipeline",
                    approval_type="pipeline_stage", decision="allow")
    assert channel.resolve(decision) is True
    worker.join(1)
    assert outcome == {"accepted": True}
    assert [item.kind for item in events if item.kind.startswith("approval.")] == [
        "approval.requested", "approval.resolved"]


def test_native_approval_channel_expires_without_decision():
    events = []
    channel = NativePipelineApprovalChannel(ttl=0.01)
    observer = NativePipelineObserver(events.append, "/tmp/project")
    channel.bind(observer)
    observer.bind_approval_channel(channel)
    observer.start("demo", 1)
    assert channel.request("review", []) is False
    assert [item.kind for item in events if item.kind.startswith("approval.")] == [
        "approval.requested", "approval.expired"]


def test_native_pipeline_stage_routes_task_cancel_without_cancelling_run():
    events = []
    channel = NativePipelineControlChannel()
    observer = NativePipelineObserver(
        events.append, "/tmp/project", session_id="session", run_id="run",
        turn_id="turn", actor_id="pipeline")
    channel.bind(observer)
    observer.bind_approval_channel(channel)
    observer.start("demo", 1)
    observer.stage_started("build", 1, 1)
    task = Task(prompt="work", cwd="/tmp/project")
    scope = {"workspace_id": "/tmp/project", "session_id": "session",
             "run_id": "run", "turn_id": "turn", "actor_id": "pipeline"}

    def fake_parallel(tasks, _workers, **kwargs):
        actor_id = observer.task_id("build", 1, tasks[0])
        assert channel.request_actor_cancel(scope, actor_id=actor_id)
        assert kwargs["cancel_event"].for_task(tasks[0]).is_set()
        return [SimpleNamespace(task=tasks[0], success=False, returncode=-1,
                                reason="cancelled", validation_ok=False,
                                validation_exit=-1)]

    results, outputs = execute_stage_tasks(
        Stage(name="build", mode="parallel"), [task], [], "", "new", "",
        "stage-session", 0, 0, 1.0, None, observer,
        lambda *args, **kwargs: ([], []), fake_parallel,
        lambda *args, **kwargs: [])

    assert outputs == []
    assert len(results) == 1 and results[0].reason == "cancelled"
    assert channel.request_actor_cancel(scope, actor_id=observer.task_id(
        "build", 1, task)) is False


def test_native_pipeline_actor_cancel_requires_exact_scope_and_live_task():
    events = []
    channel = NativePipelineControlChannel()
    observer = NativePipelineObserver(
        events.append, "/tmp/project", session_id="session", run_id="run",
        turn_id="turn", actor_id="pipeline")
    channel.bind(observer)
    observer.bind_approval_channel(channel)
    observer.start("demo", 1)
    observer.stage_started("build", 1, 1)
    task = SimpleNamespace(effective_agent=lambda: "pi", output="out.md")
    actor_id = observer.task_started("build", 1, 1, task)
    assert channel.register_task(actor_id, task) is True
    scope = {"workspace_id": "/tmp/project", "session_id": "session",
             "run_id": "run", "turn_id": "turn", "actor_id": "pipeline"}
    assert channel.request_actor_cancel(dict(scope, workspace_id="/tmp/other"),
                                        actor_id=actor_id) is False
    assert channel.request_actor_cancel(scope, actor_id="missing") is False
    assert channel.request_actor_cancel(scope, actor_id=actor_id) is True
    assert channel.cancel_event_for_task(task).is_set() is True
    assert any(event.kind == "actor.status" and event.actor_id == actor_id
               for event in events)
    channel.unregister_task(actor_id, task)
    assert channel.request_actor_cancel(scope, actor_id=actor_id) is False


def test_native_pipeline_control_requires_exact_scope_and_interrupts():
    events = []
    channel = NativePipelineControlChannel()
    observer = NativePipelineObserver(
        events.append, "/tmp/project", session_id="session", run_id="run",
        turn_id="turn", actor_id="pipeline")
    channel.bind(observer)
    observer.bind_approval_channel(channel)
    observer.start("demo", 1)

    assert channel.request_cancel({
        "workspace_id": "/tmp/other", "session_id": "session",
        "run_id": "run", "turn_id": "turn", "actor_id": "pipeline",
    }) is False
    assert channel.request_cancel({
        "workspace_id": "/tmp/project", "session_id": "session",
        "run_id": "run", "turn_id": "turn", "actor_id": "pipeline",
    }) is True
    assert channel.cancelled is True
    assert [item.kind for item in events if item.kind == "turn.interrupting"] == [
        "turn.interrupting"]


def test_native_pipeline_cancel_finishes_as_interrupted(tmp_path, monkeypatch):
    events = []
    channel = NativePipelineControlChannel()
    outcome = {}
    agent = tmp_path / "blocking-agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        "trap 'exit 130' TERM INT\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    pipeline = Pipeline(stages=[Stage(
        name="build", from_prompt="work", output=str(tmp_path / "out.md"),
        cwd=str(tmp_path))], name="demo")

    def run():
        outcome["value"] = run_pipeline(
            pipeline, event_sink=events.append, approval_channel=channel)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 2
    while not any(item.kind == "stage.started" for item in events):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    started = next(item for item in events if item.kind == "pipeline.started")
    assert channel.request_cancel({
        "workspace_id": started.workspace_id, "session_id": started.session_id,
        "run_id": started.run_id, "turn_id": started.turn_id,
        "actor_id": started.actor_id,
    }) is True
    worker.join(2)

    assert not worker.is_alive()
    results, failed = outcome["value"]
    assert failed == ["build"]
    assert len(results) == 1 and results[0].reason == "cancelled"
    assert [item.kind for item in events[-2:]] == [
        "pipeline.failed", "turn.interrupted"]


def test_native_pipeline_cancel_kills_active_task_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "agent.pid"
    agent = tmp_path / "agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        f"echo $$ > '{pidfile}'\n"
        "while true; do sleep 30; done\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    events = []
    channel = NativePipelineControlChannel()
    outcome = {}
    pipeline = Pipeline(stages=[Stage(
        name="build", from_prompt="work", output=str(tmp_path / "out.md"),
        cwd=str(tmp_path))], name="demo")

    def run():
        outcome["value"] = run_pipeline(
            pipeline, event_sink=events.append, approval_channel=channel)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 2
    while not any(item.kind == "task.started" for item in events):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    while not pidfile.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    started = next(item for item in events if item.kind == "pipeline.started")
    assert channel.request_cancel({
        "workspace_id": started.workspace_id, "session_id": started.session_id,
        "run_id": started.run_id, "turn_id": started.turn_id,
        "actor_id": started.actor_id,
    }) is True
    worker.join(2)

    assert not worker.is_alive()
    assert outcome["value"][1] == ["build"]
    assert not (tmp_path / "out.md").exists()
    assert [item.kind for item in events[-2:]] == [
        "pipeline.failed", "turn.interrupted"]
    child_pid = int(pidfile.read_text(encoding="utf-8").strip())
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        return
    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
    assert state == "Z"


def test_native_observer_marks_cancelled_task_and_stage_as_interrupted():
    events = []
    observer = NativePipelineObserver(events.append, "/tmp/project")
    observer.start("demo", 1)
    observer.stage_started("build", 1, 1)
    task = SimpleNamespace(effective_agent=lambda: "pi", output="out.md")
    item_id = observer.task_started("build", 1, 1, task)
    result = SimpleNamespace(
        success=False, returncode=-1, reason="cancelled",
        validation_ok=False, validation_exit=-1)
    observer.task_completed(item_id, task, result, stage="build")
    observer.validation_completed(item_id, task, result, stage="build")
    observer.stage_completed("build", 1, 1, False, cancelled=True)

    task_event = next(event for event in events if event.kind == "task.completed")
    task_actor = next(event for event in events
                      if event.kind == "actor.completed"
                      and event.actor_id == task_event.actor_id)
    stage_event = next(event for event in events if event.kind == "stage.completed")
    stage_actor = next(event for event in events
                       if event.kind == "actor.completed"
                       and event.actor_id == stage_event.actor_id)
    assert task_event.payload["cancelled"] is True
    assert task_event.payload["status"] == "CANCELLED"
    assert task_actor.payload["status"] == "CANCELLED"
    assert stage_event.payload["cancelled"] is True
    assert stage_actor.payload["status"] == "CANCELLED"


def test_native_observer_exposes_meta_role_child_lineage():
    events = []
    observer = NativePipelineObserver(events.append, "/tmp/project")
    observer.start("demo", 1)
    observer.stage_started("review", 1, 1)
    actor_id = observer.meta_role_started(
        "review", 1, "security_engineer", 1, "pi")
    observer.meta_role_completed(actor_id, "review", True)
    observer.stage_completed("review", 1, 1, True)
    observer.finish(True)

    spawned = next(event for event in events
                   if event.kind == "actor.spawned"
                   and event.actor_id == actor_id)
    completed = next(event for event in events
                     if event.kind == "actor.completed"
                     and event.actor_id == actor_id)
    assert spawned.payload["parent_actor_id"] == "stage_1_review"
    assert completed.payload["parent_actor_id"] == "stage_1_review"


def test_native_observer_sink_failure_disables_observation_without_raising():
    calls = []

    def broken(_event):
        calls.append(1)
        raise RuntimeError("journal unavailable")

    observer = NativePipelineObserver(broken, "/tmp/project")
    observer.start("demo", 0)
    observer.stage_started("never", 1, 1)

    assert calls == [1]
    assert not observer.enabled
    assert "journal unavailable" in observer.disabled_reason
    assert observer.events_emitted == 0
