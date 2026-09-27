"""Provider-neutral AgentRuntime adapter for Pi's stdio RPC mode."""

from __future__ import annotations

import os
import queue
import threading
from typing import Any, Iterator, Mapping, Optional

from .pi_version import PiVersionProbe, probe_pi_version
from .tui_appserver_support import (
    EventQueue, RuntimeEventStream, bounded_mapping, json_summary,
)
from .tui_codex_support import (
    _bound, _bounded_id, _id, _now, _request_value, _workspace_id,
)
from .tui_pi_rpc_support import (
    DEFAULT_QUEUE_SIZE, DEFAULT_TIMEOUT, MAX_RPC_LINE_BYTES, RUNTIME_NAME,
    DefaultPiRpcTransport, Transport, bounded_value, decode_line,
    encode_command, make_runtime_info, message_text, normalized_usage,
    session_path,
)
from .tui_protocol import RuntimeEvent, TurnState
from .tui_runtime import (
    ActiveSessionRequest, CompactSessionRequest, CompactionHandle, RuntimeInfo,
    RuntimeModelCatalog, RuntimeSession, RuntimeSessionState, RuntimeSessionStats,
    RuntimeThinkingLevels,
    SessionReconciliationError, SetModelRequest, SetThinkingLevelRequest,
    TurnHandle, UnsupportedOperation,
)


_DEFAULT_CALL_TIMEOUT = object()


class PiRpcAdapter:
    """Translate Pi 0.84 LF-JSONL RPC commands/events to RuntimeEvent."""

    def __init__(
            self, cwd: str | os.PathLike[str], *, agent: str = "pi",
            model: str = "", provider: str = "", session_name: str = "",
            transport: Optional[Transport] = None, timeout: float = DEFAULT_TIMEOUT,
            maxsize: int = DEFAULT_QUEUE_SIZE, spawn=None,
            max_line_bytes: int = MAX_RPC_LINE_BYTES,
            allow_persistent_settings: bool = False,
            version_probe: Optional[PiVersionProbe] = None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        if (isinstance(max_line_bytes, bool) or not isinstance(max_line_bytes, int)
                or max_line_bytes <= 0):
            raise ValueError("max_line_bytes must be a positive integer")
        if not isinstance(allow_persistent_settings, bool):
            raise ValueError("allow_persistent_settings must be a bool")
        if version_probe is not None and not isinstance(
                version_probe, PiVersionProbe):
            raise ValueError("version_probe must be a PiVersionProbe")
        self._cwd = os.path.realpath(os.path.abspath(os.fsdecode(cwd)))
        self._agent, self._model, self._provider = agent, model, provider
        self._session_name, self._spawn = session_name, spawn
        self._max_line_bytes, self._timeout = max_line_bytes, timeout
        self._allow_persistent_settings = allow_persistent_settings
        self._version_probe = version_probe
        self._transport, self._queue = transport, EventQueue(maxsize)
        self._lock, self._write_lock = threading.RLock(), threading.Lock()
        self._control_operation_lock = threading.RLock()
        self._reader: Optional[threading.Thread] = None
        self._closed = self._stream_ended = self._initialized = False
        self._next_id, self._seq = 1, 0
        self._responses: dict[str, queue.Queue[Any]] = {}
        self._expected_commands: dict[str, str] = {}
        self._active: Optional[TurnHandle] = None
        self._abort_thread: Optional[threading.Thread] = None
        self._abort_requested = self._turn_started = False
        self._failure_reason = self._stop_reason = ""
        self._current_message_id, self._usage_key = "", ""
        self._usage: dict[str, Any] = {}
        self._provider_state: dict[str, Any] = {}
        self._compaction: Optional[CompactionHandle] = None
        self._compaction_thread: Optional[threading.Thread] = None
        self._startup_claimed = False
        self._session = RuntimeSession(
            _id("ses"), "", _workspace_id(self._cwd), _now(), "ACTIVE")

    @property
    def session(self) -> RuntimeSession:
        return self._session

    @property
    def provider_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._provider_state)

    def runtime_info(self) -> RuntimeInfo:
        connected = self._initialized and not self._closed and not self._stream_ended
        session_settings = self._session_settings_safe()
        version = self._version_probe.reported_version if self._version_probe else ""
        return make_runtime_info(
            connected, session_settings=session_settings,
            persistent_settings=(
                self._allow_persistent_settings and not session_settings),
            runtime_version=version)

    def initialize(self, request: Any = None) -> RuntimeInfo:
        self._ensure_open()
        workspace = str(_request_value(request, "workspace_id", "") or "")
        if workspace:
            self._session.workspace_id = _bound(workspace, 4096)[0]
        try:
            self._start_transport()
            state = self._call("get_state")
            self._set_provider_state(self._validated_state(state))
            self._session.provider_session_id = session_path(self._provider_state)
            if self._stream_ended:
                raise RuntimeError("connection lost during get_state")
        except Exception as exc:
            self.close()
            raise RuntimeError(f"Pi RPC unavailable: {exc}") from exc
        self._initialized = True
        return self.runtime_info()

    def _start_transport(self) -> None:
        if self._reader is not None:
            return
        if self._transport is None:
            agent = self._agent
            if self._spawn is None:
                self._version_probe = probe_pi_version(
                    agent, cwd=self._cwd)
                agent = self._version_probe.executable
            elif self._version_probe is not None:
                agent = self._version_probe.executable
            self._transport = DefaultPiRpcTransport(
                self._cwd, agent=agent, model=self._model,
                provider=self._provider, session_name=self._session_name,
                spawn=self._spawn, max_line_bytes=self._max_line_bytes)
        elif self._version_probe is None:
            observed = getattr(self._transport, "pi_version_probe", None)
            if isinstance(observed, PiVersionProbe):
                self._version_probe = observed
        self._reader = threading.Thread(
            target=self._read_loop, name="pi-rpc-reader", daemon=True)
        self._reader.start()

    def _call(
            self, command: str, *, response_timeout: Any = _DEFAULT_CALL_TIMEOUT,
            **params: Any) -> dict[str, Any]:
        with self._lock:
            if self._stream_ended:
                raise RuntimeError("Pi RPC connection lost")
            request_id = f"pbatch-{self._next_id}"
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._responses[request_id] = waiter
            self._expected_commands[request_id] = command
        try:
            self._send({"id": request_id, "type": command, **params})
            timeout = (self._timeout if response_timeout is _DEFAULT_CALL_TIMEOUT
                       else response_timeout)
            response = waiter.get() if timeout is None else waiter.get(timeout=timeout)
        except queue.Empty as exc:
            self._forget_waiter(request_id)
            raise RuntimeError(
                f"Pi RPC {command} timed out after {timeout:g}s") from exc
        except Exception:
            self._forget_waiter(request_id)
            raise
        if isinstance(response, Exception):
            raise response
        return self._response_data(response, command)

    def _forget_waiter(self, request_id: str) -> None:
        with self._lock:
            self._responses.pop(request_id, None)
            self._expected_commands.pop(request_id, None)

    def _response_data(
            self, response: Mapping[str, Any], command: str) -> dict[str, Any]:
        if response.get("command") != command:
            raise RuntimeError(f"Pi RPC response command mismatch for {command}")
        if response.get("success") is not True:
            detail = _bound(response.get("error", "command rejected"), 4096)[0]
            raise RuntimeError(f"Pi RPC {command} failed: {detail}")
        data = response.get("data", {})
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            raise RuntimeError(f"Pi RPC {command} returned invalid data")
        return dict(data)

    def _send(self, value: Mapping[str, Any]) -> None:
        transport = self._transport
        if transport is None:
            raise RuntimeError("Pi RPC transport is not connected")
        line = encode_command(value, self._max_line_bytes)
        with self._write_lock:
            transport.send_line(line)

    def start_session(self, request: Any) -> RuntimeSession:
        with self._control_operation_lock:
            return self._start_session_locked(request)

    def _start_session_locked(self, request: Any) -> RuntimeSession:
        self._require_initialized()
        self._ensure_session_replaceable()
        first = not self._startup_claimed
        if first:
            # Pi emits session_info_changed before set_session_name replies.
            # Bind the startup session before that event enters the idle queue.
            self._bind_startup_session(request)
        else:
            self._session_command("new_session")
        name = self._request_text(request, "name", maximum=4096)
        if name:
            self._call("set_session_name", name=name)
        state = self._validated_state(self._call("get_state"))
        event = "session.created"
        session = self._adopt_state(state, request, event)
        self._startup_claimed = True
        return session

    def _bind_startup_session(self, request: Any) -> None:
        requested = self._request_text(request, "session_id", maximum=4096)
        workspace = self._request_text(request, "workspace_id", maximum=4096)
        with self._lock:
            self._session.session_id = requested or self._session.session_id
            self._session.workspace_id = workspace or self._session.workspace_id

    def _session_command(self, command: str, **params: Any) -> dict[str, Any]:
        result = self._call(command, **params)
        if result.get("cancelled") is True:
            raise RuntimeError(f"Pi RPC {command} was cancelled")
        return result

    def resume_session(self, request: Any) -> RuntimeSession:
        with self._control_operation_lock:
            return self._resume_session_locked(request)

    def _resume_session_locked(self, request: Any) -> RuntimeSession:
        self._require_initialized()
        self._ensure_session_replaceable()
        provider_id = self._provider_id(request)
        state = self._switch_and_read(provider_id)
        self._startup_claimed = True
        return self._adopt_state(state, request, "session.resumed")

    def _switch_and_read(self, provider_id: str) -> dict[str, Any]:
        self._session_command("switch_session", sessionPath=provider_id)
        state = self._validated_state(self._call("get_state"))
        observed = session_path(state)
        if observed != provider_id:
            raise SessionReconciliationError(
                "mismatched", "Pi switch_session get_state returned a different sessionFile")
        return state

    def fork_session(self, request: Any) -> RuntimeSession:
        with self._control_operation_lock:
            return self._fork_session_locked(request)

    def _fork_session_locked(self, request: Any) -> RuntimeSession:
        self._require_initialized()
        self._ensure_session_replaceable()
        source = self._provider_id(request)
        self._switch_and_read(source)
        self._session_command("clone")
        state = self._validated_state(self._call("get_state"))
        created = session_path(state)
        if created == source:
            raise SessionReconciliationError(
                "mismatched", "Pi clone did not create a new sessionFile")
        self._startup_claimed = True
        return self._adopt_state(
            state, request, "session.forked", parent_provider_session_id=source)

    def _adopt_state(
            self, state: Mapping[str, Any], request: Any, event: str,
            **extra: Any) -> RuntimeSession:
        provider_id = session_path(state)
        requested = self._request_text(request, "session_id", maximum=4096)
        workspace = self._request_text(request, "workspace_id", maximum=4096)
        self._set_provider_state(state)
        local_id = (self._session.session_id
                    if event == "session.created" and not self._startup_claimed
                    else _id("ses"))
        self._session = RuntimeSession(
            requested or local_id, provider_id,
            workspace or self._session.workspace_id, _now(), "ACTIVE")
        with self._lock:
            if self._active is not None and self._active.done:
                self._active = None
        payload = {"provider_session_id": provider_id, **extra}
        self._publish(event, payload)
        return self._session

    def _validated_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, Mapping):
            raise RuntimeError("Pi get_state returned invalid data")
        copied = dict(state)
        session_path(copied)
        return copied

    def _set_provider_state(self, state: Mapping[str, Any]) -> None:
        with self._lock:
            self._provider_state = dict(state)

    def _provider_id(self, request: Any) -> str:
        provider = self._request_text(
            request, "provider_session_id", maximum=16 * 1024)
        requested = self._request_text(request, "session_id", maximum=16 * 1024)
        if not provider and requested == self._session.session_id:
            provider = self._session.provider_session_id
        provider = provider or requested
        if not provider:
            raise ValueError("provider_session_id is required")
        return provider

    def read_session(self, request: Any) -> Any:
        raise UnsupportedOperation("read_session", "session_read")

    def list_sessions(self, request: Any = None) -> Any:
        raise UnsupportedOperation("list_sessions", "session_list")

    def get_session_state(
            self, request: ActiveSessionRequest | Any = None) -> RuntimeSessionState:
        self._require_initialized()
        self._check_active_session(request)
        return self._read_normalized_state()

    def get_session_stats(
            self, request: ActiveSessionRequest | Any = None) -> RuntimeSessionStats:
        from .tui_pi_controls import normalize_session_stats

        self._require_initialized()
        self._check_active_session(request)
        raw = self._call("get_session_stats")
        return normalize_session_stats(
            raw, self._session.session_id, self._session.provider_session_id)

    def get_available_models(
            self, request: ActiveSessionRequest | Any = None
            ) -> RuntimeModelCatalog:
        from .tui_pi_controls import normalize_model_catalog

        self._require_initialized()
        self._check_active_session(request)
        return normalize_model_catalog(
            self._call("get_available_models"), self._session.session_id)

    def get_available_thinking_levels(
            self, request: ActiveSessionRequest | Any = None
            ) -> RuntimeThinkingLevels:
        from .tui_pi_controls import normalize_thinking_levels

        self._require_initialized()
        self._check_active_session(request)
        return normalize_thinking_levels(
            self._call("get_available_thinking_levels"),
            self._session.session_id)

    def set_model(self, request: SetModelRequest | Any) -> RuntimeSessionState:
        with self._control_operation_lock:
            return self._set_model_locked(request)

    def _set_model_locked(
            self, request: SetModelRequest | Any) -> RuntimeSessionState:
        self._require_settings_mutation("set_model", "model_select")
        self._check_active_session(request)
        self._ensure_control_idle()
        provider = self._request_text(request, "provider", maximum=4096)
        model_id = self._request_text(request, "model_id", maximum=4096)
        if not provider.strip() or not model_id.strip():
            raise ValueError("provider and model_id are required")
        self._require_available_model(provider, model_id)
        self._call("set_model", provider=provider, modelId=model_id)
        state = self._read_normalized_state()
        if (state.model is None or state.model.provider != provider
                or state.model.model_id != model_id):
            raise SessionReconciliationError(
                "mismatched", "Pi model readback did not match the request")
        self._refresh_thinking_levels_after_model_change()
        return state

    def _refresh_thinking_levels_after_model_change(self) -> None:
        try:
            self.get_available_thinking_levels(
                ActiveSessionRequest(self._session.session_id))
        except Exception as exc:
            self._publish("runtime.warning", {
                "operation": "get_available_thinking_levels",
                "model_change_confirmed": True,
                "text": (
                    "Pi model change was confirmed, but thinking-level "
                    f"refresh failed: {_bound(exc, 4096)[0]}"),
            })

    def set_thinking_level(
            self, request: SetThinkingLevelRequest | Any) -> RuntimeSessionState:
        with self._control_operation_lock:
            return self._set_thinking_level_locked(request)

    def _set_thinking_level_locked(
            self, request: SetThinkingLevelRequest | Any
            ) -> RuntimeSessionState:
        self._require_settings_mutation(
            "set_thinking_level", "thinking_select")
        self._check_active_session(request)
        self._ensure_control_idle()
        level = self._request_text(request, "level", maximum=256)
        if not level.strip():
            raise ValueError("thinking level is required")
        self._require_available_thinking_level(level)
        self._call("set_thinking_level", level=level)
        return self._read_normalized_state()

    def _require_available_model(self, provider: str, model_id: str) -> None:
        catalog = self.get_available_models(
            ActiveSessionRequest(self._session.session_id))
        if not any(
                item.provider == provider and item.model_id == model_id
                for item in catalog.models):
            raise ValueError(
                f"model is not available: {provider}/{model_id}")

    def _require_available_thinking_level(self, level: str) -> None:
        choices = self.get_available_thinking_levels(
            ActiveSessionRequest(self._session.session_id))
        if level not in choices.levels:
            raise ValueError(f"thinking level is not available: {level}")

    def start_compaction(
            self, request: CompactSessionRequest | Any = None) -> CompactionHandle:
        with self._control_operation_lock:
            return self._start_compaction_locked(request)

    def _start_compaction_locked(
            self, request: CompactSessionRequest | Any = None
            ) -> CompactionHandle:
        self._require_initialized()
        self._check_active_session(request)
        instructions = self._request_text(
            request, "instructions", maximum=512 * 1024)
        with self._lock:
            self._ensure_control_idle_locked()
            handle = CompactionHandle(
                _id("compact"), self._session.session_id, _now())
            self._compaction = handle
            worker = threading.Thread(
                target=self._run_compaction, args=(handle, instructions),
                name="pi-rpc-compaction", daemon=True)
            self._compaction_thread = worker
            worker.start()
        return handle

    def _run_compaction(
            self, handle: CompactionHandle, instructions: str) -> None:
        from .tui_pi_controls import normalize_compaction_result

        params = {"customInstructions": instructions} if instructions else {}
        try:
            raw = self._call("compact", response_timeout=None, **params)
            result = normalize_compaction_result(raw, handle.session_id)
        except BaseException as exc:
            handle._set_exception(exc)
        else:
            handle._set_result(result)

    def _read_normalized_state(self) -> RuntimeSessionState:
        from .tui_pi_controls import normalize_session_state

        raw = self._validated_state(self._call("get_state"))
        expected = self._session.provider_session_id
        if expected and session_path(raw) != expected:
            raise SessionReconciliationError(
                "mismatched", "Pi get_state returned a different sessionFile")
        state = normalize_session_state(
            raw, self._session.session_id, expected or session_path(raw))
        self._set_provider_state(raw)
        return state

    def _check_active_session(self, request: Any) -> None:
        requested = self._request_text(request, "session_id", maximum=4096)
        if requested and requested != self._session.session_id:
            raise ValueError("request does not match the active session")

    def _require_settings_mutation(
            self, operation: str, capability: str) -> None:
        self._require_initialized()
        if not (self._session_settings_safe()
                or self._allow_persistent_settings):
            raise UnsupportedOperation(operation, capability)

    def _session_settings_safe(self) -> bool:
        probe = self._version_probe
        return bool(
            isinstance(probe, PiVersionProbe)
            and probe.supports_session_scoped_settings)

    def _ensure_control_idle(self) -> None:
        with self._lock:
            self._ensure_control_idle_locked()

    def _ensure_control_idle_locked(self) -> None:
        if self._active is not None and not self._active.done:
            raise RuntimeError("control operation cannot run during an active Pi turn")
        if self._compaction is not None and not self._compaction.done:
            raise RuntimeError("Pi session compaction is already running")

    def start_turn(self, request: Any) -> TurnHandle:
        with self._control_operation_lock:
            return self._start_turn_locked(request)

    def _start_turn_locked(self, request: Any) -> TurnHandle:
        self._require_initialized()
        self._ensure_no_compaction()
        prompt = self._request_text(request, "prompt", maximum=512 * 1024)
        if not prompt.strip() or not self._session.provider_session_id:
            raise ValueError("prompt and active Pi session are required")
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("a Pi RPC turn is already running")
            handle = TurnHandle(
                self._session.session_id,
                self._request_text(request, "run_id", maximum=4096) or _id("run"),
                self._request_text(request, "turn_id", maximum=4096) or _id("turn"),
                self._request_text(request, "actor_id", maximum=4096) or "agent_main",
                _now())
            self._reset_turn(handle)
        self._publish("turn.queued", {"state": TurnState.QUEUED})
        try:
            self._call("prompt", message=prompt)
        except Exception as exc:
            self._reject_start(handle, exc)
            raise
        return handle

    def _reset_turn(self, handle: TurnHandle) -> None:
        self._active, self._abort_requested = handle, False
        self._turn_started = False
        self._failure_reason = self._stop_reason = ""
        self._current_message_id, self._usage_key = "", ""
        self._usage = {}

    def _reject_start(self, handle: TurnHandle, exc: Exception) -> None:
        with self._lock:
            if self._active is not handle or handle.done:
                return
            handle.state = TurnState.FAILED
            reason = _bound(exc, 4096)[0]
            self._publish("turn.failed", {"state": handle.state, "reason": reason})
            handle._done.set()

    def steer_turn(self, request: Any) -> None:
        self._queue_message("steer", request)

    def follow_up(self, request: Any) -> None:
        self._queue_message("follow_up", request)

    def _queue_message(self, command: str, request: Any) -> None:
        self._current_handle(request)
        with self._lock:
            if self._abort_requested:
                raise RuntimeError("cannot queue Pi messages after abort was requested")
        text = self._request_text(request, "text", maximum=512 * 1024)
        if not text.strip():
            raise ValueError(f"{command} text must not be empty")
        self._call(command, message=text)
        self._publish("turn.steered", {
            "text": _bound(text)[0], "queue": command})

    def interrupt_turn(self, request: Any) -> None:
        handle = self._current_handle(request)
        state = self._read_normalized_state()
        if state.pending_message_count is None:
            raise UnsupportedOperation("interrupt_turn", "queue_clear")
        if state.pending_message_count:
            raise UnsupportedOperation("interrupt_turn", "queue_clear")
        with self._lock:
            previous = handle.state
            self._abort_requested = True
            handle.state = TurnState.INTERRUPTING
        self._publish("turn.interrupting", {"state": TurnState.INTERRUPTING})
        worker = threading.Thread(
            target=self._run_abort, args=(handle, previous),
            name="pi-rpc-abort", daemon=True)
        with self._lock:
            self._abort_thread = worker
        worker.start()

    def _run_abort(self, handle: TurnHandle, previous: str) -> None:
        try:
            self._call("abort", response_timeout=None)
        except Exception as exc:
            with self._lock:
                if self._active is handle and not handle.done:
                    self._abort_requested = False
                    handle.state = previous
            self._publish("runtime.error", {
                "text": _bound(exc, 4096)[0], "operation": "abort"})

    def _current_handle(self, request: Any) -> TurnHandle:
        requested = self._request_text(request, "turn_id", maximum=4096)
        with self._lock:
            handle = self._active
            if handle is None or handle.done:
                raise ValueError("there is no active Pi RPC turn")
            if requested and requested != handle.turn_id:
                raise ValueError("request does not match the active turn")
            return handle

    def resolve_approval(self, decision: Any) -> None:
        raise UnsupportedOperation("resolve_approval", "typed_approvals")

    def _request_text(self, request: Any, name: str, *, maximum: int) -> str:
        fallback = request if name == "prompt" and isinstance(request, str) else ""
        value = _request_value(request, name, fallback)
        if value in (None, ""):
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if "\x00" in value or len(value.encode("utf-8")) > maximum:
            raise ValueError(f"{name} exceeds its safe wire boundary")
        return value

    def _read_loop(self) -> None:
        try:
            while True:
                transport = self._transport
                line = transport.recv_line() if transport is not None else None
                if line is None:
                    self._connection_lost(self._transport_reason())
                    return
                self._route(decode_line(line, self._max_line_bytes))
        except Exception as exc:
            self._connection_lost(str(exc))

    def _route(self, value: Mapping[str, Any]) -> None:
        event_type = value.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Pi RPC object is missing a string type")
        if event_type == "response":
            self._route_response(value)
            return
        self._dispatch_event(event_type, value)

    def _route_response(self, value: Mapping[str, Any]) -> None:
        request_id = value.get("id")
        if not isinstance(request_id, str):
            raise ValueError("Pi RPC response is missing a string id")
        with self._lock:
            waiter = self._responses.pop(request_id, None)
            self._expected_commands.pop(request_id, None)
        if waiter is None:
            self._publish("runtime.warning", {
                "text": "unmatched Pi RPC response", "request_id": _bounded_id(request_id)})
            return
        waiter.put(dict(value))

    def _dispatch_event(self, event_type: str, value: Mapping[str, Any]) -> None:
        handlers = {
            "agent_start": self._agent_start,
            "agent_end": self._agent_end,
            "agent_settled": self._agent_settled,
            "turn_start": self._provider_turn_start,
            "turn_end": self._provider_turn_end,
            "message_start": self._message_start,
            "message_update": self._message_update,
            "message_end": self._message_end,
            "tool_execution_start": self._tool_start,
            "tool_execution_update": self._tool_update,
            "tool_execution_end": self._tool_end,
            "extension_error": self._extension_error,
            "queue_update": self._queue_update,
        }
        handler = handlers.get(event_type)
        if handler is None:
            self._unknown(event_type, value)
        else:
            handler(value)

    def _agent_start(self, value: Mapping[str, Any]) -> None:
        self._ensure_turn_started()

    def _ensure_turn_started(self) -> None:
        handle = self._active
        if handle is None or handle.done or self._turn_started:
            return
        self._turn_started = True
        handle.state = TurnState.RUNNING
        self._publish("turn.started", {"state": handle.state})

    def _agent_end(self, value: Mapping[str, Any]) -> None:
        self._ensure_turn_started()
        messages = value.get("messages", [])
        if isinstance(messages, list):
            for message in messages:
                self._inspect_assistant(message)
        self._publish("actor.status", {
            "phase": "agent_end", "will_retry": value.get("willRetry") is True})

    def _provider_turn_start(self, value: Mapping[str, Any]) -> None:
        self._ensure_turn_started()
        self._publish("actor.status", {"phase": "provider_turn_start"})

    def _provider_turn_end(self, value: Mapping[str, Any]) -> None:
        self._inspect_assistant(value.get("message"))
        self._publish("actor.status", {"phase": "provider_turn_end"})

    def _message_start(self, value: Mapping[str, Any]) -> None:
        message = value.get("message")
        if not self._is_assistant(message):
            return
        self._ensure_turn_started()
        message_id = self._message_id(message, reuse_current=False)
        self._current_message_id = message_id
        self._publish("message.started", {"item_id": message_id, "text": ""})

    def _message_update(self, value: Mapping[str, Any]) -> None:
        self._update_usage(value.get("usage"))
        delta = value.get("assistantMessageEvent")
        if not isinstance(delta, Mapping) or delta.get("type") != "text_delta":
            return
        text = _bound(delta.get("delta", ""))[0]
        if text:
            self._publish("message.delta", {
                "item_id": self._current_message_id, "text": text})

    def _message_end(self, value: Mapping[str, Any]) -> None:
        message = value.get("message")
        if not self._is_assistant(message):
            return
        self._inspect_assistant(message)
        message_id = self._current_message_id or self._message_id(message)
        self._publish("message.completed", {
            "item_id": message_id, "text": message_text(message),
            "stop_reason": self._stop_reason})

    def _message_id(self, message: Any, *, reuse_current: bool = True) -> str:
        raw = message.get("id", "") if isinstance(message, Mapping) else ""
        if raw:
            return _bounded_id(raw)
        if reuse_current and self._current_message_id:
            return self._current_message_id
        return _bounded_id(_id("msg"))

    def _is_assistant(self, message: Any) -> bool:
        return isinstance(message, Mapping) and message.get("role") == "assistant"

    def _inspect_assistant(self, message: Any) -> None:
        if not self._is_assistant(message):
            return
        self._update_usage(message.get("usage"))
        reason = str(message.get("stopReason", "") or "")
        self._stop_reason = _bound(reason, 256)[0]
        self._failure_reason = ""
        if reason == "error":
            detail = message.get("errorMessage", "Pi assistant message failed")
            self._failure_reason = _bound(detail, 4096)[0]

    def _tool_start(self, value: Mapping[str, Any]) -> None:
        self._publish("tool.started", self._tool_payload(value, "args"))

    def _tool_update(self, value: Mapping[str, Any]) -> None:
        payload = self._tool_payload(value, "partialResult")
        payload["provider_event"] = "tool_execution_update"
        self._publish("runtime.unknown_item", payload)

    def _tool_end(self, value: Mapping[str, Any]) -> None:
        payload = self._tool_payload(value, "result")
        failed = value.get("isError") is True
        self._publish("tool.failed" if failed else "tool.completed", payload)

    def _tool_payload(self, value: Mapping[str, Any], detail: str) -> dict[str, Any]:
        return {
            "tool_call_id": _bounded_id(value.get("toolCallId", "")),
            "tool_name": _bound(value.get("toolName", ""), 1024)[0],
            detail: bounded_value(value.get(detail)),
        }

    def _update_usage(self, value: Any) -> None:
        usage = normalized_usage(value)
        if not usage:
            return
        key = json_summary(usage)
        self._usage = usage
        if key == self._usage_key:
            return
        self._usage_key = key
        self._publish("usage.updated", {"usage": usage})

    def _extension_error(self, value: Mapping[str, Any]) -> None:
        self._publish("runtime.error", {
            "text": _bound(value.get("error", "Pi extension error"), 4096)[0],
            "extension_path": _bound(value.get("extensionPath", ""), 4096)[0],
            "event": _bound(value.get("event", ""), 512)[0]})

    def _queue_update(self, value: Mapping[str, Any]) -> None:
        self._publish("runtime.unknown_item", {
            "provider_event": "queue_update",
            "raw": json_summary(bounded_value(value))})

    def _agent_settled(self, value: Mapping[str, Any]) -> None:
        handle = self._active
        if handle is None or handle.done:
            return
        self._ensure_turn_started()
        if self._abort_requested or self._stop_reason == "aborted":
            kind, state, reason = "turn.interrupted", TurnState.INTERRUPTED, "aborted"
        elif self._failure_reason:
            kind, state, reason = "turn.failed", TurnState.FAILED, self._failure_reason
        else:
            kind, state, reason = "turn.completed", TurnState.SUCCEEDED, ""
        handle.state = state
        payload: dict[str, Any] = {"state": state, "usage": self._usage}
        if reason:
            payload["reason"] = reason
        self._publish(kind, payload)
        handle._done.set()

    def _unknown(self, event_type: str, value: Mapping[str, Any]) -> None:
        self._publish("runtime.unknown_item", {
            "provider_event": _bound(event_type, 256)[0],
            "raw": json_summary(bounded_value(value))})

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
                actor_id=handle.actor_id if handle else "agent_main",
                kind=kind, payload=bounded_mapping(payload),
                extensions={"runtime": RUNTIME_NAME})
        self._queue.put(event)

    def _connection_lost(self, reason: str) -> None:
        with self._lock:
            if self._closed or self._stream_ended:
                return
            self._stream_ended = True
            waiters = list(self._responses.values())
            self._responses.clear()
            self._expected_commands.clear()
            handle = self._active
            compaction = self._compaction
            if handle is not None and not handle.done:
                handle.state = TurnState.DISCONNECTED
                handle._done.set()
        error = RuntimeError(f"Pi RPC connection lost: {reason}")
        if compaction is not None and not compaction.done:
            compaction._set_exception(error, state="UNKNOWN")
        for waiter in waiters:
            waiter.put(error)
        self._publish("runtime.error", {
            "text": _bound(reason, 4096)[0], "reason": "Pi RPC connection lost"})
        self._queue.close()

    def _transport_reason(self) -> str:
        reason = getattr(self._transport, "unavailable_reason", None)
        return str(reason()) if callable(reason) else "transport EOF"

    def events(self) -> Iterator[RuntimeEvent]:
        return RuntimeEventStream(self._queue)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            waiters = list(self._responses.values())
            self._responses.clear()
            self._expected_commands.clear()
            transport, reader, handle = self._transport, self._reader, self._active
            compaction, compaction_thread = (
                self._compaction, self._compaction_thread)
            abort_thread = self._abort_thread
        closed_error = RuntimeError("Pi RPC adapter closed")
        if compaction is not None and not compaction.done:
            compaction._set_exception(closed_error, state="UNKNOWN")
        for waiter in waiters:
            waiter.put(closed_error)
        if transport is not None:
            transport.close()
        self._queue.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if (compaction_thread is not None
                and compaction_thread is not threading.current_thread()):
            compaction_thread.join(timeout=1.0)
        if abort_thread is not None and abort_thread is not threading.current_thread():
            abort_thread.join(timeout=1.0)
        if handle is not None and not handle.done:
            handle.state = TurnState.UNKNOWN
            handle._done.set()

    def _ensure_session_replaceable(self) -> None:
        if self._active is not None and not self._active.done:
            raise RuntimeError("cannot replace a session during an active Pi turn")
        self._ensure_no_compaction()

    def _ensure_no_compaction(self) -> None:
        with self._lock:
            if self._compaction is not None and not self._compaction.done:
                raise RuntimeError("cannot continue while Pi session compaction is running")

    def _require_initialized(self) -> None:
        self._ensure_open()
        if not self._initialized:
            raise RuntimeError("initialize must be called first")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Pi RPC runtime is closed")
