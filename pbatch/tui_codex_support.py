"""Support helpers for the codex exec adapter: process lifecycle, bounded
field coercion, and runtime-info construction. Split out of
tui_adapter_codex.py to stay within the repository file-size budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .tui_protocol import RUNTIME_PROTOCOL_VERSION
from .tui_runtime import RuntimeCapabilities, RuntimeInfo

CODEX_EXEC_RUNTIME_NAME = "codex-exec"
MAX_FIELD_BYTES = 48 * 1024
TRUNCATION_MARKER = "\n...[truncated]"


def _runtime_info() -> RuntimeInfo:
    enabled = {
        "structured_events": True, "usage": True, "message_stream": True,
        "interrupt": True, "session_resume": True, "session_fork": True,
        "turn_steer": False, "message_queue": False, "typed_approvals": False,
        "review": False, "subagents": False, "images": False,
        "instruction_sources": False}
    capabilities = RuntimeCapabilities(
        runtime=CODEX_EXEC_RUNTIME_NAME, capabilities=enabled)
    return RuntimeInfo(CODEX_EXEC_RUNTIME_NAME, RUNTIME_PROTOCOL_VERSION, capabilities)


_HEAD_ONLY_OPTIONS = frozenset({"--approve-for-me"})


def _option_tail_index(argv: tuple[str, ...]) -> int:
    """Find Codex exec's trailing option block for resume/fork insertion.
    'codex exec resume' rejects a TRAILING --approve-for-me but accepts a
    trailing --json (measured 0.149 contract), so head-only options are
    skipped and the tail starts at the first other "-" token."""
    try:
        exec_index = argv.index("exec")
    except ValueError:
        exec_index = 0
    return next((index for index in range(exec_index + 1, len(argv))
                 if argv[index].startswith("-")
                 and argv[index] not in _HEAD_ONLY_OPTIONS), len(argv))


def _default_spawn(argv: Sequence[str], cwd: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, start_new_session=True)


def _stop_process(process: Any) -> None:
    if process is None or _process_done(process):
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            _call(process, "terminate")
        if _wait(process):
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            _call(process, "kill")
    else:
        _call(process, "kill")
    _wait(process)


def _process_done(process: Any) -> bool:
    try:
        return process.poll() is not None
    except (AttributeError, OSError):
        return False


def _wait(process: Any) -> bool:
    try:
        process.wait(timeout=0.25)
        return True
    except (subprocess.TimeoutExpired, TimeoutError, AttributeError, OSError):
        return False


def _call(process: Any, method: str) -> None:
    try:
        getattr(process, method)()
    except (AttributeError, OSError):
        pass


def _bound(value: Any, maximum: int = MAX_FIELD_BYTES) -> tuple[str, bool, int]:
    text = str(value if value is not None else "")
    encoded, marker = text.encode("utf-8", "replace"), TRUNCATION_MARKER.encode()
    if len(encoded) <= maximum:
        return text, False, len(encoded)
    prefix = encoded[:maximum - len(marker)].decode("utf-8", "ignore")
    return prefix + TRUNCATION_MARKER, True, len(encoded)


def _text(name: str, value: Any) -> dict[str, Any]:
    text, truncated, original = _bound(value)
    result: dict[str, Any] = {name: text, "truncated": truncated}
    if truncated:
        result["original_bytes"] = original
    return result


def _bounded_changes(value: Any) -> tuple[list[dict[str, str]], bool]:
    if not isinstance(value, list):
        return [], bool(value)
    result: list[dict[str, str]] = []; truncated = False
    for change in value:
        if not isinstance(change, Mapping):
            continue
        path, path_cut, _ = _bound(change.get("path", ""), 2048)
        kind, kind_cut, _ = _bound(change.get("kind", ""), 256)
        truncated = truncated or path_cut or kind_cut
        item = {"path": path, "kind": kind}
        if len(_json_bytes(result + [item])) > MAX_FIELD_BYTES:
            return result, True
        result.append(item)
    return result, truncated or len(result) != len(value)


def _bounded_list(value: Any) -> tuple[list[Any], bool]:
    if not isinstance(value, list):
        return [], bool(value)
    result: list[Any] = []
    for item in value:
        if len(_json_bytes(result + [item])) > MAX_FIELD_BYTES:
            return result, True
        result.append(item)
    return result, False


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def _request_value(request: Any, name: str, default: Any) -> Any:
    if isinstance(request, Mapping):
        return request.get(name, default)
    return getattr(request, name, default)


def _workspace_id(cwd: str) -> str:
    digest = hashlib.sha256(os.path.abspath(cwd).encode()).hexdigest()[:16]
    return f"ws_{digest}"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
