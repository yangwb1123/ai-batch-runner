"""Verified stage receipts: the machine-readable record of what a pipeline verified.

The decision log answers "what did a human-readable summary say". It cannot
answer "which artifacts passed which validators, and has any of that changed
since". Nothing downstream can reuse knowledge it cannot first locate, so this
records the same event in a form code can consume.

Two boundaries are deliberate:

* **Runtime, not repository.** Receipts live under the private per-workspace
  ``data_dir`` (like ``advance/state.jsonl`` and the campaign state), never in
  the project tree. They are telemetry about a run, not a deliverable.
* **Never breaks the runner.** A receipt that cannot be written is a lost
  observation, not a failed pipeline. Every failure mode here degrades to
  ``False`` and is logged at debug level, matching the webhook and checkpoint
  paths.

The ``digest`` is an identity for change detection over a bounded prefix, not
an integrity proof: the point is to notice that a deliverable moved, cheaply,
without reading a large artifact twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config
from .config import log

RECEIPT_SCHEMA_VERSION = 1
RECEIPTS_MAX_BYTES = 4 * 1024 * 1024
RECEIPTS_MAX_LINE_BYTES = 8 * 1024
RECEIPTS_KEEP_LINES = 2000
READ_MAX_RECORDS = 1000
PATH_MAX_CHARS = 512
REASON_MAX_CHARS = 256
DIGEST_PREFIX_BYTES = 64 * 1024
KNOWLEDGE_MAX_LINES = 32
KNOWLEDGE_MAX_BYTES = 4096
READ_MAX_RECORDS = 1000


KNOWLEDGE_HEADER = (
    "Verified upstream artifacts (from an earlier run of this pipeline). "
    "These passed their stage validators; cite them instead of re-deriving "
    "them, and re-verify before relying on anything they do not cover:\n")


def verified_knowledge(cwd: str, outputs: list) -> str:
    """Bounded, re-verified list of upstream artifacts a prior run validated.

    Soundness rests on two properties, both load-bearing:

    * **Re-verified now.** An artifact is cited only if its size and digest
      still match the receipt. A receipt whose artifact has since been edited
      describes something that no longer exists, so it is dropped rather than
      presented as verified.
    * **Stable while nothing changed.** A receipt is keyed by absolute path
      and digest, never by timestamp, and a stage re-verified under --reuse
      writes a byte-identical receipt. So when upstream is unchanged this block
      is identical across runs, which keeps the downstream reuse fingerprint
      stable instead of self-invalidating.

    Returns "" when there is nothing safe to say, so a stage's prompt stays
    untouched rather than gaining an empty heading.
    """
    wanted = {_absolute(item) for item in outputs or ()}
    wanted.discard("")
    if not wanted:
        return ""
    latest = _latest_verified(cwd, wanted)
    body = "\n".join(_knowledge_lines(cwd, latest))
    if not body or len(body.encode("utf-8")) > KNOWLEDGE_MAX_BYTES:
        return ""
    return f"{KNOWLEDGE_HEADER}{body}"


def _latest_verified(cwd: str, wanted: set[str]) -> dict[str, tuple[int, str]]:
    """Newest passing receipt per path, ignoring failed stages and FAIL verdicts."""
    latest: dict[str, tuple[int, str]] = {}
    for record in read_receipts(cwd, limit=READ_MAX_RECORDS):
        if not record.get("ok") or record.get("verdict") in {"FAIL", "REJECT"}:
            continue
        for task in record.get("tasks") or ():
            path = str(task.get("path") or "")
            if path in wanted and task.get("ok") and task.get("digest"):
                latest[path] = (int(task.get("bytes") or 0), str(task["digest"]))
    return latest


def _knowledge_lines(cwd: str, latest: dict[str, tuple[int, str]]) -> list[str]:
    """One line per artifact that still matches its receipt on disk."""
    lines: list[str] = []
    for path in sorted(latest):
        if len(lines) >= KNOWLEDGE_MAX_LINES:
            break
        size, digest = latest[path]
        current_size, current_digest = _identity(path)
        if not digest or current_digest != digest or current_size != size:
            continue
        lines.append(f"- {_display(path, cwd)} ({size} bytes, sha256:{digest[:12]})")
    return lines


def _absolute(value: Any) -> str:
    try:
        return str(Path(str(value)).resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def _display(path: str, cwd: str) -> str:
    """Workspace-relative label when possible; the absolute path otherwise."""
    try:
        return str(Path(path).resolve().relative_to(Path(cwd).resolve()))
    except (OSError, RuntimeError, ValueError):
        return path


def receipts_path(cwd: str = "") -> Path:
    """Private append-only receipt ledger for one workspace."""
    return config.data_dir(cwd) / "pipeline" / "receipts.jsonl"


def record_stage(cwd: str, stage_name: str, results: list, stage_ok: bool,
                 verdict: Optional[str]) -> bool:
    """Append one receipt for a completed stage. Best effort by contract."""
    record = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stage": _brief(stage_name, 128),
        "ok": bool(stage_ok),
        "verdict": _brief(verdict, 64) if verdict else "",
        "tasks": [_task_receipt(item) for item in results or ()],
    }
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        log.debug("pipeline receipts: unserialisable record (%s)", exc)
        return False
    payload = line.encode("utf-8")
    if len(payload) > RECEIPTS_MAX_LINE_BYTES:
        record["tasks"] = []
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    return _append(cwd, payload)


def read_receipts(cwd: str = "", limit: int = 50) -> list[dict]:
    """Return the most recent receipts, newest last. Bounded on every axis."""
    try:
        maximum = max(1, min(int(limit), READ_MAX_RECORDS))
    except (TypeError, ValueError):
        maximum = 50
    path = receipts_path(cwd)
    if not path.is_file():
        return []
    try:
        raw = _tail_bytes(path, RECEIPTS_MAX_BYTES)
    except OSError as exc:
        log.debug("pipeline receipts: unreadable ledger (%s)", exc)
        return []
    records = []
    for line in raw.splitlines()[-maximum:]:
        value = _decode(line)
        if value is not None:
            records.append(value)
    return records


def _task_receipt(result: Any) -> dict[str, Any]:
    task = getattr(result, "task", None)
    output = str(getattr(task, "output", "") or "")
    resolved, size, digest = "", 0, ""
    if output:
        resolved = _output_path(task, result)
        size, digest = _identity(resolved)
    return {
        "output": _brief(output, PATH_MAX_CHARS),
        # Absolute, because stage_outputs carries absolute paths (see
        # pipeline.py) and the knowledge block has to match the two exactly.
        # It is derived from the stage cwd, so it is stable across re-runs.
        "path": _brief(resolved, PATH_MAX_CHARS),
        "ok": bool(getattr(result, "success", False)),
        "bytes": size,
        "digest": digest,
        "reason": _brief(getattr(result, "reason", "") or "", REASON_MAX_CHARS),
        "attempt": int(getattr(result, "attempt", 0) or 0),
        "stage_id": _brief(getattr(result, "stage_id", "") or "", 128),
    }


def _output_path(task: Any, result: Any) -> str:
    resolve = getattr(task, "output_path", None)
    if callable(resolve):
        try:
            return str(resolve())
        except (OSError, TypeError, ValueError):
            return ""
    return str(getattr(result, "output_path", "") or "")


def _identity(path: Any) -> tuple[int, str]:
    """Bounded (size, digest) of one regular file; (0, "") for anything else."""
    try:
        target = Path(str(path))
        info = os.lstat(target)
        if not stat.S_ISREG(info.st_mode):
            return 0, ""
        hasher = hashlib.sha256()
        read = 0
        with open(target, "rb") as handle:
            while read < DIGEST_PREFIX_BYTES:
                block = handle.read(min(65536, DIGEST_PREFIX_BYTES - read))
                if not block:
                    break
                hasher.update(block)
                read += len(block)
        return int(info.st_size), hasher.hexdigest()[:32]
    except (OSError, TypeError, ValueError) as exc:
        log.debug("pipeline receipts: cannot identify artifact (%s)", exc)
        return 0, ""


def _brief(value: Any, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum].decode("utf-8", "ignore")


def _append(cwd: str, payload: bytes) -> bool:
    path = receipts_path(cwd)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _rotate(path, len(payload) + 1)
        with path.open("ab") as handle:
            handle.write(payload + b"\n")
    except (OSError, ValueError) as exc:
        log.debug("pipeline receipts: write failed (%s)", exc)
        return False
    return True


def _rotate(path: Path, incoming: int) -> None:
    """Keep the ledger inside its byte budget by dropping the oldest records."""
    current = path.stat().st_size if path.exists() else 0
    if current + incoming <= RECEIPTS_MAX_BYTES:
        return
    budget = max(0, RECEIPTS_MAX_BYTES - incoming)
    kept: list[bytes] = []
    used = 0
    for line in reversed(_tail_lines(path, RECEIPTS_MAX_BYTES)[-RECEIPTS_KEEP_LINES:]):
        size = len(line) + 1
        if used + size > budget:
            break
        kept.append(line)
        used += size
    payload = b"\n".join(reversed(kept)) + (b"\n" if kept else b"")
    descriptor, temporary = tempfile.mkstemp(prefix="receipts.", suffix=".tmp",
                                            dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _tail_bytes(path: Path, maximum: int) -> bytes:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - max(1, maximum))
        handle.seek(start)
        data = handle.read(max(1, maximum))
    if start:
        _, separator, data = data.partition(b"\n")
        return data if separator else b""
    return data


def _tail_lines(path: Path, maximum: int) -> list[bytes]:
    if not path.is_file():
        return []
    return [line for line in _tail_bytes(path, maximum).splitlines() if line]


def _decode(line: bytes) -> Optional[dict]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return None
    return value