"""Bounded LF-JSONL transport and wire helpers for Pi RPC mode."""

from __future__ import annotations

from collections import deque
import json
import os
import subprocess
import threading
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .tui_codex_support import _bound, _stop_process
from .provider_env import provider_child_env
from .tui_protocol import RUNTIME_PROTOCOL_VERSION, RuntimeCapabilities
from .tui_runtime import RuntimeInfo


RUNTIME_NAME = "pi-rpc"
DEFAULT_QUEUE_SIZE = 256
DEFAULT_TIMEOUT = 5.0
MAX_RPC_LINE_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 32 * 1024
MAX_SESSION_PATH_BYTES = 16 * 1024
MAX_CLI_VALUE_BYTES = 4096


def pi_rpc_argv(
        agent: str = "pi", *, model: str = "", provider: str = "",
        session_name: str = "") -> list[str]:
    """Build the public Pi 0.84 RPC invocation without shell expansion."""
    executable = _cli_value(agent, "agent", required=True)
    argv = [executable, "--mode", "rpc"]
    for flag, value in (
            ("--provider", provider), ("--model", model),
            ("--name", session_name)):
        if value:
            argv.extend((flag, _cli_value(value, flag)))
    return argv


def _cli_value(value: Any, label: str, *, required: bool = False) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{label} must be a string")
    text = os.fsdecode(value)
    if "\x00" in text or (required and not text):
        raise ValueError(f"{label} must be a non-empty safe string")
    if len(text.encode("utf-8")) > MAX_CLI_VALUE_BYTES:
        raise ValueError(f"{label} exceeds {MAX_CLI_VALUE_BYTES} bytes")
    return text


def make_runtime_info(
        connected: bool, *, session_settings: bool = False,
        persistent_settings: bool = False, runtime_version: str = "") -> RuntimeInfo:
    names = (
        "structured_events", "message_stream", "interrupt",
        "session_resume", "session_fork", "turn_steer", "message_queue",
        "usage", "instruction_sources", "session_state", "session_stats",
        "session_compact", "model_catalog", "thinking_levels",
    )
    enabled = {name: connected for name in names}
    settings_enabled = session_settings or persistent_settings
    enabled.update({
        "model_select": connected and settings_enabled,
        "thinking_select": connected and settings_enabled,
    })
    enabled["typed_approvals"] = False
    caps = RuntimeCapabilities(runtime=RUNTIME_NAME, capabilities=enabled)
    scope = "session" if session_settings else (
        "global" if persistent_settings else "")
    return RuntimeInfo(
        RUNTIME_NAME, RUNTIME_PROTOCOL_VERSION, caps,
        runtime_version=runtime_version, settings_scope=scope)


def encode_command(value: Mapping[str, Any], maximum: int) -> str:
    try:
        line = json.dumps(
            dict(value), ensure_ascii=False, separators=(",", ":"),
            allow_nan=False)
        size = len(line.encode("utf-8")) + 1
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Pi RPC command must be valid JSON") from exc
    if size > maximum:
        raise ValueError(f"Pi RPC command exceeds {maximum} bytes")
    return line + "\n"


def decode_line(line: str, maximum: int = MAX_RPC_LINE_BYTES) -> dict[str, Any]:
    """Decode exactly one LF-framed object; CR is accepted only before LF."""
    if not isinstance(line, str):
        raise ValueError("Pi RPC record must be text")
    try:
        size = len(line.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("Pi RPC record is not valid UTF-8") from exc
    if size > maximum:
        raise ValueError(f"Pi RPC record exceeds {maximum} bytes")
    if not line.endswith("\n") or "\n" in line[:-1]:
        raise ValueError("Pi RPC record must use one LF delimiter")
    body = line[:-1]
    if body.endswith("\r"):
        body = body[:-1]
    if not body or "\r" in body:
        raise ValueError("Pi RPC record must contain one JSON object")
    return _decode_object(body)


def _decode_object(body: str) -> dict[str, Any]:
    try:
        value = json.loads(body, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Pi RPC record is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Pi RPC record must contain a JSON object")
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def session_path(state: Mapping[str, Any]) -> str:
    value = state.get("sessionFile")
    if not isinstance(value, str) or not value:
        raise ValueError("Pi get_state did not return sessionFile")
    if len(value.encode("utf-8")) > MAX_SESSION_PATH_BYTES:
        raise ValueError("Pi sessionFile is too long")
    return value


def message_text(message: Any) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return _bound(content)[0]
    if not isinstance(content, list):
        return ""
    parts = [item.get("text", "") for item in content
             if isinstance(item, Mapping) and item.get("type") == "text"]
    return _bound("".join(str(part) for part in parts))[0]


def normalized_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    aliases = {
        "input": "input_tokens", "output": "output_tokens",
        "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens",
        "totalTokens": "total_tokens",
    }
    usage: dict[str, Any] = {}
    for source, target in aliases.items():
        item = value.get(source)
        if _finite_number(item):
            usage[target] = item
    cost = value.get("cost")
    if isinstance(cost, Mapping):
        usage["cost"] = bounded_value(cost, 8 * 1024)
    return usage


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def bounded_value(value: Any, maximum: int = 24 * 1024) -> Any:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return _bound(repr(value), maximum)[0]
    if len(encoded) <= maximum:
        return value
    return {"summary": _bound(encoded.decode("utf-8", "replace"), maximum)[0],
            "truncated": True, "original_bytes": len(encoded)}


@runtime_checkable
class Transport(Protocol):
    def send_line(self, line: str) -> None: ...
    def recv_line(self) -> Optional[str]: ...
    def close(self) -> None: ...


def _default_spawn(argv: Sequence[str], cwd: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        list(argv), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True, env=provider_child_env())


class _LfReader:
    def __init__(self, stream: Any, maximum: int):
        self._stream, self._maximum = stream, maximum
        self._buffer = bytearray()

    def read_line(self) -> Optional[str]:
        while b"\n" not in self._buffer:
            chunk = self._read_chunk()
            if not chunk:
                if self._buffer:
                    raise ValueError("Pi RPC stream ended with an unterminated record")
                return None
            self._buffer.extend(chunk)
            if len(self._buffer) > self._maximum and b"\n" not in self._buffer:
                raise ValueError(f"Pi RPC record exceeds {self._maximum} bytes")
        raw, _, remainder = self._buffer.partition(b"\n")
        self._buffer[:] = remainder
        if len(raw) + 1 > self._maximum:
            raise ValueError(f"Pi RPC record exceeds {self._maximum} bytes")
        try:
            return raw.decode("utf-8") + "\n"
        except UnicodeDecodeError as exc:
            raise ValueError("Pi RPC record is not valid UTF-8") from exc

    def _read_chunk(self) -> bytes:
        reader = getattr(self._stream, "read1", None)
        value = reader(8192) if callable(reader) else self._stream.read(8192)
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value or b"")


class DefaultPiRpcTransport:
    """Own a local Pi RPC child with bounded stdout framing and stderr."""
    def __init__(
            self, cwd: str, *, agent: str = "pi", model: str = "",
            provider: str = "", session_name: str = "", spawn=None,
            max_line_bytes: int = MAX_RPC_LINE_BYTES):
        if (isinstance(max_line_bytes, bool) or not isinstance(max_line_bytes, int)
                or max_line_bytes <= 0):
            raise ValueError("max_line_bytes must be a positive integer")
        argv = pi_rpc_argv(
            agent, model=model, provider=provider, session_name=session_name)
        self.argv = tuple(argv)
        self._process = (spawn or _default_spawn)(argv, cwd)
        if self._process.stdin is None or self._process.stdout is None:
            _stop_process(self._process)
            raise RuntimeError("Pi RPC process pipes are unavailable")
        self._reader = _LfReader(self._process.stdout, max_line_bytes)
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._lock, self._stderr_lock = threading.Lock(), threading.Lock()
        self._closed = False
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="pi-rpc-stderr", daemon=True)
        self._stderr_thread.start()

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        while stream is not None:
            chunk = stream.read(4096)
            if not chunk:
                return
            raw = chunk.encode("utf-8", "replace") if isinstance(chunk, str) else bytes(chunk)
            raw = raw[-MAX_STDERR_BYTES:]
            with self._stderr_lock:
                self._stderr.append(raw)
                self._stderr_size += len(raw)
                while (self._stderr_size > MAX_STDERR_BYTES
                       and len(self._stderr) > 1):
                    self._stderr_size -= len(self._stderr.popleft())

    def send_line(self, line: str) -> None:
        if not isinstance(line, str) or not line.endswith("\n") or "\n" in line[:-1]:
            raise ValueError("Pi RPC writes require exactly one trailing LF")
        encoded = line.encode("utf-8")
        with self._lock:
            if self._closed or self._process.stdin is None:
                raise EOFError("Pi RPC input is closed")
            self._process.stdin.write(encoded)
            self._process.stdin.flush()

    def recv_line(self) -> Optional[str]:
        return self._reader.read_line()

    def unavailable_reason(self) -> str:
        with self._stderr_lock:
            detail = b"".join(self._stderr)[-MAX_STDERR_BYTES:].decode(
                "utf-8", "replace")
        code = self._process.poll()
        status = f"Pi RPC exited with code {code}" if code is not None else "Pi RPC disconnected"
        return f"{status}: {_bound(detail, 8192)[0]}" if detail else status

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream = self._process.stdin
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        _stop_process(self._process)
        self._stderr_thread.join(timeout=0.5)
