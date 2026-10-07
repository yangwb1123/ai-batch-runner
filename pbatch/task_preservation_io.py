"""Bounded private file-copy I/O and exclusive publication. No source deletion."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .task_center_io import checked_path, _open_regular
from .task_state_io import _sync_directory
from .task_preservation_schema import MAX_PAYLOAD_BYTES, MAX_RECEIPT_BYTES, validate_receipt


def stat_fields(info) -> dict:
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": info.st_mode,
            "bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate preservation JSON field")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nonfinite preservation JSON")


def read_receipt(path: Path) -> dict | None:
    try:
        with _open_regular(path) as handle:
            before = stat_fields(os.fstat(handle.fileno()))
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
            after = stat_fields(os.fstat(handle.fileno()))
        current = stat_fields(checked_path(path).stat())
        if not hasattr(os, "geteuid") or current["uid"] != os.geteuid() or current["mode"] & 0o077:
            raise ValueError("preservation receipt is not an owned private file")
    except FileNotFoundError:
        return None
    if len(raw) > MAX_RECEIPT_BYTES or before != after or after != current:
        raise ValueError("preservation receipt is oversized or changed during bounded read")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid preservation JSON") from exc
    validate_receipt(value)
    return value


def copy_stream(source: Path, output, expected: dict) -> dict:
    digest, total = hashlib.sha256(), 0
    with _open_regular(source) as handle:
        before = stat_fields(os.fstat(handle.fileno()))
        if (before != expected or not stat.S_ISREG(before["mode"]) or before["bytes"] > MAX_PAYLOAD_BYTES or
                not hasattr(os, "geteuid") or before["uid"] != os.geteuid()):
            raise ValueError("preservation source changed/type or byte budget exhausted; no truncation")
        while chunk := handle.read(min(65536, MAX_PAYLOAD_BYTES - total + 1)):
            total += len(chunk)
            if total > MAX_PAYLOAD_BYTES:
                raise ValueError("preservation payload grew beyond byte budget; no truncation")
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        after = stat_fields(os.fstat(handle.fileno()))
    if before != after or after != stat_fields(checked_path(source).stat()) or total != after["bytes"]:
        raise ValueError("preservation source changed during bounded copy or verification")
    return {"bytes": total, "sha256": digest.hexdigest()}


def publish(temporary: Path, receipt: dict, root: Path) -> None:
    """Payload first, receipt last; leftovers are evidence, never capacity-cleaned."""
    key = receipt["preservation_id"]
    payload, meta = root / (key + ".blob"), root / (key + ".json")
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("preservation receipt byte budget exceeded")
    if os.path.lexists(payload) or os.path.lexists(meta):
        raise ValueError("preservation publication collision; no existing file was overwritten")
    os.link(temporary, payload)  # exclusive; no rename over an existing destination
    _sync_directory(root)
    staged = temporary.with_suffix(".receipt")
    created = None
    try:
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            created = os.fstat(handle.fileno())
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(staged, meta)
        _sync_directory(root)
    finally:
        remove_owned_temporary(staged, created)


def remove_owned_temporary(path: Path, created) -> None:
    if created is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino) and stat.S_ISREG(current.st_mode):
        path.unlink()  # own unpublished inode only, never existing/final recovery evidence
