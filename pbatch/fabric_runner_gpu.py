"""Runner-side GPU preflight and terminal evidence helpers."""

from __future__ import annotations

import uuid
from typing import Optional

from .config import log
from .fabric import (FABRIC_VERSION, ExecutionEvidence, _digest,
                     _environment_digest)
from .fabric_gpu import (normalize_gpu_assignment, verify_gpu_assignment)
from .fabric_outbox import EvidenceOutbox


def preflight_gpu(resources: dict, task: dict, probe) -> tuple:
    """Verify a durable assignment against a fresh local GPU measurement."""
    count = resources.get("gpu_count", 0) if isinstance(resources, dict) else 0
    if not count:
        return None, ""
    assignment = task.get("gpu_assignment")
    memory = resources.get("gpu_memory_bytes", 0)
    error = verify_gpu_assignment(assignment, probe(), count, memory)
    if error:
        return assignment, error
    return normalize_gpu_assignment(assignment, count, memory), ""


def report_cancel_if_requested(client, task: dict,
                               outbox: Optional[EvidenceOutbox], submit) -> dict | None:
    """Commit cancellation only when the control plane confirms the request."""
    status = getattr(client, "task_status", None)
    if not callable(status):
        return None
    try:
        result = status(str(task.get("task_id", "")))
    except Exception:
        return None
    remote = result.get("task") if isinstance(result, dict) else None
    if not isinstance(remote, dict) or remote.get("cancel_requested") is not True:
        return None
    return submit(client, task, _cancelled_before_spawn_evidence(task), outbox)


def _cancelled_before_spawn_evidence(task: dict) -> dict:
    """Create explicit terminal evidence for cancellation before process spawn."""
    message = "task cancellation requested before process spawn"
    evidence = ExecutionEvidence(
        attempt_id=f"cancelled-{uuid.uuid4().hex[:16]}",
        target_id="local", label=f"task:{task.get('task_id', '')}",
        result="inconclusive", exit_code=-1, timed_out=False,
        output_overflow=False, stdout="", stderr=message, code_digest="",
        environment_digest=_environment_digest(), input_digest=_digest("[]"),
        output_digest=_digest(message), runner_version=FABRIC_VERSION,
        elapsed=0.0, cancelled=True,
        gpu_assignment=(task.get("gpu_assignment")
                        if isinstance(task.get("gpu_assignment"), dict)
                        else None))
    return evidence.to_dict()


def report_gpu_preflight_failure(client, task: dict, assignment, reason: str,
                                 outbox: Optional[EvidenceOutbox], submit) -> dict:
    """Record a GPU mismatch as failed evidence without spawning the task."""
    stderr = str(reason)[:4096]
    evidence = ExecutionEvidence(
        attempt_id=f"gpu-preflight-{uuid.uuid4().hex[:16]}",
        target_id="local", label=f"task:{task.get('task_id', '')}",
        result="failed", exit_code=-1, timed_out=False,
        output_overflow=False, stdout="", stderr=stderr, code_digest="",
        environment_digest=_environment_digest(),
        input_digest=_digest("[]"), output_digest=_digest(stderr),
        runner_version=FABRIC_VERSION, elapsed=0.0,
        gpu_assignment=assignment if isinstance(assignment, dict) else None)
    result = submit(client, task, evidence.to_dict(), outbox)
    if not result.get("accepted"):
        log.warning("GPU任务 %s 预检失败证据未确认：%s",
                    task.get("task_id", "?"),
                    result.get("reason", result.get("error", "?")))
    return result
