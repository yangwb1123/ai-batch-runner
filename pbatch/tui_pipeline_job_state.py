"""Schema validation and bounded private I/O for TUI pipeline job records."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import time
from typing import Any

from . import config

SCHEMA_VERSION = 3
MAX_STATE_BYTES = 64 * 1024
MAX_HANDOFF_TASKS = 32
MAX_RUN_ATTEMPTS = 8
_ACTIVE_STATES = frozenset({"queued", "running", "handoff_pending"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_STATES = _ACTIVE_STATES | _TERMINAL_STATES
_HEX = frozenset("0123456789abcdef")
_JOB_FIELDS = frozenset({
    "job_id", "pipeline", "fingerprint", "controller_fingerprint",
    "session_name", "state", "attempts", "owner_pid", "worker_pid", "owner_id",
    "created_at", "updated_at", "handoff", "reuse", "cancel_requested",
    "progress", "retry_after", "execution",
})
_HANDOFF_FIELDS = frozenset({
    "outcome", "failed_stages", "error", "tasks", "omitted_task_results",
    "inline",
})
_TASK_FIELDS = frozenset({
    "index", "stage", "role", "success", "output", "reason",
})
_EXECUTION_FIELDS = frozenset({"agent", "provider", "timeout", "memory_mode"})


class PipelineJobStoreError(ValueError):
    """The persisted job state is unsafe, inconsistent, or unavailable."""


def pipeline_fingerprint(path: Path) -> str:
    """Hash one bounded pipeline definition for exact restart matching."""
    try:
        descriptor = _open_private(path, os.O_RDONLY)
        try:
            info = os.fstat(descriptor)
            if info.st_size > config.INPUT_MAX_BYTES:
                raise PipelineJobStoreError("pipeline definition exceeds input limit")
            raw = _read_bounded(descriptor, config.INPUT_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PipelineJobStoreError(f"cannot fingerprint pipeline file: {exc}") from exc
    if len(raw) > config.INPUT_MAX_BYTES:
        raise PipelineJobStoreError("pipeline definition exceeds input limit")
    return hashlib.sha256(raw).hexdigest()


def _validate_state(state: Any, workspace: Path) -> None:
    if (not isinstance(state, dict)
            or set(state) != {"schema_version", "workspace", "job"}
            or state["schema_version"] != SCHEMA_VERSION
            or state["workspace"] != str(workspace)):
        raise PipelineJobStoreError("pipeline job state scope/schema mismatch")
    if state["job"] is not None:
        _validate_job(state["job"])


def _validate_job(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _JOB_FIELDS:
        raise PipelineJobStoreError("pipeline job record schema mismatch")
    if not _valid_job_id(value["job_id"]):
        raise PipelineJobStoreError("pipeline job id is invalid")
    _relative_path(value["pipeline"], "pipeline")
    _digest(value["fingerprint"], "pipeline fingerprint")
    _digest(value["controller_fingerprint"], "controller fingerprint")
    _bounded_text(value["session_name"], "session_name", 160)
    if value["state"] not in _STATES:
        raise PipelineJobStoreError("pipeline job state is invalid")
    _bounded_int(value["attempts"], "attempts", 0, MAX_RUN_ATTEMPTS)
    owner_pid = _bounded_int(value["owner_pid"], "owner_pid", 0, 2**31 - 1)
    worker_pid = _bounded_int(value["worker_pid"], "worker_pid", 0, 2**31 - 1)
    owner_id = _bounded_text(value["owner_id"], "owner_id", 64, allow_empty=True)
    _validate_leases(value["state"], owner_pid, worker_pid, owner_id)
    _bounded_int(value["created_at"], "created_at", 0, 2**63 - 1)
    _bounded_int(value["updated_at"], "updated_at", 0, 2**63 - 1)
    _bounded_int(value["retry_after"], "retry_after", 0, 2**63 - 1)
    if not isinstance(value["reuse"], bool) or not isinstance(
            value["cancel_requested"], bool):
        raise PipelineJobStoreError("pipeline job flags are invalid")
    _bounded_text(value["progress"], "progress", 256)
    _execution(value["execution"])
    if value["handoff"] is not None:
        _handoff(value["handoff"])


def _validate_leases(state: str, owner_pid: int, worker_pid: int,
                     owner_id: str) -> None:
    if bool(owner_pid) != bool(owner_id):
        raise PipelineJobStoreError("pipeline job owner fields are inconsistent")
    if state == "running" and not owner_pid:
        raise PipelineJobStoreError("running pipeline has no owner")
    if state == "queued" and (
            bool(owner_pid) != bool(worker_pid)
            or (worker_pid and worker_pid != owner_pid)):
        raise PipelineJobStoreError("queued supervisor lease is inconsistent")
    if worker_pid and (state not in {"running", "queued", "handoff_pending"}
                       or not owner_pid):
        raise PipelineJobStoreError("pipeline worker lease fields are inconsistent")
    if state in {"completed", "failed", "cancelled"} and (
            owner_pid or worker_pid or owner_id):
        raise PipelineJobStoreError("terminal pipeline still has an owner")


def _execution(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EXECUTION_FIELDS:
        raise PipelineJobStoreError("pipeline execution settings are invalid")
    agent = _bounded_text(value["agent"], "execution agent", 4096,
                          allow_empty=True)
    provider = _bounded_text(value["provider"], "execution provider", 256,
                              allow_empty=True)
    timeout = _bounded_int(value["timeout"], "execution timeout", 0, 2**31 - 1)
    memory = value["memory_mode"]
    if not isinstance(memory, str) or memory not in {"auto", "on", "off"}:
        raise PipelineJobStoreError("pipeline execution memory mode is invalid")
    return {"agent": agent, "provider": provider,
            "timeout": timeout, "memory_mode": memory}


def _handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _HANDOFF_FIELDS:
        raise PipelineJobStoreError("pipeline handoff record schema mismatch")
    if value["outcome"] not in {"completed", "failed"}:
        raise PipelineJobStoreError("pipeline handoff outcome is invalid")
    failed = value["failed_stages"]
    if not isinstance(failed, list) or len(failed) > 16:
        raise PipelineJobStoreError("pipeline handoff failures are invalid")
    normalized_failed = [_bounded_text(item, "failed stage", 160)
                         for item in failed]
    error = _bounded_text(value["error"], "handoff error", 512, allow_empty=True)
    tasks = value["tasks"]
    if not isinstance(tasks, list) or len(tasks) > MAX_HANDOFF_TASKS:
        raise PipelineJobStoreError("pipeline handoff tasks are invalid")
    normalized_tasks = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
            raise PipelineJobStoreError("pipeline handoff task schema mismatch")
        _bounded_int(task["index"], "task index", 1, 1_000_000)
        if not isinstance(task["success"], bool):
            raise PipelineJobStoreError("pipeline task result is invalid")
        normalized_tasks.append({
            "index": task["index"],
            "stage": _bounded_text(task["stage"], "stage", 128, allow_empty=True),
            "role": _bounded_text(task["role"], "role", 128, allow_empty=True),
            "success": task["success"],
            "output": _relative_path(task["output"], "artifact", allow_empty=True),
            "reason": _bounded_text(task["reason"], "reason", 256, allow_empty=True),
        })
    omitted = _bounded_int(value["omitted_task_results"], "omitted count", 0, 1_000_000)
    inline = _bounded_text(value["inline"], "inline worker output", 8192,
                           allow_empty=True)
    return {
        "outcome": value["outcome"], "failed_stages": normalized_failed,
        "error": error, "tasks": normalized_tasks,
        "omitted_task_results": omitted, "inline": inline,
    }


def _relative_path(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    text = _bounded_text(value, label, 4096)
    path = PurePosixPath(text)
    if (path.is_absolute() or "\\" in text
            or (path.parts and path.parts[0].endswith(":"))
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise PipelineJobStoreError(f"pipeline job {label} path is invalid")
    return path.as_posix()


def _bounded_text(value: Any, label: str, maximum: int,
                  *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise PipelineJobStoreError(f"pipeline job {label} is invalid")
    if (not value and not allow_empty) or len(value.encode("utf-8")) > maximum:
        raise PipelineJobStoreError(f"pipeline job {label} exceeds its limit")
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not minimum <= value <= maximum):
        raise PipelineJobStoreError(f"pipeline job {label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in _HEX for char in value)):
        raise PipelineJobStoreError(f"pipeline job {label} is invalid")
    return value


def _valid_job_id(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 32
            and all(char in _HEX for char in value))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _now() -> int:
    return int(time.time())


def _open_private(path: Path, flags: int, *, create: bool = False) -> int:
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PipelineJobStoreError("pipeline job metadata is unavailable") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise PipelineJobStoreError("pipeline job metadata must be a regular file")
    if create:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
    return descriptor


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks, total = [], 0
    while total < limit:
        chunk = os.read(descriptor, min(8192, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("short write to pipeline job state")
        view = view[count:]
