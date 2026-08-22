"""Bidirectional Codex App Server JSON-RPC adapter for the TUI runtime."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Iterator, Mapping, Optional

from .tui_codex_support import (
    _bound, _id, _now, _request_value, _workspace_id,
)
from .tui_appserver_support import (
    APPROVAL_METHODS, DEFAULT_APPROVAL_TTL, DEFAULT_QUEUE_SIZE, DEFAULT_TIMEOUT,
    RUNTIME_NAME, ApprovalEntry, DefaultTransport, EventQueue, ItemProjector,
    Transport, approval_payload, bounded_mapping, decision_result, item_text,
    json_summary, make_runtime_info, record_server_info, request_key,
    session_summary, target_summary, thread_id, turn_id, valid_request_id,
)
from .tui_protocol import RUNTIME_PROTOCOL_VERSION, RuntimeEvent, TurnState
from .tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle, UnsupportedOperation
class CodexAppServerAdapter(ItemProjector):
    """Map App Server requests and notifications to provider-neutral events."""
    def __init__(self, cwd: str | os.PathLike[str], *, daemon_socket: Optional[str] = None,
                 transport: Optional[Transport] = None, maxsize: int = DEFAULT_QUEUE_SIZE,
                 request_timeout: float = DEFAULT_TIMEOUT,
                 approval_ttl: float = DEFAULT_APPROVAL_TTL):
        if request_timeout <= 0 or approval_ttl <= 0:
            raise ValueError("timeouts must be positive")
        self._cwd, self._daemon_socket = os.fspath(cwd), daemon_socket
        self._transport, self._queue = transport, EventQueue(maxsize)
        self._timeout, self._approval_ttl = request_timeout, approval_ttl
        self._lock, self._write_lock = threading.RLock(), threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._closed = self._stream_ended = self._initialized = False
        self._next_id, self._seq = 1, 0
        self._responses: dict[tuple[type, Any], queue.Queue[Any]] = {}
        self._approvals: dict[tuple[type, Any], ApprovalEntry] = {}
        self._approval_timers: dict[tuple[type, Any], threading.Timer] = {}
        self._resolved: set[tuple[type, Any]] = set()
        self._announced_threads: set[str] = set()
        self._command_output: dict[str, str] = {}
        self._usage: dict[str, Any] = {}
        self._active: Optional[TurnHandle] = None
        self._provider_turn_id = ""
        self._session = RuntimeSession(
            _id("ses"), "", _workspace_id(self._cwd), _now(), "ACTIVE")
        self.server_info: dict[str, Any] = {}

    @property
    def session(self) -> RuntimeSession:
        return self._session

    @property
    def pending_approvals(self) -> tuple[ApprovalEntry, ...]:
        with self._lock:
            return tuple(self._approvals.values())

    def runtime_info(self) -> RuntimeInfo:
        return self._runtime_info()

    def _runtime_info(self) -> RuntimeInfo:
        return make_runtime_info(
            self._initialized and not self._stream_ended and not self._closed, self.server_info)

    def initialize(self, request: Any = None) -> RuntimeInfo:
        self._ensure_open()
        workspace = str(_request_value(request, "workspace_id", "") or "")
        if workspace:
            self._session.workspace_id = workspace
        try:
            self._start_transport()
            result = self._call("initialize", {
                "clientInfo": {"name": "pi-batch", "version": "1"},
                "protocolVersion": RUNTIME_PROTOCOL_VERSION,
                "capabilities": {"experimentalApi": True},
            })
            self.server_info = record_server_info(result)
            if self._stream_ended: raise RuntimeError("connection lost during initialize")
        except Exception as exc:
            self.close()
            raise RuntimeError(f"codex app-server unavailable: {exc}") from exc
        self._initialized = True
        return self.runtime_info()

    def _start_transport(self) -> None:
        if self._reader is not None:
            return
        if self._transport is None:
            self._transport = DefaultTransport(self._daemon_socket, cwd=self._cwd)
        self._reader = threading.Thread(
            target=self._read_loop, name="codex-app-server-reader", daemon=True)
        self._reader.start()

    def _call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            if self._stream_ended:
                raise RuntimeError("connection lost")
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._responses[request_key(request_id)] = waiter
        try:
            self._send({"id": request_id, "method": method, "params": dict(params)})
            response = waiter.get(timeout=self._timeout)
        except queue.Empty as exc:
            with self._lock:
                self._responses.pop(request_key(request_id), None)
            raise RuntimeError(f"{method} timed out after {self._timeout:g}s") from exc
        except Exception:
            with self._lock:
                self._responses.pop(request_key(request_id), None)
            raise
        if isinstance(response, Exception):
            raise response
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(f"{method} failed: {error.get('code')} {error.get('message', '')}")
        result = response.get("result", {})
        return result if isinstance(result, Mapping) else {}

    def _send(self, value: Mapping[str, Any]) -> None:
        if self._transport is None:
            raise RuntimeError("transport is not connected")
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._write_lock:
            self._transport.send_line(line)

    def start_session(self, request: Any) -> RuntimeSession:
        params: dict[str, Any] = {}
        workspace = _request_value(request, "workspace", None)
        params["workspace" if workspace else "cwd"] = workspace or self._cwd
        return self._session_call("thread/start", params, request, "created")

    def resume_session(self, request: Any) -> RuntimeSession:
        provider_id = self._provider_id(request)
        return self._session_call(
            "thread/resume", {"threadId": provider_id}, request, "resumed")

    def fork_session(self, request: Any) -> RuntimeSession:
        provider_id = self._provider_id(request)
        return self._session_call(
            "thread/fork", {"threadId": provider_id}, request, "forked", force_new=True)

    def _provider_id(self, request: Any) -> str:
        provider_id = str(_request_value(request, "provider_session_id", "") or "")
        requested = str(_request_value(request, "session_id", "") or "")
        if not provider_id and requested == self._session.session_id:
            provider_id = self._session.provider_session_id
        if not provider_id:
            provider_id = requested
        if not provider_id:
            raise ValueError("provider_session_id is required")
        return provider_id

    def _session_call(self, method: str, params: Mapping[str, Any], request: Any,
                      mode: str, force_new: bool = False) -> RuntimeSession:
        self._require_initialized()
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("cannot replace an active runtime session")
            previous = self._session
            requested_id = str(_request_value(request, "session_id", "") or "")
            local_id = _id("ses") if force_new or not requested_id else requested_id
            workspace = str(_request_value(request, "workspace_id", "") or
                            previous.workspace_id)
            self._session = RuntimeSession(local_id, "", workspace, _now(), "ACTIVE")
        try:
            result = self._call(method, params)
            thread = result.get("thread", result)
            provider_id = thread_id(thread) or str(params.get("threadId", ""))
            self._session.provider_session_id = provider_id
            return self._session
        except Exception:
            with self._lock:
                self._session = previous
            raise

    def list_sessions(self, query: Any = None) -> list[dict[str, Any]]:
        self._require_initialized()
        params = dict(query) if isinstance(query, Mapping) else {}
        result = self._call("thread/list", params)
        data = result.get("data", result.get("threads", []))
        summaries = []
        for thread in data if isinstance(data, list) else []:
            if isinstance(thread, Mapping):
                summaries.append(session_summary(thread))
        return summaries

    def start_turn(self, request: Any) -> TurnHandle:
        self._require_initialized()
        prompt = str(_request_value(request, "prompt", request if isinstance(request, str) else ""))
        if not prompt.strip() or not self._session.provider_session_id:
            raise ValueError("prompt and active provider session are required")
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("an app-server turn is already running")
            handle = TurnHandle(
                self._session.session_id,
                str(_request_value(request, "run_id", "") or _id("run")),
                str(_request_value(request, "turn_id", "") or _id("turn")),
                str(_request_value(request, "actor_id", "agent_main") or "agent_main"), _now())
            self._active, self._provider_turn_id = handle, ""
        try:
            result = self._call("turn/start", {
                "threadId": self._session.provider_session_id,
                "input": [{"type": "text", "text": prompt}],
            })
            self._provider_turn_id = turn_id(result.get("turn", result))
            return handle
        except Exception:
            with self._lock:
                self._active = None
            raise

    def steer_turn(self, request: Any) -> None:
        handle = self._current_handle(request)
        text = str(_request_value(request, "text", "") or "")
        if not text.strip():
            raise ValueError("steer text must not be empty")
        self._call("turn/steer", {"threadId": self._session.provider_session_id,
                                  "expectedTurnId": self._provider_turn_id,
                                  "input": [{"type": "text", "text": text}]})
        if not handle.done:
            self._publish("turn.steered", {"text": _bound(text)[0]})

    def interrupt_turn(self, request: Any) -> None:
        handle = self._current_handle(request)
        self._call("turn/interrupt", {"threadId": self._session.provider_session_id,
                                      "turnId": self._provider_turn_id})
        if not handle.done:
            handle.state = TurnState.INTERRUPTING
            self._publish("turn.interrupting", {"state": handle.state})

    def _current_handle(self, request: Any) -> TurnHandle:
        requested = str(_request_value(request, "turn_id", "") or "")
        with self._lock:
            handle = self._active
            matches = handle is not None and requested in {handle.turn_id, self._provider_turn_id}
            if not matches or handle.done:
                raise ValueError("request does not match the active turn")
            return handle

    def resolve_approval(self, decision: Any) -> bool:
        request_id = _request_value(decision, "request_id", None)
        key = request_key(request_id)
        with self._lock:
            entry = self._approvals.get(key)
            if entry is None or time.monotonic() - entry.received_monotonic >= self._approval_ttl:
                expired = self._pop_approval(key)
                if expired:
                    self._publish("approval.expired", approval_payload(expired, "approval expired"))
                return False
            claimed = _request_value(decision, "approval_type",
                                     _request_value(decision, "request_type", ""))
            if claimed and claimed not in {entry.approval_type, entry.method}:
                return False
            result = decision_result(entry, decision)
            if result is None:
                return False
            try:
                self._send({"id": entry.request_id, "result": result})
            except Exception as exc:
                self._connection_lost(str(exc))
                return False
            self._pop_approval(key)
            self._resolved.add(key)
        self._publish("approval.resolved", {
            "request_id": entry.request_id, "approval_type": entry.approval_type,
            "decision": _request_value(decision, "decision", "")})
        return True

    def start_review(self, request: Any) -> None:
        raise UnsupportedOperation("review/start", "review")

    review_start = start_review

    def archive_session(self, request: Any) -> None:
        raise UnsupportedOperation("thread/archive")

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._transport.recv_line() if self._transport is not None else None
                if line is None or line == "":
                    raise EOFError(self._transport_reason())
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise ValueError("JSON-RPC line is not an object")
                self._dispatch(dict(value))
        except Exception as exc:
            if not self._closed:
                self._connection_lost(str(exc))

    def _dispatch(self, value: dict[str, Any]) -> None:
        if "id" in value and ("result" in value or "error" in value):
            with self._lock:
                waiter = self._responses.pop(request_key(value["id"]), None)
            if waiter is not None:
                waiter.put(value)
            return
        method = value.get("method")
        if not isinstance(method, str):
            self._unknown("<missing>", value)
        elif "id" in value:
            self._server_request(value["id"], method, value.get("params", {}))
        else:
            self._notification(method, value.get("params", {}))

    def _notification(self, method: str, raw_params: Any) -> None:
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        if method == "thread/started":
            self._thread_started(params)
        elif method == "turn/started":
            self._turn_started(params)
        elif method == "turn/completed":
            self._turn_completed(params)
        elif method in {"item/started", "item/completed"}:
            self._item(method.rsplit("/", 1)[-1], params.get("item", {}))
        elif method == "item/agentMessage/delta":
            self._publish("message.delta", item_text(params, "delta"))
        elif method == "item/commandExecution/outputDelta":
            self._command_delta(params)
        elif method == "item/fileChange/patchUpdated":
            self._file_change(params, "updated")
        elif method == "thread/tokenUsage/updated":
            self._usage = bounded_mapping(params.get("tokenUsage", params.get("usage", {})))
            self._publish("usage.updated", {"usage": self._usage})
        elif method == "warning":
            self._publish("runtime.warning", {"message": _bound(params.get("message", ""))[0]})
        elif method == "serverRequest/resolved":
            self._server_resolved(params)
        else:
            self._unknown(method, params)

    def _thread_started(self, params: Mapping[str, Any]) -> None:
        thread = params.get("thread", params)
        provider_id = thread_id(thread)
        if provider_id:
            self._session.provider_session_id = provider_id
        if provider_id in self._announced_threads:
            return
        self._announced_threads.add(provider_id)
        self._publish("session.created", {"provider_session_id": provider_id})

    def _turn_started(self, params: Mapping[str, Any]) -> None:
        handle = self._active
        if handle is None or handle.done or handle.state != TurnState.QUEUED:
            return
        self._provider_turn_id = turn_id(params.get("turn", params)) or self._provider_turn_id
        self._publish("turn.queued", {"state": TurnState.QUEUED})
        handle.state = TurnState.RUNNING
        self._publish("turn.started", {"state": TurnState.RUNNING})

    def _turn_completed(self, params: Mapping[str, Any]) -> None:
        handle = self._active
        if handle is None or handle.done:
            return
        turn = params.get("turn", params)
        provider_id = turn_id(turn) or self._provider_turn_id
        if self._provider_turn_id and provider_id != self._provider_turn_id:
            return
        usage = params.get("usage", self._usage)
        handle.state = TurnState.SUCCEEDED
        self._publish("turn.completed", {
            "state": handle.state, "provider_turn_id": provider_id,
            "usage": bounded_mapping(usage)})
        handle._done.set()

    def _server_request(self, request_id: Any, method: str, raw_params: Any) -> None:
        approval_type = APPROVAL_METHODS.get(method)
        if approval_type is None or not valid_request_id(request_id):
            self._unknown(method, {"request_id": request_id, "params": raw_params})
            return
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        summary = json_summary(params)
        entry = ApprovalEntry(request_id, method, approval_type, summary, _now(), time.monotonic())
        key = request_key(request_id)
        with self._lock:
            if key in self._approvals:
                self._publish("runtime.warning", {
                    "message": "duplicate approval request id", "request_id": request_id})
                return
            self._approvals[key] = entry
            timer = threading.Timer(self._approval_ttl, self._expire_approval, args=(key,))
            timer.daemon = True
            self._approval_timers[key] = timer
            timer.start()
        self._publish("approval.requested", approval_payload(entry, target_summary(params)))

    def _expire_approval(self, key: tuple[type, Any]) -> None:
        with self._lock:
            entry = self._pop_approval(key)
        if entry is not None and not self._closed:
            self._publish("approval.expired", approval_payload(entry, "approval TTL expired"))

    def _pop_approval(self, key: tuple[type, Any]) -> Optional[ApprovalEntry]:
        entry = self._approvals.pop(key, None)
        timer = self._approval_timers.pop(key, None)
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        return entry

    def _server_resolved(self, params: Mapping[str, Any]) -> None:
        request_id = params.get("requestId", params.get("request_id"))
        key = request_key(request_id)
        if key in self._resolved:
            return
        entry = self._pop_approval(key)
        self._resolved.add(key)
        self._publish("approval.resolved", {
            "request_id": request_id,
            "approval_type": entry.approval_type if entry else "unknown"})

    def _unknown(self, method: str, value: Any) -> None:
        self._publish("runtime.unknown_item", {
            "method": _bound(method, 256)[0], "raw": json_summary(value)})

    def _publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            handle = self._active
            event = RuntimeEvent(
                event_id=_id("evt"), seq=self._seq, occurred_at=_now(),
                workspace_id=self._session.workspace_id,
                session_id=self._session.session_id,
                run_id=handle.run_id if handle else "",
                turn_id=handle.turn_id if handle else "",
                actor_id=handle.actor_id if handle else "agent_main", kind=kind,
                payload=bounded_mapping(payload), extensions={"runtime": RUNTIME_NAME})
        self._queue.put(event)

    def _connection_lost(self, reason: str) -> None:
        with self._lock:
            if self._stream_ended:
                return
            self._stream_ended = True
            approvals = [self._pop_approval(key) for key in list(self._approvals)]
            waiters = list(self._responses.values())
            self._responses.clear()
            handle = self._active
            if handle is not None and not handle.done:
                handle.state = TurnState.DISCONNECTED
                handle._done.set()
        error = RuntimeError(f"connection lost: {reason}")
        for waiter in waiters:
            waiter.put(error)
        for entry in approvals:
            if entry is not None:
                self._publish("approval.expired", approval_payload(
                    entry, "connection lost, approval invalidated"))
        self._publish("runtime.error", {
            "reason": _bound(reason)[0],
            "message": "connection lost, approval invalidated"})
        self._queue.close()

    def _transport_reason(self) -> str:
        reason = getattr(self._transport, "unavailable_reason", None)
        return str(reason()) if callable(reason) else "transport EOF"

    def events(self) -> Iterator[RuntimeEvent]:
        while True:
            event = self._queue.get()
            if event is None:
                return
            yield event

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            approvals = list(self._approvals)
            waiters = list(self._responses.values())
            self._responses.clear()
            transport, reader, handle = self._transport, self._reader, self._active
            for key in approvals:
                self._pop_approval(key)
        for waiter in waiters:
            waiter.put(RuntimeError("app-server adapter closed"))
        if transport is not None:
            transport.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if handle is not None and not handle.done:
            handle.state = TurnState.UNKNOWN
            handle._done.set()
        self._queue.close()

    def _require_initialized(self) -> None:
        self._ensure_open()
        if not self._initialized:
            raise RuntimeError("initialize must be called first")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("app-server runtime is closed")
