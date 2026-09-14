"""Runner-side materialization, output capture, and cleanup for workspaces."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path

from .fabric import (ExecutionSpec, LocalExecutionTarget,
                     _failed_execution_evidence)
from .fabric_workspace import normalize_workspace_request
from .workspace_bundle import SCHEMA, WorkspaceBundleError, bundle_digest
from .workspace_bundle import validate_bundle
from .workspace_files import (_remove_partial, pack_directory, restore_bundle)
from .fabric_runner_gpu import (preflight_gpu, report_cancel_if_requested,
                                report_gpu_preflight_failure)
from .fabric_gpu import probe_nvidia_gpus
from .fabric_runner_monitor import TaskMonitor

WORKSPACE_OUTPUT_CAP = 32 * 1024
_WORKSPACE_PARENT = ".pbatch-workspaces"


def _safe_dir_flags() -> int:
    nofollow, directory = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.name != "posix":
        raise WorkspaceBundleError("workspace filesystem protections unavailable")
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _prepare_parent(cwd: str) -> tuple[Path, tuple[int, int, int, int]]:
    raw = Path(cwd)
    if not raw.is_absolute():
        raise WorkspaceBundleError("workspace cwd must be an absolute real directory")
    root_fd = os.open(raw, _safe_dir_flags())
    try:
        try:
            os.mkdir(_WORKSPACE_PARENT, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        parent_fd = os.open(_WORKSPACE_PARENT, _safe_dir_flags(), dir_fd=root_fd)
        try:
            mode = stat.S_IMODE(os.fstat(parent_fd).st_mode)
            if mode & 0o077:
                raise WorkspaceBundleError("workspace parent permissions are too broad")
            root_info, parent_info = os.fstat(root_fd), os.fstat(parent_fd)
            identity = (root_info.st_dev, root_info.st_ino,
                        parent_info.st_dev, parent_info.st_ino)
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)
    return raw / _WORKSPACE_PARENT, identity


def workspace_directory(task: dict) -> tuple[Path, tuple[int, int, int, int]]:
    """Create a unique task/lease staging directory below the granted cwd."""
    lease = task.get("lease") if isinstance(task.get("lease"), dict) else {}
    identity = f"{task.get('task_id', '')}\0{lease.get('lease_id', '')}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    parent, identity = _prepare_parent(str(task.get("cwd", "")))
    return parent / ("w-" + suffix), identity


def prepare_workspace(task: dict) -> tuple[Path | None, dict | None, str, tuple | None]:
    """Validate the polled descriptor and materialize it before task spawn."""
    try:
        workspace = normalize_workspace_request(task.get("workspace"))
        destination, parent_identity = workspace_directory(task)
        restore_bundle(workspace["bundle"], destination)
        identity = _capture_directory_identity(destination, parent_identity)
        return destination, workspace, "", identity
    except (OSError, RuntimeError, ValueError) as exc:
        reason = (str(exc) if isinstance(exc, WorkspaceBundleError)
                  else "workspace staging directory is unavailable")
        return None, None, reason[:240], None


def _capture_directory_identity(path: Path, parent_identity: tuple) -> tuple:
    cwd_fd = os.open(path.parent.parent, _safe_dir_flags())
    try:
        cwd = os.fstat(cwd_fd)
        if (cwd.st_dev, cwd.st_ino) != parent_identity[:2]:
            raise WorkspaceBundleError("workspace cwd changed during staging")
        parent_fd = os.open(_WORKSPACE_PARENT, _safe_dir_flags(), dir_fd=cwd_fd)
        try:
            parent = os.fstat(parent_fd)
            if (parent.st_dev, parent.st_ino) != parent_identity[2:]:
                raise WorkspaceBundleError("workspace parent changed during staging")
            named = os.stat(path.name, dir_fd=parent_fd,
                            follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode):
                raise WorkspaceBundleError("workspace staging directory changed")
            fd = os.open(path.name, _safe_dir_flags(), dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise WorkspaceBundleError("workspace staging directory changed")
                return (*parent_identity, opened.st_dev, opened.st_ino, opened)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(cwd_fd)


def _empty_bundle() -> dict:
    return validate_bundle({"schema": SCHEMA, "files": []})


def _append_capture_failure(evidence: dict) -> None:
    detail = "workspace output capture failed"
    evidence["stderr"] = (str(evidence.get("stderr", "")) + "\n" + detail)[:WORKSPACE_OUTPUT_CAP]
    if evidence.get("result") == "passed":
        evidence["result"] = "failed"
        evidence["exit_code"] = -1


def bind_workspace_result(evidence: dict, workspace: dict,
                          directory: Path | None) -> dict:
    """Attach input/output hashes; missing requested outputs can never pass."""
    try:
        bundle = (pack_directory(directory, workspace["outputs"])
                  if directory is not None else _empty_bundle())
    except (OSError, RuntimeError, ValueError):
        bundle = _empty_bundle()
        _append_capture_failure(evidence)
    evidence["stdout_bytes"] = len(str(evidence.get("stdout", "")).encode(
        "utf-8", errors="replace"))
    evidence["stderr_bytes"] = len(str(evidence.get("stderr", "")).encode(
        "utf-8", errors="replace"))
    evidence["output_digest"] = hashlib.sha256(
        (str(evidence.get("stdout", "")) + str(evidence.get("stderr", "")))
        .encode("utf-8", errors="replace")).hexdigest()
    evidence["input_digest"] = workspace["sha256"]
    evidence["workspace"] = {
        "input_sha256": workspace["sha256"],
        "output_sha256": bundle_digest(bundle),
        "bundle": bundle,
    }
    return evidence


def staging_failure_evidence(task: dict, reason: str) -> dict:
    """Create ordinary failed evidence when safe staging cannot be completed."""
    from .fabric import FABRIC_VERSION

    workspace = normalize_workspace_request(task["workspace"])
    spec = ExecutionSpec(
        argv=[], cwd=str(task.get("cwd", "")) or ".",
        timeout=float(task.get("timeout", 60) or 60),
        effect_class=str(task.get("effect_class", "read_only")),
        mobility="pinned", label=f"task:{task.get('task_id', '')}")
    evidence = _failed_execution_evidence(
        spec, f"workspace-{hashlib.sha256(str(task.get('task_id', '')).encode()).hexdigest()[:12]}",
        time.monotonic(), reason[:240]).to_dict()
    evidence["runner_version"] = FABRIC_VERSION
    return bind_workspace_result(evidence, workspace, None)


def execute_workspace_task(client, task: dict, outbox, *, start_task,
                           submit_evidence, authorize_task_grant) -> dict:
    """Run the fenced Runner path for one already-authorized workspace task."""
    if not start_task(client, task):
        return report_cancel_if_requested(
            client, task, outbox, submit_evidence) or {
                "accepted": False, "reason": "task start rejected"}
    cancel_event = threading.Event()
    monitor = TaskMonitor(client, str(task.get("task_id", "")), cancel_event)
    staging = staging_identity = None
    monitor.start()
    try:
        assignment, reason = preflight_gpu(
            task.get("resources") or {}, task, probe_nvidia_gpus)
        if reason:
            return report_gpu_preflight_failure(
                client, task, assignment, reason, outbox, submit_evidence)
        staging, workspace, error, staging_identity = prepare_workspace(task)
        if error:
            cancelled = report_cancel_if_requested(
                client, task, outbox, submit_evidence)
            if cancelled is not None:
                return cancelled
            evidence = staging_failure_evidence(task, error)
        elif not start_task(client, task):
            return report_cancel_if_requested(
                client, task, outbox, submit_evidence) or {
                    "accepted": False, "reason": "task start rejected"}
        else:
            evidence = _run_workspace_payload(
                client, task, staging, workspace, assignment, cancel_event,
                authorize_task_grant)
        return submit_evidence(client, task, evidence, outbox)
    finally:
        monitor.stop()
        cleanup_workspace(staging, staging_identity)


def _run_workspace_payload(client, task, staging, workspace, assignment,
                           cancel_event, authorize_task_grant) -> dict:
    allowed, reasons = authorize_task_grant(client, task, str(staging))
    if not allowed:
        return staging_failure_evidence(
            task, "workspace execution grant denied: " + "; ".join(reasons))
    spec = ExecutionSpec(
        argv=task.get("argv", []), cwd=str(staging),
        timeout=float(task.get("timeout", 60) or 60),
        output_cap=WORKSPACE_OUTPUT_CAP,
        effect_class=str(task.get("effect_class", "read_only")),
        mobility="pinned", label=f"task:{task.get('task_id', '')}",
        cancel_event=cancel_event, gpu_assignment=assignment,
        restrict_gpu_visibility=isinstance(task.get("resources"), dict))
    evidence = LocalExecutionTarget().execute(spec).to_dict()
    return bind_workspace_result(evidence, workspace, staging)


def cleanup_workspace(path: Path | None, identity: tuple | None) -> None:
    """Clear only the captured task directory; retain its root and parent."""
    if path is None or identity is None:
        return
    cwd_fd = parent_fd = child_fd = -1
    try:
        cwd_fd = os.open(path.parent.parent, _safe_dir_flags())
        cwd = os.fstat(cwd_fd)
        if (cwd.st_dev, cwd.st_ino) != identity[:2]:
            return
        parent_fd = os.open(_WORKSPACE_PARENT, _safe_dir_flags(), dir_fd=cwd_fd)
        parent = os.fstat(parent_fd)
        if (parent.st_dev, parent.st_ino) != identity[2:4]:
            return
        child_fd = os.open(path.name, _safe_dir_flags(), dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) == identity[4:6]:
            _remove_partial(child_fd, identity[6])
    except OSError:
        pass
    finally:
        for fd in (child_fd, parent_fd, cwd_fd):
            if fd >= 0:
                os.close(fd)


__all__ = ["WORKSPACE_OUTPUT_CAP", "bind_workspace_result",
           "cleanup_workspace", "execute_workspace_task", "prepare_workspace",
           "staging_failure_evidence", "workspace_directory"]
