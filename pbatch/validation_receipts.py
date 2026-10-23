"""Best-effort, content-free observations of artifact validator invocations.

Storage budgets and rotation are shared with pipeline_receipts. These records
are observations, never authorization to save or reuse an artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
import time
from pathlib import Path

from . import config, pipeline_receipts as storage
from .config import log
from .validation_batch import run_file_validators

RECEIPT_SCHEMA_VERSION = 1
DIGEST_PREFIX_BYTES = storage.DIGEST_PREFIX_BYTES
_WRITE_LOCK = threading.Lock()
_MISSING_ARTIFACT = (
    "validation failed: configured validator requires an artifact output target"
)


def reject_missing_artifact(result) -> bool:
    """Refuse a configured gate without pretending a validator ran."""
    result.success = False
    result.validation_ok = False
    result.validation_stderr = _MISSING_ARTIFACT
    result.reason = _MISSING_ARTIFACT
    log.error("NOT SAVED: %s", _MISSING_ARTIFACT)
    return False


def receipts_path(cwd: str = "") -> Path:
    return config.data_dir(cwd) / "validation" / "receipts.jsonl"


def command_identity(command: str):
    """SHA-256 identity only; never retain a command or its arguments."""
    if not isinstance(command, str) or not command:
        return None
    try:
        hasher = hashlib.sha256()
        for offset in range(0, len(command), DIGEST_PREFIX_BYTES):
            chunk = command[offset:offset + DIGEST_PREFIX_BYTES]
            hasher.update(chunk.encode("utf-8", "replace"))
        return hasher.hexdigest()
    except Exception as exc:
        log.debug("validation receipts: identity unavailable (%s)", type(exc).__name__)
        return None


def _task_scope(task) -> str:
    """Recognize the existing gate_fix task metadata without changing callers."""
    if task is None:
        return "task"
    stage = str(task.memory.get("stage", ""))
    name = Path(task.output).stem
    prefix, separator, round_number = name.rpartition("-")
    if stage.endswith("-fix") and prefix == stage and separator and round_number.isdecimal():
        return "gate-fix"
    return "task"


def run_recorded_validators(specs, workdir, artifact, expand_command, invoke,
                            logger, label, *, task=None, result=None,
                            scope=None, cancel_event=None, runner=None):
    """Keep file filtering, parallel execution and declaration-order AND folding.

    Hash templates before expansion so random temporary filenames do not change
    command identity. The expanded command is used solely for the actual call.
    ``runner`` lets the caller keep its historical ``run_file_validators`` seam
    (monkeypatchable) while receipts stay observational.
    """
    identities = {}

    def expand(template, cwd, output):
        command = expand_command(template, cwd, output)
        identities[command] = command_identity(template)
        return command

    def run_one(command, cwd):
        return observe_validation(
            invoke, command, cwd, artifact, scope=scope or _task_scope(task),
            task=task, result=result, command_hash=identities.get(command),
            cancel_event=cancel_event)

    return (runner or run_file_validators)(
        specs, workdir, artifact, expand, run_one, logger, label)


def observe_validation(invoke, command, cwd, artifact, *, scope="task",
                       task=None, result=None, command_hash=None,
                       cancel_event=None):
    """Measure exactly the actual call, returning its original result unchanged.

    Exceptions from execution still propagate, with unknown outcome fields in
    the single receipt attempt. Clock/receipt failures never affect execution.
    """
    validation = None
    started = _clock()
    try:
        validation = (invoke(command, cwd) if cancel_event is None else
                      invoke(command, cwd, cancel_event=cancel_event))
        return validation
    finally:
        finished = _clock()
        record_invocation(cwd, artifact, validation, _duration(started, finished),
                          command_hash or command_identity(command), scope,
                          task=task, result=result)


def _clock():
    try:
        return time.perf_counter()
    except Exception as exc:
        log.debug("validation receipts: clock unavailable (%s)", type(exc).__name__)
        return None


def _duration(started, finished):
    try:
        values = (started, finished)
        if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
            return None
        elapsed = finished - started
        return elapsed if math.isfinite(elapsed) and elapsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def record_invocation(cwd, artifact, validation, duration, command_hash, scope,
                      *, task=None, result=None) -> bool:
    """Append one bounded observation; exclude all bodies, diagnostics and paths."""
    try:
        identity = _artifact_identity(artifact)
        target = task.output_path() if task is not None else artifact
        record = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": "validator_invocation",
            "command_hash": command_hash,
            "scope": scope,
            "task_id_hash": command_identity(getattr(result, "task_id", None)),
            "stage_id_hash": command_identity(getattr(result, "stage_id", None)),
            "artifact_path_hash": command_identity(str(target)) if target is not None else None,
            "exit_code": getattr(validation, "exit_code", None),
            "timed_out": getattr(validation, "timed_out", None),
            "ok": getattr(validation, "ok", None),
            "duration_seconds": duration,
            "artifact_bytes": identity[0],
            "artifact_digest": identity[1],
            "artifact_digest_bytes": identity[2],
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        return _append(cwd, payload)
    except Exception as exc:
        # Exception text can itself contain commands, paths or secrets.
        log.debug("validation receipts: observation failed (%s)", type(exc).__name__)
        return False


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _open_file(path, flags, mode):
    """Close our descriptor even if wrapping it in a Python stream fails."""
    descriptor = os.open(path, flags, 0o600)
    try:
        return os.fdopen(descriptor, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _private_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not info.st_mode & 0o077
            and (not hasattr(os, "getuid") or info.st_uid == os.getuid()))


def _artifact_identity(path):
    """Bounded prefix SHA-256; missing, changed or non-regular files are unknown."""
    unknown = (None, None, None)
    if path is None:
        return unknown
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            return unknown
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with _open_file(path, flags, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _signature(before) != _signature(opened):
                return unknown
            prefix = handle.read(DIGEST_PREFIX_BYTES)
            after = os.fstat(handle.fileno())
        if (_signature(before) != _signature(after)
                or _signature(before) != _signature(os.lstat(path))):
            return unknown
        return before.st_size, hashlib.sha256(prefix).hexdigest(), len(prefix)
    except (OSError, TypeError, ValueError):
        return unknown


def _append(cwd, payload) -> bool:
    """Serialize thread writers and reuse the pipeline ledger's bounded rotation."""
    if len(payload) > storage.RECEIPTS_MAX_LINE_BYTES:
        log.debug("validation receipts: oversized observation")
        return False
    if len(payload) + 1 > storage.RECEIPTS_MAX_BYTES:
        log.debug("validation receipts: observation exceeds ledger budget")
        return False
    path = receipts_path(cwd)
    workspace = Path(cwd or os.getcwd()).resolve()
    if workspace == path.resolve() or workspace in path.resolve().parents:
        log.debug("validation receipts: refusing runtime writes inside workspace")
        return False
    with _WRITE_LOCK:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077:
            raise OSError("receipt directory must be private")
        if path.is_symlink() or (path.exists() and not _private_file(path.lstat())):
            raise OSError("receipt ledger must be a private regular file")
        storage._rotate(path, len(payload) + 1)
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        with _open_file(path, flags, "ab") as handle:
            if not _private_file(os.fstat(handle.fileno())):
                raise OSError("receipt descriptor must be a private regular file")
            if handle.write(payload + b"\n") != len(payload) + 1:
                raise OSError("incomplete receipt write")
    return True


def read_receipts(cwd: str = "", limit: int = 50) -> list:
    """Read a bounded tail, oldest first, without creating runtime directories."""
    try:
        maximum = max(1, min(int(limit), storage.READ_MAX_RECORDS))
        path = receipts_path(cwd)
        if not _private_file(path.lstat()):
            return []
        raw = storage._tail_bytes(path, storage.RECEIPTS_MAX_BYTES)
        records = []
        for line in raw.splitlines()[-maximum:]:
            if len(line) > storage.RECEIPTS_MAX_LINE_BYTES:
                continue
            record = storage._decode(line)
            if record is not None and record.get("kind") == "validator_invocation":
                records.append(record)
        return records
    except Exception as exc:
        log.debug("validation receipts: read failed (%s)", type(exc).__name__)
        return []
