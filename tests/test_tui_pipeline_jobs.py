"""Crash-safe TUI pipeline job state contracts."""

from __future__ import annotations

import hashlib

import pytest

from pbatch.tui_pipeline_jobs import (
    PipelineJobBusy, PipelineJobStoreError, TuiPipelineJobStore,
    pipeline_fingerprint,
)


def _handoff(output="reports/final.md"):
    return {
        "outcome": "completed", "failed_stages": [], "error": "",
        "tasks": [{
            "index": 1, "stage": "gate", "role": "",
            "success": True, "output": output, "reason": "",
        }],
        "omitted_task_results": 0, "inline": "",
    }


def test_pipeline_job_store_lifecycle_and_restart_handoff(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    first = TuiPipelineJobStore(workspace, owner_id="a" * 32)
    job = first.create("pipelines/build.yaml", "b" * 64, "controller")
    job = first.claim_run(job["job_id"])
    assert job["state"] == "running" and job["attempts"] == 1
    job = first.save_handoff(job["job_id"], _handoff())
    assert job["state"] == "handoff_pending"
    assert first.active()["handoff"]["tasks"][0]["output"] == "reports/final.md"

    resumed = TuiPipelineJobStore(workspace, owner_id="c" * 32)
    assert resumed.owned_by_other_live_process(job)
    with pytest.raises(PipelineJobBusy):
        resumed.claim_handoff(job["job_id"])

    monkeypatch.setattr("pbatch.tui_pipeline_jobs._pid_alive", lambda _pid: False)
    job = resumed.claim_handoff(job["job_id"])
    assert job["owner_id"] == "c" * 32
    job = resumed.finish(job["job_id"], "completed")
    assert job["state"] == "completed" and resumed.active() is None


def test_pipeline_job_store_rejects_conflicting_or_unsafe_records(
        tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create("flow.yaml", "d" * 64, "tui")
    with pytest.raises(PipelineJobBusy):
        store.create("other.yaml", "e" * 64, "tui")

    store.state_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    store.state_path.symlink_to(outside)
    with pytest.raises(PipelineJobStoreError):
        store.read()


def test_pipeline_relaunch_attempts_have_a_deterministic_budget(
        tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create("flow.yaml", "f" * 64, "tui")
    for _ in range(8):
        job = store.claim_run(job["job_id"])
        job = store.requeue(job["job_id"])
    with pytest.raises(PipelineJobStoreError, match="attempt budget"):
        store.claim_run(job["job_id"])


def test_orphaned_exhausted_job_becomes_reviewable_failure(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create("flow.yaml", "6" * 64, "controller")
    for _ in range(8):
        job = store.claim_run(job["job_id"])
        job = store.requeue(job["job_id"], retry_after=0)
    monkeypatch.setattr("pbatch.tui_pipeline_jobs._pid_alive", lambda _pid: False)

    failed = store.fail_orphaned_exhausted(job["job_id"])

    assert failed["state"] == "handoff_pending"
    assert "budget exhausted" in failed["handoff"]["error"]


def test_abandoned_worker_cancellation_is_terminalized_safely(
        tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create("flow.yaml", "5" * 64, "tui")
    job = store.claim_run(job["job_id"])
    store.request_cancel(job["job_id"])
    monkeypatch.setattr("pbatch.tui_pipeline_jobs._pid_alive", lambda _pid: False)

    result = store.finish_abandoned_cancel(job["job_id"])

    assert result["state"] == "cancelled"
    assert result["owner_pid"] == result["worker_pid"] == 0


def test_deferred_executor_keeps_supervisor_lease_visible(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner_id = "4" * 32
    store = TuiPipelineJobStore(workspace, owner_id=owner_id)
    job = store.create("flow.yaml", "7" * 64, "controller")
    store.claim_run(job["job_id"])

    supervisor = TuiPipelineJobStore(workspace, owner_id=owner_id)
    supervisor.owner_pid = 500
    supervisor.adopt_supervisor(job["job_id"])
    worker_store = TuiPipelineJobStore(workspace, owner_id=owner_id)
    worker_store.owner_pid = 600
    worker_store.adopt_run(job["job_id"])
    deferred = worker_store.requeue_supervised(
        job["job_id"], retry_after=123, progress="workspace lock busy")
    another_tui = TuiPipelineJobStore(workspace, owner_id="5" * 32)
    monkeypatch.setattr(
        "pbatch.tui_pipeline_jobs._pid_alive", lambda pid: pid == 500)

    assert deferred["state"] == "queued"
    assert deferred["owner_pid"] == deferred["worker_pid"] == 500
    assert another_tui.owned_by_other_live_process(deferred)


def test_retry_claim_cannot_clear_a_racing_cancellation(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create("flow.yaml", "8" * 64, "controller")
    job = store.claim_run(job["job_id"])
    job = store.requeue(job["job_id"])
    store.request_cancel(job["job_id"])

    with pytest.raises(PipelineJobBusy, match="cancellation"):
        store.claim_run(job["job_id"])
    assert store.read()["cancel_requested"]


def test_worker_lease_transfers_to_spawned_process(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    parent = TuiPipelineJobStore(workspace, owner_id="7" * 32)
    job = parent.create("flow.yaml", "6" * 64, "tui")
    job = parent.claim_run(job["job_id"])
    worker_pid = 98_765
    parent.record_worker_pid(job["job_id"], worker_pid)
    monkeypatch.setattr("pbatch.tui_pipeline_jobs.os.getpid", lambda: worker_pid)
    worker = TuiPipelineJobStore(workspace, owner_id=parent.owner_id)

    adopted = worker.adopt_run(job["job_id"])

    assert adopted["owner_pid"] == worker_pid
    assert adopted["worker_pid"] == worker_pid


def test_job_cancel_and_deferred_retry_transitions(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create("flow.yaml", "1" * 64, "tui")
    store.request_cancel(job["job_id"])
    job = store.finish_queued_cancel(job["job_id"])
    assert job["state"] == "cancelled"

    job = store.create("flow.yaml", "2" * 64, "tui")
    job = store.claim_run(job["job_id"])
    job = store.requeue(
        job["job_id"], retry_after=2**63 - 1, progress="deferred")
    with pytest.raises(PipelineJobBusy, match="deferred"):
        store.claim_run(job["job_id"])


def test_pipeline_fingerprint_is_bounded_and_exact(tmp_path):
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    assert pipeline_fingerprint(pipeline) == hashlib.sha256(
        b"stages: []\n").hexdigest()
