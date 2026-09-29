"""Detached TUI pipeline worker contracts."""

from __future__ import annotations

import time
from types import SimpleNamespace

from pbatch.tui_pipeline_jobs import TuiPipelineJobStore, pipeline_fingerprint
import pbatch.tui_pipeline as pipeline_tui
import pbatch.tui_pipeline_worker as worker


def test_spawn_uses_detached_argument_vector_and_workspace_runtime(
        tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    calls = []

    def spawn(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(pipeline_tui.subprocess, "Popen", spawn)
    process = pipeline_tui._spawn_pipeline_worker(
        workspace, "a" * 32, "b" * 32)

    args, kwargs = calls[0]
    assert process.pid == 123
    assert args[1:3] == ["-m", "pbatch.tui_pipeline_worker"]
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(workspace)
    assert kwargs["env"]["PBATCH_DATA_DIR"] == str(tmp_path / "runtime")
    assert "shell" not in kwargs


def test_detached_supervisor_completes_real_pipeline(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline_file = workspace / "flow.yaml"
    pipeline_file.write_text(
        "stages:\n  - name: noop\n    from_prompt: hello\n    output: out.md\n",
        encoding="utf-8")
    agent = workspace / "fake-agent.sh"
    agent.write_text('#!/bin/sh\necho "$2"\n', encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    store = TuiPipelineJobStore(workspace)
    job = store.create(
        "flow.yaml", pipeline_fingerprint(pipeline_file), "controller",
        execution={"agent": str(agent), "provider": "", "timeout": 30,
                   "memory_mode": "off"})
    store.claim_run(job["job_id"])

    process = pipeline_tui._spawn_pipeline_worker(
        workspace, job["job_id"], store.owner_id)
    store.record_worker_pid(job["job_id"], process.pid)
    deadline = time.monotonic() + 15
    try:
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    result = store.read()
    assert result["state"] == "handoff_pending"
    assert result["handoff"]["outcome"] == "completed"
    assert (workspace / "out.md").read_text(encoding="utf-8") == "hello\n"


def test_supervisor_retries_deferred_job_without_tui(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner_id = "7" * 32
    store = TuiPipelineJobStore(workspace, owner_id=owner_id)
    job = store.create("flow.yaml", "2" * 64, "controller")
    store.claim_run(job["job_id"])
    launches = 0

    class ExitedChild:
        deferred = False

        def poll(self):
            if launches == 1 and not self.deferred:
                self.deferred = True
                store.requeue(job["job_id"], retry_after=0,
                              progress="workspace lock busy")
            return 0

    def spawn(*_args):
        nonlocal launches
        launches += 1
        if launches == 2:
            store.save_handoff(job["job_id"], {
                "outcome": "completed", "failed_stages": [], "error": "",
                "tasks": [], "omitted_task_results": 0, "inline": "",
            })
            store.release_handoff(job["job_id"])
        return ExitedChild()

    monkeypatch.setattr(worker, "_spawn_executor_or_defer", spawn)

    assert worker._supervise(str(workspace), job["job_id"], owner_id) == 0
    result = store.read()
    assert launches == 2
    assert result["attempts"] == 2
    assert result["state"] == "handoff_pending"
    assert result["handoff"]["outcome"] == "completed"


def test_supervisor_caps_crash_restarts_and_persists_failure(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner_id = "8" * 32
    store = TuiPipelineJobStore(workspace, owner_id=owner_id)
    job = store.create("flow.yaml", "3" * 64, "controller")
    store.claim_run(job["job_id"])

    class ExitedChild:
        def poll(self):
            return 1

    monkeypatch.setattr(
        worker, "_spawn_executor_or_defer",
        lambda *_args: ExitedChild())

    assert worker._supervise(str(workspace), job["job_id"], owner_id) == 0
    result = store.read()
    assert result["state"] == "handoff_pending"
    assert result["attempts"] == 8
    assert "budget exhausted" in result["handoff"]["error"]


def test_worker_adopts_job_runs_and_persists_handoff(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline_file = workspace / "flow.yaml"
    pipeline_file.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner_id = "f" * 32
    store = TuiPipelineJobStore(workspace, owner_id=owner_id)
    job = store.create(
        "flow.yaml", pipeline_fingerprint(pipeline_file), "controller",
        execution={"agent": "pi", "provider": "", "timeout": 0,
                   "memory_mode": "auto"})
    store.claim_run(job["job_id"])
    monkeypatch.setattr(worker, "load_pipeline", lambda _path: SimpleNamespace(stages=[]))
    monkeypatch.setattr(worker, "_prepare_pipeline", lambda *_args: None)
    monkeypatch.setattr(worker, "acquire_lock", lambda *_args: None)
    monkeypatch.setattr(worker, "release_lock", lambda *_args: None)
    output = SimpleNamespace(
        task=None, success=True, stdout="worker-only stdout", reason="")
    monkeypatch.setattr(
        worker, "run_pipeline", lambda *_args, **_kwargs: ([output], []))

    assert worker._execute(str(workspace), job["job_id"], owner_id) == 0
    result = store.read()
    assert result["state"] == "handoff_pending"
    assert result["owner_pid"] == 0
    assert result["handoff"]["outcome"] == "completed"
    assert "worker-only stdout" in result["handoff"]["inline"]


def test_worker_rejects_interactive_approval_stage(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline_file = workspace / "flow.yaml"
    pipeline_file.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner_id = "a" * 32
    store = TuiPipelineJobStore(workspace, owner_id=owner_id)
    job = store.create(
        "flow.yaml", pipeline_fingerprint(pipeline_file), "controller",
        execution={"agent": "pi", "provider": "", "timeout": 0,
                   "memory_mode": "auto"})
    store.claim_run(job["job_id"])
    pipeline = SimpleNamespace(stages=[SimpleNamespace(approval=True)])
    monkeypatch.setattr(worker, "load_pipeline", lambda _path: pipeline)
    monkeypatch.setattr(worker, "_prepare_pipeline", lambda *_args: None)

    assert worker._execute(str(workspace), job["job_id"], owner_id) == 0
    result = store.read()
    assert result["state"] == "handoff_pending"
    assert result["handoff"]["outcome"] == "failed"
    assert "interactive approval" in result["handoff"]["error"]
