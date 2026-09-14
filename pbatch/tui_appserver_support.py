"""Transport, bounds and wire helpers for the Codex App Server adapter."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import queue
import subprocess
import threading
import time
import unicodedata
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .tui_codex_support import (
    _bound, _bounded_changes, _bounded_id, _request_value, _stop_process,
)
from .tui_protocol import (
    RUNTIME_PROTOCOL_VERSION, RuntimeCapabilities, RuntimeEvent, TurnState,
)
from .provider_env import provider_child_env
from .tui_runtime import (
    ListSessionsRequest, RuntimeInfo, RuntimeSessionObservation, RuntimeSessionPage,
    RuntimeSessionSummary,
)
from .tui_view_support import clean_text, one_line

RUNTIME_NAME = "codex-app-server"
DEFAULT_QUEUE_SIZE = 256
DEFAULT_TIMEOUT = 5.0
DEFAULT_APPROVAL_TTL = 120.0
DEFAULT_MAX_PENDING_APPROVALS = 32
MAX_ACTIVE_FLAGS = 32
MAX_SESSION_PAGE_SIZE = 100
MAX_SESSION_CURSOR_BYTES = 4 * 1024
MAX_SESSION_NAME_BYTES = 4 * 1024
MAX_SESSION_PREVIEW_BYTES = 4 * 1024
MAX_SESSION_PROVIDER_BYTES = 512
THREAD_STATUS_TYPES = frozenset({"notLoaded", "idle", "systemError", "active"})
SESSION_SORT_KEYS = frozenset({"created_at", "updated_at", "recency_at"})
SESSION_SORT_DIRECTIONS = frozenset({"asc", "desc"})
SESSION_SOURCE_KINDS = frozenset({
    "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
    "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
})
_POLICY_FIELDS = {
    "approval": ("approvalPolicy", "approval_policy", "approval"),
    "sandbox": ("sandboxPolicy", "sandbox_policy", "sandbox", "sandboxMode"),
    "network": ("networkPolicy", "network_policy", "networkAccess", "network_access"),
}
_POLICY_NESTED = ("effectivePolicy", "effective_policy", "policy", "config")
_SESSION_QUERY_FIELDS = frozenset({
    "cursor", "limit", "sort_key", "sort_direction", "model_providers",
    "source_kinds", "archived", "is_pinned", "search_term",
    "use_state_db_only",
})
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
             "session_resume", "session_read", "session_list", "session_fork",
             "session_archive", "turn_steer", "typed_approvals")
    enabled = {name: initialized for name in names}
    raw = server_info.get("capabilities", {}) if initialized else {}
    for name in ("review", "subagents", "images", "instruction_sources"):
        enabled[name] = initialized and declared(raw, name)
    caps = RuntimeCapabilities(runtime=RUNTIME_NAME, capabilities=enabled)
    return RuntimeInfo(RUNTIME_NAME, RUNTIME_PROTOCOL_VERSION, caps)


def effective_policy_values(value: Any) -> dict[str, str]:
    """Extract only explicit, display-safe effective policy scalars.

    App Server versions place these fields in different response objects.  The
    UI must show unknown when none is supplied; it must never infer a policy
    from local defaults.  Structured values are intentionally summarized as
    ``present`` so a policy payload cannot leak arbitrary provider data.
    """
    sources = [value] if isinstance(value, Mapping) else []
    if sources:
        for key in _POLICY_NESTED:
            nested = value.get(key)
            if isinstance(nested, Mapping):
                sources.insert(0, nested)
    result: dict[str, str] = {}
    for label, keys in _POLICY_FIELDS.items():
        for source in sources:
            key = next((name for name in keys if name in source), None)
            if key is not None:
                result[label] = _policy_scalar(source.get(key))
                break
    return result


def explicit_policy_values(values: Mapping[str, Any]) -> dict[str, str]:
    """Return known values suitable for one typed policy event."""
    return {
        name: values[name] for name in ("approval", "sandbox", "network")
        if isinstance(values.get(name), str) and values[name] != "unknown"
    }


def _policy_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return one_line(clean_text(value))[:256] or "unknown"
    if value is None:
        return "unknown"
    return "present"


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
    """Local App Server transport; the child is always its own process group.

    A normal local integration owns a stdio App Server process.  ``proxy`` is
    only appropriate when the caller explicitly supplied an already-running
    daemon socket.
    """
    def __init__(self, daemon_socket: Optional[str] = None, *, cwd: Optional[str] = None):
        argv = app_server_argv(daemon_socket)
        self._stderr: deque[str] = deque(maxlen=16)
        self._lock, self._closed = threading.Lock(), False
        self._process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
            env=provider_child_env(),
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


def app_server_argv(daemon_socket: Optional[str] = None) -> list[str]:
    """Return the official stdio command or an explicit daemon proxy command."""
    if daemon_socket:
        return ["codex", "app-server", "proxy", "--sock", str(daemon_socket)]
    return ["codex", "app-server"]


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

    def get(self, timeout: Optional[float] = None) -> Optional[RuntimeEvent]:
        with self._condition:
            deadline = None if timeout is None else time.monotonic() + max(0, timeout)
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if not self._items:
                return None
            item = self._items.popleft()
            self._condition.notify_all()
            return item

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class RuntimeEventStream:
    """Iterator-compatible queue view for incremental TUI consumers."""

    def __init__(self, source: EventQueue):
        self._source = source

    def get(self, timeout: Optional[float] = None) -> Optional[RuntimeEvent]:
        return self._source.get(timeout)

    def __iter__(self) -> "RuntimeEventStream":
        return self

    def __next__(self) -> RuntimeEvent:
        event = self._source.get()
        if event is None:
            raise StopIteration
        return event


@dataclass
class ApprovalEntry:
    request_id: str | int
    method: str
    approval_type: str
    params_summary: str
    target: str
    received_at: str
    expires_at: str
    received_monotonic: float
    available_decisions: tuple[str, ...]
    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    actor_id: str


class ItemProjector:
    """Notification item projection mixed into the stateful adapter."""
    def _item(self, phase: str, raw_item: Any) -> None:
        if not isinstance(raw_item, Mapping):
            self._unknown(f"item/{phase}", raw_item)
            return
        item = dict(raw_item)
        self._project_explicit_actor(phase, item)
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

    def _project_explicit_actor(self, phase: str,
                                item: Mapping[str, Any]) -> None:
        """Optional adapter hook for provider-declared actor lineage."""

    def _command_item(self, phase: str, item: Mapping[str, Any]) -> None:
        item_id = str(item.get("id", ""))
        base = {"item_id": _bounded_id(item_id),
                "command": _bound(item.get("command", ""), 24 * 1024)[0]}
        if phase == "started":
            self._command_artifact_started(item_id)
            self._command_output.pop(item_id, None)
            self._publish("command.started", base)
            return
        supplied = "aggregatedOutput" in item or "aggregated_output" in item
        output = item.get("aggregatedOutput", item.get(
            "aggregated_output", self._command_output.get(item_id, "")))
        text, truncated, original = _bound(output)
        code = item.get("exitCode", item.get("exit_code"))
        base.update({"exit_code": code if isinstance(code, int) and not isinstance(code, bool)
                     else None, "aggregated_output": text, "truncated": truncated})
        if truncated:
            base["original_bytes"] = original
        self._command_artifact_completed(
            item_id, base["command"], output, supplied)
        self._command_output.pop(item_id, None)
        self._publish("command.completed", base)

    def _command_delta(self, params: Mapping[str, Any]) -> None:
        item_id = str(params.get("itemId", params.get("item_id", "")))
        delta = str(params.get("delta", ""))
        self._command_artifact_delta(item_id, delta)
        output, _, _ = _bound(self._command_output.get(item_id, "") + delta)
        self._command_output[item_id] = output
        bounded = _bound(delta)[0]
        self._publish("command.output", {"item_id": _bounded_id(item_id),
                                         "output": bounded, "delta": bounded})

    def _command_artifact_started(self, item_id: str) -> None:
        """Optional hook implemented by a stateful runtime adapter."""

    def _command_artifact_delta(self, item_id: str, delta: str) -> None:
        """Optional hook implemented by a stateful runtime adapter."""

    def _command_artifact_completed(
            self, item_id: str, command: str,
            output: Any, supplied: bool) -> None:
        """Optional hook implemented by a stateful runtime adapter."""

    def _file_change(self, params: Mapping[str, Any], phase: str) -> None:
        changes, truncated = _bounded_changes(params.get("changes", []))
        self._publish("file.changed", {
            "item_id": _bounded_id(params.get("id", params.get("itemId", ""))),
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


def session_summary(thread: Mapping[str, Any]) -> RuntimeSessionSummary:
    """Validate one ``thread/list`` item without turning display text into ids."""
    if not isinstance(thread, Mapping):
        raise ValueError("thread/list item must be an object")
    provider_id = _exact_wire_text(
        thread.get("id"), "thread/list id", 2_048, required=True)
    status, flags = _thread_status(thread.get("status"), "thread/list")
    name = _display_wire_text(
        thread.get("name"), "thread/list name", MAX_SESSION_NAME_BYTES)
    preview = _display_wire_text(
        thread.get("preview"), "thread/list preview", MAX_SESSION_PREVIEW_BYTES)
    model_provider = _display_wire_text(
        thread.get("modelProvider"), "thread/list modelProvider",
        MAX_SESSION_PROVIDER_BYTES)
    return RuntimeSessionSummary(
        provider_session_id=provider_id,
        name=name,
        preview=preview,
        status=status,
        active_flags=flags,
        model_provider=model_provider,
        created_at=_wire_timestamp(thread, "createdAt"),
        updated_at=_wire_timestamp(thread, "updatedAt"),
        ephemeral=_wire_bool(thread, "ephemeral"),
        is_pinned=_wire_bool(thread, "isPinned"),
    )


def session_page(result: Mapping[str, Any], *, limit: int) -> RuntimeSessionPage:
    """Parse one bounded provider page and preserve pagination evidence."""
    if not isinstance(result, Mapping):
        raise ValueError("thread/list returned a non-object result")
    data = result.get("data")
    if not isinstance(data, list):
        raise ValueError("thread/list returned no data list")
    if "nextCursor" not in result:
        raise ValueError("thread/list returned no nextCursor field")
    if len(data) > limit or len(data) > MAX_SESSION_PAGE_SIZE:
        raise ValueError("thread/list returned more items than requested")
    summaries: list[RuntimeSessionSummary] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for position, thread in enumerate(data, 1):
        try:
            summary = session_summary(thread)
            if summary.provider_session_id in seen:
                raise ValueError("duplicate thread id")
        except ValueError as exc:
            warnings.append(f"thread/list item {position} skipped: {_bound(exc, 512)[0]}")
            continue
        seen.add(summary.provider_session_id)
        summaries.append(summary)
    cursor = result.get("nextCursor")
    if cursor is not None:
        cursor = _exact_wire_text(
            cursor, "thread/list nextCursor", MAX_SESSION_CURSOR_BYTES,
            required=True)
    return RuntimeSessionPage(tuple(summaries), cursor, tuple(warnings))


def session_query_params(request: Any, cwd: str) -> dict[str, Any]:
    """Normalize a typed query and force it into this adapter's workspace."""
    if request is None:
        request = ListSessionsRequest()
    if not isinstance(request, (ListSessionsRequest, Mapping)):
        raise TypeError("session query must be ListSessionsRequest, mapping, or null")
    if isinstance(request, Mapping):
        unknown = [key for key in request
                   if not isinstance(key, str) or key not in _SESSION_QUERY_FIELDS]
        if unknown:
            raise ValueError("unsupported session query field")
    defaults = ListSessionsRequest()
    limit = _query_value(request, "limit", defaults.limit)
    if (isinstance(limit, bool) or not isinstance(limit, int)
            or limit <= 0 or limit > MAX_SESSION_PAGE_SIZE):
        raise ValueError(
            f"session query limit must be between 1 and {MAX_SESSION_PAGE_SIZE}")
    sort_key = _query_choice(
        request, "sort_key", defaults.sort_key, SESSION_SORT_KEYS)
    direction = _query_choice(
        request, "sort_direction", defaults.sort_direction,
        SESSION_SORT_DIRECTIONS)
    params: dict[str, Any] = {
        "limit": limit,
        "sortKey": sort_key,
        "sortDirection": direction,
        "sourceKinds": _query_text_list(
            request, "source_kinds", defaults.source_kinds,
            SESSION_SOURCE_KINDS),
        "archived": _query_bool(request, "archived", defaults.archived),
        "cwd": os.path.realpath(os.path.abspath(cwd)),
        "useStateDbOnly": _query_bool(
            request, "use_state_db_only", defaults.use_state_db_only),
    }
    params.update(_optional_session_query_params(request, defaults))
    return params


def _optional_session_query_params(
        request: Any, defaults: ListSessionsRequest) -> dict[str, Any]:
    params: dict[str, Any] = {}
    cursor = _query_value(request, "cursor", defaults.cursor)
    if cursor is not None:
        params["cursor"] = _exact_wire_text(
            cursor, "session query cursor", MAX_SESSION_CURSOR_BYTES,
            required=True)
    providers = _query_text_list(
        request, "model_providers", defaults.model_providers, None)
    if providers:
        params["modelProviders"] = providers
    pinned = _query_value(request, "is_pinned", defaults.is_pinned)
    if pinned is not None:
        if not isinstance(pinned, bool):
            raise ValueError("session query is_pinned must be a boolean or null")
        params["isPinned"] = pinned
    search = _query_value(request, "search_term", defaults.search_term)
    search = _exact_wire_text(
        search, "session query search_term", 4_096)
    if search:
        params["searchTerm"] = search
    return params


def _query_value(request: Any, name: str, default: Any) -> Any:
    return request.get(name, default) if isinstance(request, Mapping) else getattr(
        request, name, default)


def _query_choice(
        request: Any, name: str, default: str,
        choices: frozenset[str]) -> str:
    value = _query_value(request, name, default)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"session query {name} is unsupported")
    return value


def _query_bool(request: Any, name: str, default: bool) -> bool:
    value = _query_value(request, name, default)
    if not isinstance(value, bool):
        raise ValueError(f"session query {name} must be a boolean")
    return value


def _query_text_list(
        request: Any, name: str, default: tuple[str, ...],
        choices: Optional[frozenset[str]]) -> list[str]:
    values = _query_value(request, name, default)
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise ValueError(f"session query {name} must be a bounded list")
    result: list[str] = []
    for value in values:
        text = _exact_wire_text(
            value, f"session query {name}", MAX_SESSION_PROVIDER_BYTES,
            required=True)
        if choices is not None and text not in choices:
            raise ValueError(f"session query {name} contains an unsupported value")
        if text not in result:
            result.append(text)
    return result


def thread_observation(
        result: Mapping[str, Any], expected_id: str) -> RuntimeSessionObservation:
    """Validate the stable ``thread/read`` summary without adopting the thread."""
    thread = result.get("thread")
    if not isinstance(thread, Mapping):
        raise ValueError("thread/read returned no thread object")
    observed_id = thread.get("id")
    if not isinstance(observed_id, str) or observed_id != expected_id:
        raise ValueError("thread/read returned a different thread id")
    status_type, flags = _thread_status(thread.get("status"), "thread/read")
    name_value = thread.get("name")
    if name_value is None:
        name_value = thread.get("preview")
    name = _display_wire_text(name_value, "thread/read name", 4_096)
    return RuntimeSessionObservation(observed_id, status_type, flags, name)


def _thread_status(value: Any, method: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{method} returned no structured status")
    status_type = value.get("type")
    if not isinstance(status_type, str) or status_type not in THREAD_STATUS_TYPES:
        raise ValueError(f"{method} returned an unsupported status")
    present = "activeFlags" in value
    raw_flags = value.get("activeFlags", [])
    if not isinstance(raw_flags, list) or len(raw_flags) > MAX_ACTIVE_FLAGS:
        raise ValueError(f"{method} returned invalid active flags")
    flags: list[str] = []
    for flag in raw_flags:
        flags.append(_exact_wire_text(
            flag, f"{method} active flag", 256, required=True))
    if status_type == "active" and not present:
        raise ValueError(f"active {method} status requires activeFlags")
    if status_type != "active" and present:
        raise ValueError(f"non-active {method} status cannot have activeFlags")
    return status_type, tuple(flags)


def _exact_wire_text(
        value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if required and not value:
        raise ValueError(f"{label} must not be empty")
    if len(encoded) > maximum:
        raise ValueError(f"{label} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _display_wire_text(value: Any, label: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    text = value.encode("utf-8", errors="replace").decode("utf-8")
    text = clean_text(text)
    text = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in text)
    return _bound(one_line(text), maximum)[0]


def _wire_timestamp(value: Mapping[str, Any], key: str) -> Optional[int]:
    if key not in value or value.get(key) is None:
        return None
    timestamp = value.get(key)
    if (isinstance(timestamp, bool) or not isinstance(timestamp, int)
            or timestamp < 0 or timestamp > 2 ** 63 - 1):
        raise ValueError(f"thread/list {key} must be a non-negative integer")
    return timestamp


def _wire_bool(value: Mapping[str, Any], key: str) -> Optional[bool]:
    if key not in value or value.get(key) is None:
        return None
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValueError(f"thread/list {key} must be a boolean")
    return result


def item_text(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    text, truncated, original = _bound(value.get(field, ""))
    result = {"item_id": _bounded_id(value.get("id", value.get("itemId", ""))),
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


def turn_outcome(value: Any) -> tuple[str, str, dict[str, Any]]:
    """Translate the provider's terminal status without ever guessing success."""
    turn = value if isinstance(value, Mapping) else {}
    status = str(turn.get("status", "")).strip()
    error = turn.get("error")
    reason = ""
    if isinstance(error, Mapping):
        reason = _bound(error.get("message", ""), 4096)[0]
    elif error not in (None, ""):
        reason = _bound(error, 4096)[0]
    if status == "completed" and not error:
        kind, state = "turn.completed", TurnState.SUCCEEDED
    elif status == "failed":
        kind, state = "turn.failed", TurnState.FAILED
        reason = reason or "Codex App Server turn failed"
    elif status == "interrupted":
        kind, state = "turn.interrupted", TurnState.INTERRUPTED
        reason = reason or "Codex App Server turn interrupted"
    else:
        kind, state = "turn.failed", TurnState.UNKNOWN
        label = status or "<missing>"
        reason = f"unsupported Codex App Server turn status: {label}"
    payload: dict[str, Any] = {"state": state, "provider_status": status}
    if reason:
        payload["reason"] = reason
    if error:
        payload["error"] = _bound(json_summary(error), 8192)[0]
    return kind, state, payload


def declared(capabilities: Any, name: str) -> bool:
    aliases = {"instruction_sources": "instructionSources"}
    if isinstance(capabilities, Mapping):
        return capabilities.get(name) is True or capabilities.get(aliases.get(name, "")) is True
    return isinstance(capabilities, list) and name in capabilities


def approval_payload(
        entry: ApprovalEntry, *, reason: str = "", decision: str = ""
        ) -> dict[str, Any]:
    payload = {
        "request_id": entry.request_id,
        "approval_type": entry.approval_type,
        "method": entry.method,
        "target": _bound(entry.target)[0],
        "received_at": entry.received_at,
        "expires_at": entry.expires_at,
        "available_decisions": list(entry.available_decisions),
    }
    if reason:
        payload["reason"] = _bound(reason, 2048)[0]
    if decision:
        payload["decision"] = _bound(decision, 128)[0]
    return payload


_REVIEW_TARGET_TYPES = frozenset({
    "uncommittedChanges", "baseBranch", "commit", "custom",
})
_REVIEW_DELIVERIES = frozenset({"inline"})
_REVIEW_TARGET_BYTES = 64 * 1024


def review_start_params(request: Any, thread_id_value: str) -> dict[str, Any]:
    """Build the current App Server ``review/start`` request fail-closed."""
    thread_id_value = _exact_wire_text(
        thread_id_value, "review threadId", 2048, required=True)
    explicit = _query_value(request, "target", None)
    target = (_review_target(explicit) if explicit is not None else
              _review_scope_target(request))
    delivery = _query_value(request, "delivery", None)
    if delivery not in (None, ""):
        delivery = _exact_wire_text(delivery, "review delivery", 32, required=True)
        if delivery not in _REVIEW_DELIVERIES:
            raise ValueError("only inline App Server reviews are supported")
    params = {"threadId": thread_id_value, "target": target}
    if delivery:
        params["delivery"] = delivery
    return params


def _review_scope_target(request: Any) -> dict[str, Any]:
    scope = _exact_wire_text(
        _query_value(request, "scope", "default"), "review scope", 4096,
        required=True)
    values = _review_scope_values(request)
    return _review_named_scope(scope, values)


def _review_scope_values(request: Any) -> tuple[str, ...]:
    values = _query_value(request, "scope_argv", ())
    if not isinstance(values, (list, tuple)) or len(values) > 2:
        raise ValueError("review scope arguments are invalid")
    if any(not isinstance(value, str) for value in values):
        raise ValueError("review scope arguments are invalid")
    return tuple(values)


def _review_named_scope(scope: str, values: tuple[str, ...]) -> dict[str, Any]:
    if scope == "default":
        if values:
            raise ValueError("default review does not accept arguments")
        return {"type": "uncommittedChanges"}
    if scope == "uncommitted":
        if values not in ((), ("--uncommitted",)):
            raise ValueError("uncommitted review arguments are invalid")
        return {"type": "uncommittedChanges"}
    prefix, separator, target_value = scope.partition(":")
    if prefix in {"base", "commit"} and separator:
        return _review_ref_target(prefix, target_value, values)
    if scope == "custom":
        return {"type": "custom", "instructions": _review_text(
            values[0] if values else "", "review instructions",
            _REVIEW_TARGET_BYTES, required=True)}
    raise ValueError("review scope is not supported by App Server")


def _review_ref_target(prefix: str, value: str,
                       values: tuple[str, ...]) -> dict[str, Any]:
    expected = (f"--{prefix}", value)
    if values and values != expected:
        raise ValueError(f"{prefix} review arguments do not match the scope")
    target_type, field, label = {
        "base": ("baseBranch", "branch", "review branch"),
        "commit": ("commit", "sha", "review commit"),
    }[prefix]
    return {"type": target_type, field: _review_scalar(value, label, 4096)}


def _review_target(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("review target must be an object")
    copied = dict(value)
    target_type = copied.get("type")
    if not isinstance(target_type, str) or target_type not in _REVIEW_TARGET_TYPES:
        raise ValueError("review target type is unsupported")
    allowed = {
        "uncommittedChanges": {"type"},
        "baseBranch": {"type", "branch"},
        "commit": {"type", "sha", "title"},
        "custom": {"type", "instructions"},
    }[target_type]
    if set(copied) - allowed:
        raise ValueError("review target contains unsupported fields")
    if target_type == "uncommittedChanges":
        if set(copied) != {"type"}:
            raise ValueError("uncommitted review target is malformed")
        return {"type": target_type}
    field = "branch" if target_type == "baseBranch" else (
        "sha" if target_type == "commit" else "instructions")
    maximum = _REVIEW_TARGET_BYTES if field == "instructions" else 4096
    result = {"type": target_type, field: (
        _review_text(copied.get(field), f"review {field}", maximum, required=True)
        if field == "instructions" else
        _review_scalar(copied.get(field), f"review {field}", maximum))}
    if target_type == "commit" and "title" in copied:
        title = copied["title"]
        result["title"] = None if title is None else _review_text(
            title, "review title", 4096)
    return result


def _review_scalar(value: Any, label: str, maximum: int) -> str:
    return _exact_wire_text(value, label, maximum, required=True)


def _review_text(value: Any, label: str, maximum: int,
                 *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if required and not value:
        raise ValueError(f"{label} must not be empty")
    if len(encoded) > maximum:
        raise ValueError(f"{label} is too long")
    if any(ord(char) < 32 and char not in "\\t\\n\\r" or ord(char) == 127
           for char in value):
        raise ValueError(f"{label} contains control characters")
    return value


def target_summary(params: Mapping[str, Any]) -> str:
    network = params.get("networkApprovalContext", params.get("network_approval_context"))
    if network:
        return f"network: {json_summary(network)}"
    command = params.get("command")
    if command:
        cwd = params.get("cwd")
        suffix = f"\ncwd: {json_summary(cwd)}" if cwd else ""
        return f"command: {json_summary(command)}{suffix}"
    for name in (
            "fileChanges", "changes", "permissions", "tool", "host", "url", "reason"):
        if params.get(name):
            return f"{name}: {json_summary(params[name])}"
    return "approval target not supplied"


def approval_decisions(params: Mapping[str, Any]) -> tuple[str, ...]:
    raw = params.get("availableDecisions", params.get("available_decisions"))
    if not isinstance(raw, list):
        return "allow", "allow_session", "deny", "cancel"
    result = []
    for value in raw:
        name = canonical_approval_decision(value)
        if name and name not in result:
            result.append(name)
    if "deny" not in result:
        result.append("deny")
    return tuple(result)


def approval_expiry(ttl: float) -> str:
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return expires.isoformat().replace("+00:00", "Z")


def canonical_approval_decision(value: Any) -> str:
    aliases = {
        "allow": "allow", "approve": "allow", "approved": "allow",
        "accept": "allow", "allow_session": "allow_session",
        "approved_for_session": "allow_session",
        "acceptforsession": "allow_session", "deny": "deny",
        "denied": "deny", "decline": "deny", "interrupt": "cancel",
        "cancel": "cancel", "abort": "cancel",
    }
    return aliases.get(str(value or "").strip().lower(), "")


def decision_result(entry: ApprovalEntry, value: Any) -> Optional[dict[str, Any]]:
    decision = canonical_approval_decision(_request_value(value, "decision", ""))
    if not decision:
        return None
    allow = decision == "allow"
    session = decision == "allow_session"
    deny = decision == "deny"
    cancel = decision == "cancel"
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
