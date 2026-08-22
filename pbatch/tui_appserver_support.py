"""Transport, bounds and wire helpers for the Codex App Server adapter."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import subprocess
import threading
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .tui_codex_support import _bound, _bounded_changes, _request_value, _stop_process
from .tui_protocol import RUNTIME_PROTOCOL_VERSION, RuntimeCapabilities, RuntimeEvent
from .tui_runtime import RuntimeInfo

RUNTIME_NAME = "codex-app-server"
DEFAULT_QUEUE_SIZE = 256
DEFAULT_TIMEOUT = 5.0
DEFAULT_APPROVAL_TTL = 120.0
APPROVAL_METHODS = {
    "execCommandApproval": "exec_command",
    "fileChangeApproval": "file_change",
    "permissionsRequestApproval": "permissions",
    "applyPatchApproval": "apply_patch",
    "commandExecutionRequestApproval": "command_execution",
    "item/commandExecution/requestApproval": "command_execution",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
}


def make_runtime_info(initialized: bool, server_info: Mapping[str, Any]) -> RuntimeInfo:
    names = ("structured_events", "message_stream", "interrupt", "usage",
             "session_resume", "session_fork", "turn_steer", "typed_approvals")
    enabled = {name: initialized for name in names}
    raw = server_info.get("capabilities", {}) if initialized else {}
    for name in ("review", "subagents", "images", "instruction_sources"):
        enabled[name] = initialized and declared(raw, name)
    caps = RuntimeCapabilities(runtime=RUNTIME_NAME, capabilities=enabled)
    return RuntimeInfo(RUNTIME_NAME, RUNTIME_PROTOCOL_VERSION, caps)


def record_server_info(result: Mapping[str, Any]) -> dict[str, Any]:
    version = result.get("protocolVersion")
    if version is not None and version != RUNTIME_PROTOCOL_VERSION:
        raise RuntimeError(f"unsupported protocol version {version!r}")
    server = result.get("serverInfo", result.get("server", {}))
    server_version = server.get("version", "") if isinstance(server, Mapping) else ""
    return {"server_version": result.get("serverVersion", server_version or
                                         result.get("userAgent", "")),
            "schema_version": result.get("schemaVersion", version),
            "capabilities": result.get("capabilities", {}), "raw": dict(result)}


@runtime_checkable
class Transport(Protocol):
    """A connection carrying one JSON object per line."""
    def send_line(self, line: str) -> None: ...
    def recv_line(self) -> Optional[str]: ...
    def close(self) -> None: ...


class DefaultTransport:
    """Local stdio proxy transport; the child is always its own process group."""
    def __init__(self, daemon_socket: Optional[str] = None, *, cwd: Optional[str] = None):
        argv = ["codex", "app-server", "proxy"]
        if daemon_socket:
            argv.extend(("--sock", daemon_socket))
        self._stderr: deque[str] = deque(maxlen=16)
        self._lock, self._closed = threading.Lock(), False
        self._process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="codex-app-server-stderr", daemon=True)
        self._stderr_thread.start()

    def _read_stderr(self) -> None:
        for line in self._process.stderr or ():
            text = line.strip()
            if text:
                self._stderr.append(_bound(text, 4096)[0])

    def send_line(self, line: str) -> None:
        with self._lock:
            if self._closed or self._process.stdin is None:
                raise EOFError("proxy input is closed")
            self._process.stdin.write(line.rstrip("\r\n") + "\n")
            self._process.stdin.flush()

    def recv_line(self) -> Optional[str]:
        if self._process.stdout is None:
            return None
        line = self._process.stdout.readline()
        return line if line else None

    def unavailable_reason(self) -> str:
        detail = " | ".join(self._stderr)
        code = self._process.poll()
        status = f"proxy exited with code {code}" if code is not None else "proxy disconnected"
        return f"{status}: {detail}" if detail else status

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


class EventQueue:
    """Bounded queue that sacrifices warnings, never body or state events."""
    def __init__(self, maxsize: int):
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        self._maxsize, self._items = maxsize, deque()
        self._closed = False
        self._condition = threading.Condition()

    def put(self, event: RuntimeEvent) -> bool:
        with self._condition:
            while len(self._items) >= self._maxsize and not self._closed:
                index = next((i for i, item in enumerate(self._items)
                              if item.kind == "runtime.warning"), None)
                if index is not None:
                    del self._items[index]
                    break
                if event.kind == "runtime.warning":
                    return False
                self._condition.wait()
            if self._closed:
                return False
            self._items.append(event)
            self._condition.notify_all()
            return True

    def get(self) -> Optional[RuntimeEvent]:
        with self._condition:
            while not self._items and not self._closed:
                self._condition.wait()
            if not self._items:
                return None
            item = self._items.popleft()
            self._condition.notify_all()
            return item

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


@dataclass
class ApprovalEntry:
    request_id: str | int
    method: str
    approval_type: str
    params_summary: str
    received_at: str
    received_monotonic: float


class ItemProjector:
    """Notification item projection mixed into the stateful adapter."""
    def _item(self, phase: str, raw_item: Any) -> None:
        if not isinstance(raw_item, Mapping):
            self._unknown(f"item/{phase}", raw_item)
            return
        item = dict(raw_item)
        item_type = str(item.get("type", ""))
        if item_type in {"agentMessage", "agent_message"}:
            kind = "message.started" if phase == "started" else "message.completed"
            self._publish(kind, item_text(item, "text"))
        elif item_type in {"commandExecution", "command_execution"}:
            self._command_item(phase, item)
        elif item_type in {"fileChange", "file_change"}:
            self._file_change(item, phase)
        else:
            self._unknown(item_type or f"item/{phase}", item)

    def _command_item(self, phase: str, item: Mapping[str, Any]) -> None:
        item_id = str(item.get("id", ""))
        base = {"item_id": _bound(item_id, 2048)[0],
                "command": _bound(item.get("command", ""), 24 * 1024)[0]}
        if phase == "started":
            self._publish("command.started", base)
            return
        output = item.get("aggregatedOutput", item.get("aggregated_output",
                                                       self._command_output.get(item_id, "")))
        text, truncated, original = _bound(output)
        code = item.get("exitCode", item.get("exit_code"))
        base.update({"exit_code": code if isinstance(code, int) and not isinstance(code, bool)
                     else None, "aggregated_output": text, "truncated": truncated})
        if truncated:
            base["original_bytes"] = original
        self._publish("command.completed", base)

    def _command_delta(self, params: Mapping[str, Any]) -> None:
        item_id = str(params.get("itemId", params.get("item_id", "")))
        delta = str(params.get("delta", ""))
        output, _, _ = _bound(self._command_output.get(item_id, "") + delta)
        self._command_output[item_id] = output
        bounded = _bound(delta)[0]
        self._publish("command.output", {"item_id": _bound(item_id, 2048)[0],
                                         "output": bounded, "delta": bounded})

    def _file_change(self, params: Mapping[str, Any], phase: str) -> None:
        changes, truncated = _bounded_changes(params.get("changes", []))
        self._publish("file.changed", {
            "item_id": _bound(params.get("id", params.get("itemId", "")), 2048)[0],
            "status": _bound(params.get("status", phase), 256)[0],
            "changes": changes, "truncated": truncated})


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def request_key(value: Any) -> tuple[type, Any]:
    return type(value), value


def thread_id(value: Any) -> str:
    return str(value.get("id", value.get("threadId", ""))) if isinstance(value, Mapping) else ""


def turn_id(value: Any) -> str:
    return str(value.get("id", value.get("turnId", ""))) if isinstance(value, Mapping) else ""


def session_summary(thread: Mapping[str, Any]) -> dict[str, Any]:
    status = thread.get("status", "")
    if isinstance(status, Mapping):
        status = status.get("type", "")
    return {"id": thread.get("id", ""),
            "name": thread.get("name") or thread.get("preview", ""),
            "status": status,
            "updated": thread.get("updatedAt", thread.get("updated", ""))}


def item_text(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    text, truncated, original = _bound(value.get(field, ""))
    result = {"item_id": _bound(value.get("id", value.get("itemId", "")), 2048)[0],
              "text": text, "truncated": truncated}
    if truncated:
        result["original_bytes"] = original
    return result


def json_summary(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        text = repr(value)
    return _bound(text)[0]


def bounded_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"value": _bound(value)[0]}
    copied = dict(value)
    try:
        size = len(json.dumps(copied, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError):
        size = 65 * 1024
    return copied if size <= 60 * 1024 else {"summary": json_summary(copied), "truncated": True}


def declared(capabilities: Any, name: str) -> bool:
    aliases = {"instruction_sources": "instructionSources"}
    if isinstance(capabilities, Mapping):
        return capabilities.get(name) is True or capabilities.get(aliases.get(name, "")) is True
    return isinstance(capabilities, list) and name in capabilities


def approval_payload(entry: ApprovalEntry, target: str) -> dict[str, Any]:
    return {"request_id": entry.request_id, "approval_type": entry.approval_type,
            "method": entry.method, "target": _bound(target)[0],
            "received_at": entry.received_at}


def target_summary(params: Mapping[str, Any]) -> str:
    for name in ("command", "cwd", "fileChanges", "changes", "permissions", "reason"):
        if params.get(name):
            return f"{name}: {json_summary(params[name])}"
    return "approval target not supplied"


def decision_result(entry: ApprovalEntry, value: Any) -> Optional[dict[str, Any]]:
    supplied = _request_value(value, "result", None)
    if isinstance(supplied, Mapping):
        return dict(supplied)
    decision = str(_request_value(value, "decision", "")).strip()
    allow = decision in {"allow", "approve", "approved", "accept"}
    session = decision in {"allow_session", "approved_for_session", "acceptForSession"}
    deny = decision in {"deny", "denied", "decline"}
    cancel = decision in {"interrupt", "abort", "cancel"}
    if not any((allow, session, deny, cancel)):
        return None
    if entry.approval_type == "permissions":
        try:
            params = json.loads(entry.params_summary)
        except (TypeError, ValueError):
            return None
        permissions = params.get("permissions", {}) if allow or session else {}
        return {"permissions": permissions, "scope": "session" if session else "turn"}
    modern = entry.method.startswith("item/") or entry.method == "commandExecutionRequestApproval"
    if modern:
        mapped = "acceptForSession" if session else "accept" if allow else "cancel" if cancel else "decline"
        return {"decision": mapped}
    if session:
        mapped_value: Any = "approved_for_session"
    elif allow:
        mapped_value = "approved"
    elif cancel:
        mapped_value = "abort"
    else:
        mapped_value = {"denied": {"rejection": "user denied"}}
    return {"decision": mapped_value}
