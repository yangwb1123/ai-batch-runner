"""Bounded worker-result return to the tool-free TUI controller."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pbatch.models import Task, TaskResult
import pbatch.tui_pipeline as pipeline_module
from pbatch.tui_pipeline import (
    TUIPipelineMixin, _controller_fingerprint, _pipeline_execution_settings,
    _pipeline_handoff_prompt,
)
from pbatch.tui_pipeline_jobs import TuiPipelineJobStore, pipeline_fingerprint


class _HandoffHarness(TUIPipelineMixin):
    def __init__(self, cwd: Path):
        self.options = SimpleNamespace(
            orchestrator_only=True, cwd=str(cwd), session_name="tui",
            model="", provider="", agent="", timeout=0,
            memory_mode="auto", persistent_pipelines=False)
        self._backend = SimpleNamespace(cwd=str(cwd))
        self._orchestrator_only_ready = True
        self._native_pipeline_approval = object()
        self._worker = object()
        self.pending = []
        self.entries = []
        self.prompts = []
        self.thread_calls = []

    def _append_entry(self, kind, text):
        self.entries.append((kind, text))

    def _set_busy(self, value):
        self.busy = value

    def _mark_agent_operation_finished(self):
        return None

    def _refresh_completion_inspector(self):
        return None

    def _refresh_status(self):
        return None

    def _session_control_busy(self):
        return False

    def call_after_refresh(self, callback, *args):
        self.pending.append((callback, args))

    def _start_turn(self, prompt):
        self.prompts.append(prompt)
        return True

    def call_from_thread(self, callback, *args):
        self.thread_calls.append((callback, args))


def test_controller_model_override_does_not_replace_worker_routing(
        tmp_path, monkeypatch):
    owner = _HandoffHarness(tmp_path)
    owner.options.model = "controller-model"
    owner._option_value = lambda _name: ""
    calls = []
    monkeypatch.setattr(pipeline_module, "acquire_lock", lambda _cwd: "lock")
    monkeypatch.setattr(pipeline_module, "release_lock", lambda _path: None)
    monkeypatch.setattr(
        pipeline_module, "run_pipeline",
        lambda _pipeline, **kwargs: calls.append(kwargs) or ([], []))
    pipeline = SimpleNamespace(stages=[SimpleNamespace(cwd=str(tmp_path))])

    owner._run_pipeline_worker(
        pipeline, object(), False, SimpleNamespace(cancelled=False))

    assert calls[0]["model_override"] == ""
    assert owner.thread_calls


def test_pipeline_handoff_returns_artifact_to_controller_session(tmp_path):
    artifact = tmp_path / "worker.md"
    artifact.write_text("Implementation evidence: unit tests passed.\n", encoding="utf-8")
    task = Task(prompt="implement", output="worker.md", cwd=str(tmp_path),
                memory={"stage": "implement", "role": "backend_engineer"})
    result = TaskResult(task=task, success=True)
    owner = _HandoffHarness(tmp_path)

    owner._finish_pipeline([result], [], None)
    callback, args = owner.pending.pop()
    callback(*args)

    assert len(owner.prompts) == 1
    prompt = owner.prompts[0]
    assert "controller/reviewer only" in prompt
    assert "backend_engineer" in prompt
    assert "unit tests passed" in prompt
    assert "untrusted evidence" in prompt
    assert owner.busy is False


def test_handoff_excludes_symlinked_or_out_of_workspace_artifacts(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-secret.md"
    outside.write_text("must not be injected", encoding="utf-8")
    link = tmp_path / "worker.md"
    link.symlink_to(outside)
    task = Task(prompt="review", output="worker.md", cwd=str(tmp_path))
    prompt = _pipeline_handoff_prompt(
        [TaskResult(task=task, success=True)], [], None, tmp_path.resolve())

    assert "must not be injected" not in prompt
    assert '"output":""' in prompt


def test_interrupted_pipeline_is_relaunched_with_reuse(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline = workspace / "build.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner = _HandoffHarness(workspace)
    previous = TuiPipelineJobStore(workspace, owner_id="a" * 32)
    job = previous.create(
        "build.yaml", pipeline_fingerprint(pipeline), "tui",
        _controller_fingerprint(owner),
        execution=_pipeline_execution_settings(owner))
    previous.claim_run(job["job_id"])
    monkeypatch.setattr("pbatch.tui_pipeline_jobs._pid_alive", lambda _pid: False)
    calls = []
    owner._start_pipeline_path = lambda *args, **kwargs: calls.append((args, kwargs))

    owner._recover_pipeline_job_or_start()

    assert calls[0][0][0] == "build.yaml"
    assert calls[0][1]["reuse"] is True
    assert calls[0][1]["resume_job"]["job_id"] == job["job_id"]


def test_pending_handoff_resumes_without_persisted_artifact_bodies(
        tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline = workspace / "build.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner = _HandoffHarness(workspace)
    previous = TuiPipelineJobStore(workspace, owner_id="a" * 32)
    job = previous.create(
        "build.yaml", pipeline_fingerprint(pipeline), "tui",
        _controller_fingerprint(owner),
        execution=_pipeline_execution_settings(owner))
    job = previous.claim_run(job["job_id"])
    previous.save_handoff(job["job_id"], {
        "outcome": "completed", "failed_stages": [], "error": "",
        "tasks": [{"index": 1, "stage": "gate", "role": "",
                   "success": True, "output": "reports/final.md",
                   "reason": ""}],
        "omitted_task_results": 0, "inline": "",
    })
    monkeypatch.setattr("pbatch.tui_pipeline_jobs._pid_alive", lambda _pid: False)

    owner._recover_pipeline_job_or_start()
    callback, args = owner.pending.pop()
    callback(*args)

    assert len(owner.prompts) == 1
    assert "reports/final.md" in owner.prompts[0]
    assert "handoff_pending" not in owner.prompts[0]
    assert owner._pipeline_handoff_inflight is True


def test_live_persistent_monitor_returns_pending_worker_result(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline = workspace / "build.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner = _HandoffHarness(workspace)
    owner.options.persistent_pipelines = True
    owner._busy = False
    store = TuiPipelineJobStore(workspace, owner_id="d" * 32)
    job = store.create(
        "build.yaml", pipeline_fingerprint(pipeline), "tui",
        _controller_fingerprint(owner),
        execution=_pipeline_execution_settings(owner))
    job = store.claim_run(job["job_id"])
    store.save_handoff(job["job_id"], {
        "outcome": "completed", "failed_stages": [], "error": "",
        "tasks": [], "omitted_task_results": 0, "inline": "worker stdout",
    })
    store.release_handoff(job["job_id"])
    owner._tui_pipeline_job_store = store

    owner._poll_persistent_pipeline_job()
    callback, args = owner.pending.pop()
    callback(*args)

    assert len(owner.prompts) == 1
    assert "worker stdout" in owner.prompts[0]
    assert store.read()["owner_id"] == store.owner_id


def test_pipeline_handoff_persists_references_not_worker_content(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    pipeline = workspace / "build.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    artifact = workspace / "reports" / "final.md"
    artifact.parent.mkdir()
    artifact.write_text("private worker evidence", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owner = _HandoffHarness(workspace)
    store = TuiPipelineJobStore(workspace, owner_id="e" * 32)
    job = store.create(
        "build.yaml", pipeline_fingerprint(pipeline), "tui",
        _controller_fingerprint(owner),
        execution=_pipeline_execution_settings(owner))
    job = store.claim_run(job["job_id"])
    owner._tui_pipeline_job_store = store
    owner._tui_pipeline_job = job
    result = TaskResult(Task(
        prompt="review", output="reports/final.md", cwd=str(workspace)), True)

    prompt = owner._persist_pipeline_outcome([result], [], None, False)
    owner._send_pipeline_handoff(prompt)
    owner._complete_pipeline_handoff_turn({"success": True}, None)

    assert "private worker evidence" in prompt
    assert "private worker evidence" not in store.state_path.read_text(encoding="utf-8")
    assert store.read()["state"] == "completed"


def test_pipeline_failure_is_returned_for_controller_review(tmp_path):
    prompt = _pipeline_handoff_prompt(
        [], ["implementation"], RuntimeError("worker failed"), tmp_path.resolve())

    assert '"outcome":"failed"' in prompt
    assert "implementation" in prompt
    assert "worker failed" in prompt
