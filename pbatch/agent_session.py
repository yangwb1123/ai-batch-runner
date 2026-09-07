"""Small provider-neutral Python session API built on the TUI runtime.

The shape is inspired by pi's ``AgentSession`` lifecycle, but this module is
not a Python port of pi's Node API.  It exposes only operations that the
selected :class:`~pbatch.tui_runtime.AgentRuntime` can truthfully perform.
"""

from __future__ import annotations

import os
import shlex
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Union

from .tui_protocol import RuntimeEvent
from .tui_runtime import (
    ActiveSessionRequest,
    AgentRuntime,
    CompactSessionRequest,
    CompactionHandle,
    ForkSessionRequest,
    InitializeRequest,
    InterruptRequest,
    RuntimeInfo,
    RuntimeCompactionResult,
    RuntimeModelCatalog,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeSessionStats,
    RuntimeThinkingLevels,
    SetModelRequest,
    SetThinkingLevelRequest,
    StartSessionRequest,
    StartTurnRequest,
    SteerRequest,
    TurnHandle,
    UnsupportedOperation,
)


TERMINAL_EVENT_KINDS = frozenset({
    "turn.completed", "turn.failed", "turn.interrupted",
})
EventListener = Callable[[RuntimeEvent], None]


@dataclass(frozen=True)
class AgentSessionOptions:
    """Construction settings for :func:`create_agent_session`.

    ``runtime`` selects an adapter family (``auto``, ``pi``, ``codex``, or
    ``legacy``).  In auto mode the executable basename is used; no provider is
    inferred from model names. ``allow_persistent_settings`` is a dangerous
    compatibility opt-in for Pi versions older than 0.84.3; current stable Pi
    is detected automatically and keeps model/thinking changes session-scoped.
    """

    cwd: str = ""
    agent: str = ""
    model: str = ""
    provider: str = ""
    memory_mode: str = "auto"
    timeout: int = 0
    session_name: str = "sdk"
    session_id: str = ""
    workspace_id: str = ""
    runtime: str = "auto"
    allow_persistent_settings: bool = False


@dataclass(frozen=True)
class QueueReceipt:
    """Acknowledgement for a message queued into an already-running turn."""

    accepted: bool
    behavior: str
    session_id: str
    turn_id: str


class CompactionTimeout(TimeoutError):
    """Local wait expired; the provider operation remains authoritative."""

    def __init__(self, handle: CompactionHandle):
        self.handle = handle
        super().__init__(
            "timed out waiting for compaction; provider operation is still running")


@dataclass(frozen=True)
class TurnResult:
    """Terminal result of one directly-started turn."""

    session_id: str
    run_id: str
    turn_id: str
    state: str
    text: str
    terminal_event: RuntimeEvent
    events: tuple[RuntimeEvent, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.terminal_event.kind == "turn.completed"

    @property
    def reason(self) -> str:
        return str(self.terminal_event.payload.get("reason", "") or "")


PromptOutcome = Union[TurnResult, QueueReceipt]


@dataclass
class _TurnTracker:
    handle: TurnHandle
    events: deque[RuntimeEvent]
    terminal: Optional[RuntimeEvent] = None
    completed_text: str = ""
    completed_seen: bool = False
    delta_text: str = ""

    def consume(self, event: RuntimeEvent) -> None:
        self.events.append(event)
        text = str(event.payload.get("text", "") or "")
        if event.kind == "message.completed":
            self.completed_text = text
            self.completed_seen = True
        elif event.kind == "message.delta":
            self.delta_text += text
        if event.kind in TERMINAL_EVENT_KINDS:
            self.terminal = event

    @property
    def text(self) -> str:
        return self.completed_text if self.completed_seen else self.delta_text


class AgentSession:
    """Synchronous session facade over a provider-neutral runtime event stream."""

    def __init__(self, runtime: AgentRuntime, options: AgentSessionOptions):
        self._runtime = runtime
        self._options = options
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._operation_lock = threading.RLock()
        self._listeners: dict[int, EventListener] = {}
        self._next_listener = 1
        self._active: Optional[_TurnTracker] = None
        self._compaction: Optional[CompactionHandle] = None
        self._closed = False
        self._stream_closed = False
        self._stream_error: Optional[BaseException] = None
        try:
            self._info = self._initialize_runtime()
            self._session = self._start_initial_session()
        except Exception:
            self._runtime.close()
            raise
        self._event_thread = threading.Thread(
            target=self._pump_events, name="agent-session-events", daemon=True)
        self._event_thread.start()

    def _initialize_runtime(self) -> RuntimeInfo:
        info = self._runtime.initialize(InitializeRequest(
            workspace_id=self._options.workspace_id))
        if not isinstance(info, RuntimeInfo):
            raise TypeError("runtime.initialize() must return RuntimeInfo")
        return info

    def _start_initial_session(self) -> RuntimeSession:
        session = self._runtime.start_session(StartSessionRequest(
            workspace_id=self._options.workspace_id,
            name=self._options.session_name,
            session_id=self._options.session_id or _new_id("ses"),
        ))
        return _checked_session(session)

    @property
    def state(self) -> dict[str, Any]:
        """Return a detached snapshot; callers cannot mutate runtime state."""
        with self._lock:
            active = self._active
            compacting = self._compaction is not None and not self._compaction.done
            return {
                "closed": self._closed,
                "busy": active is not None or compacting,
                "compacting": compacting,
                "runtime": self._info.runtime,
                "session_id": self._session.session_id,
                "provider_session_id": self._session.provider_session_id,
                "workspace_id": self._session.workspace_id,
                "active_turn_id": active.handle.turn_id if active else "",
            }

    @property
    def session(self) -> RuntimeSession:
        with self._lock:
            return replace(self._session)

    @property
    def is_streaming(self) -> bool:
        with self._lock:
            return self._active is not None

    def runtime_info(self) -> RuntimeInfo:
        """Return the runtime's current non-blocking capability snapshot."""
        info = self._runtime.runtime_info()
        if not isinstance(info, RuntimeInfo):
            raise TypeError("runtime.runtime_info() must return RuntimeInfo")
        with self._lock:
            self._info = info
        return info

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """Subscribe to all runtime events and return an idempotent unsubscriber."""
        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._condition:
            self._ensure_open()
            token = self._next_listener
            self._next_listener += 1
            self._listeners[token] = listener
        return lambda: self._unsubscribe_token(token)

    def unsubscribe(self, listener: EventListener) -> None:
        """Remove every registration of ``listener`` by identity."""
        with self._condition:
            tokens = [key for key, value in self._listeners.items()
                      if value is listener]
            for token in tokens:
                self._listeners.pop(token, None)

    def _unsubscribe_token(self, token: int) -> None:
        with self._condition:
            self._listeners.pop(token, None)

    def prompt(self, text: str, *, streaming_behavior: str = "",
               timeout: Optional[float] = None) -> PromptOutcome:
        """Run one turn to its terminal event, or explicitly queue while busy.

        A queued steer/follow-up returns a :class:`QueueReceipt` immediately;
        it does not pretend that the eventual continuation is a new local turn.
        """
        submitted = self._submit_prompt(text, streaming_behavior)
        if isinstance(submitted, QueueReceipt):
            return submitted
        events = tuple(self._iterate_tracker(submitted, timeout))
        return self._turn_result(submitted, events)

    def prompt_events(self, text: str, *,
                      timeout: Optional[float] = None) -> Iterator[RuntimeEvent]:
        """Start a turn and yield only events carrying its exact identity."""
        tracker = self._submit_prompt(text, "")
        if isinstance(tracker, QueueReceipt):  # defensive; behavior is empty
            raise RuntimeError("prompt_events cannot represent a queued message")
        yield from self._iterate_tracker(tracker, timeout)

    def _submit_prompt(self, text: str,
                       behavior: str) -> Union[_TurnTracker, QueueReceipt]:
        prompt = _checked_text(text, "prompt")
        normalized = _normalize_behavior(behavior)
        with self._operation_lock, self._condition:
            self._ensure_open()
            self._raise_stream_failure()
            if self._compaction is not None and not self._compaction.done:
                raise RuntimeError("agent session compaction is still running")
            if self._active is not None:
                if not normalized:
                    raise RuntimeError(
                        "agent is already processing; specify streaming_behavior")
                return self._queue_locked(prompt, normalized)
            request = StartTurnRequest(
                prompt=prompt, session_id=self._session.session_id)
            handle = self._runtime.start_turn(request)
            if not isinstance(handle, TurnHandle):
                raise TypeError("runtime.start_turn() must return TurnHandle")
            tracker = _TurnTracker(handle=handle, events=deque())
            self._active = tracker
            return tracker

    def _queue_locked(self, text: str, behavior: str) -> QueueReceipt:
        tracker = self._active
        if tracker is None:
            raise RuntimeError("no active turn is available")
        request = SteerRequest(turn_id=tracker.handle.turn_id, text=text)
        if behavior == "steer":
            self._invoke_capability("steer_turn", "turn_steer", request)
        else:
            self._invoke_capability("follow_up", "message_queue", request)
        return QueueReceipt(True, behavior, tracker.handle.session_id,
                            tracker.handle.turn_id)

    def steer(self, text: str) -> QueueReceipt:
        prompt = _checked_text(text, "steering message")
        with self._operation_lock, self._condition:
            self._ensure_open()
            return self._queue_locked(prompt, "steer")

    def follow_up(self, text: str) -> QueueReceipt:
        prompt = _checked_text(text, "follow-up message")
        with self._operation_lock, self._condition:
            self._ensure_open()
            return self._queue_locked(prompt, "follow_up")

    def abort(self) -> None:
        """Request interruption of the active turn."""
        with self._operation_lock, self._condition:
            self._ensure_open()
            tracker = self._active
            if tracker is None:
                raise RuntimeError("no active turn is available")
            request = InterruptRequest(turn_id=tracker.handle.turn_id)
            self._invoke_capability("interrupt_turn", "interrupt", request)

    def runtime_state(self) -> RuntimeSessionState:
        """Read normalized provider state for the exact active session."""
        with self._operation_lock:
            request = ActiveSessionRequest(self._control_session_id())
            value = self._invoke_capability(
                "get_session_state", "session_state", request)
        if not isinstance(value, RuntimeSessionState):
            raise TypeError("runtime state operation must return RuntimeSessionState")
        return value

    def stats(self) -> RuntimeSessionStats:
        """Read normalized cumulative usage for the exact active session."""
        with self._operation_lock:
            request = ActiveSessionRequest(self._control_session_id())
            value = self._invoke_capability(
                "get_session_stats", "session_stats", request)
        if not isinstance(value, RuntimeSessionStats):
            raise TypeError("runtime stats operation must return RuntimeSessionStats")
        return value

    def get_available_models(self) -> RuntimeModelCatalog:
        """Return redacted model identities available to the active session."""
        with self._operation_lock:
            request = ActiveSessionRequest(self._control_session_id())
            value = self._invoke_capability(
                "get_available_models", "model_catalog", request)
        if not isinstance(value, RuntimeModelCatalog):
            raise TypeError("runtime model catalog must return RuntimeModelCatalog")
        return value

    def get_available_thinking_levels(self) -> RuntimeThinkingLevels:
        """Return the current model's provider-declared thinking choices."""
        with self._operation_lock:
            request = ActiveSessionRequest(self._control_session_id())
            value = self._invoke_capability(
                "get_available_thinking_levels", "thinking_levels", request)
        if not isinstance(value, RuntimeThinkingLevels):
            raise TypeError(
                "runtime thinking choices must return RuntimeThinkingLevels")
        return value

    def set_model(self, provider: str, model_id: str) -> RuntimeSessionState:
        """Select a model for the active session when safely supported."""
        with self._operation_lock:
            request = SetModelRequest(
                _checked_text(provider, "provider"),
                _checked_text(model_id, "model_id"),
                self._control_session_id(require_idle=True),
            )
            value = self._invoke_capability(
                "set_model", "model_select", request)
        if not isinstance(value, RuntimeSessionState):
            raise TypeError("runtime model operation must return RuntimeSessionState")
        return value

    def set_thinking_level(self, level: str) -> RuntimeSessionState:
        """Select a thinking level and return the provider's clamped readback."""
        with self._operation_lock:
            request = SetThinkingLevelRequest(
                _checked_text(level, "thinking level"),
                self._control_session_id(require_idle=True),
            )
            value = self._invoke_capability(
                "set_thinking_level", "thinking_select", request)
        if not isinstance(value, RuntimeSessionState):
            raise TypeError("runtime thinking operation must return RuntimeSessionState")
        return value

    def start_compaction(self, instructions: str = "") -> CompactionHandle:
        """Start one lossy provider compaction without imposing a short timeout."""
        if not isinstance(instructions, str):
            raise ValueError("compaction instructions must be a string")
        with self._operation_lock:
            request = CompactSessionRequest(
                self._control_session_id(require_idle=True), instructions)
            value = self._invoke_capability(
                "start_compaction", "session_compact", request)
        if not isinstance(value, CompactionHandle):
            raise TypeError("runtime compaction must return CompactionHandle")
        with self._condition:
            self._compaction = value
        return value

    def compact(
            self, instructions: str = "", *,
            timeout: Optional[float] = None) -> RuntimeCompactionResult:
        """Wait for compaction; local timeout never claims provider cancellation."""
        handle = self.start_compaction(instructions)
        if not handle.wait(timeout):
            raise CompactionTimeout(handle)
        return handle.result()

    def _control_session_id(self, *, require_idle: bool = False) -> str:
        with self._operation_lock, self._condition:
            self._ensure_open()
            self._raise_stream_failure()
            if require_idle and self._active is not None:
                raise RuntimeError("control operation requires an idle agent session")
            if (require_idle and self._compaction is not None
                    and not self._compaction.done):
                raise RuntimeError("control operation requires completed compaction")
            return self._session.session_id

    def _invoke_capability(self, method_name: str, capability: str,
                           request: Any) -> Any:
        method = getattr(self._runtime, method_name, None)
        supported = self.runtime_info().capabilities.supports(capability)
        if not supported or not callable(method):
            raise UnsupportedOperation(method_name, capability)
        return method(request)

    def new_session(self, *, name: str = "", session_id: str = "") -> RuntimeSession:
        request = StartSessionRequest(
            workspace_id=self._options.workspace_id,
            name=name or self._options.session_name,
            session_id=session_id or _new_id("ses"),
        )
        return self._replace_session("start_session", request)

    def resume_session(self, provider_session_id: str, *,
                       session_id: str = "") -> RuntimeSession:
        provider_id = _checked_text(provider_session_id, "provider_session_id")
        request = {
            "provider_session_id": provider_id,
            "session_id": session_id or _new_id("ses"),
            "workspace_id": self._options.workspace_id,
        }
        return self._replace_session(
            "resume_session", request, capability="session_resume")

    def fork_session(self, provider_session_id: str = "", *,
                     session_id: str = "") -> RuntimeSession:
        provider_id = provider_session_id or self.session.provider_session_id
        provider_id = _checked_text(provider_id, "provider_session_id")
        request = ForkSessionRequest(
            provider_session_id=provider_id,
            session_id=session_id or _new_id("ses"),
            workspace_id=self._options.workspace_id,
        )
        return self._replace_session(
            "fork_session", request, capability="session_fork")

    def clone_session(self, provider_session_id: str = "", *,
                      session_id: str = "") -> RuntimeSession:
        """Use an explicit clone operation when present, otherwise fork."""
        clone = getattr(self._runtime, "clone_session", None)
        if not callable(clone):
            return self.fork_session(provider_session_id, session_id=session_id)
        provider_id = provider_session_id or self.session.provider_session_id
        request = {
            "provider_session_id": _checked_text(
                provider_id, "provider_session_id"),
            "session_id": session_id or _new_id("ses"),
            "workspace_id": self._options.workspace_id,
        }
        return self._replace_session(
            "clone_session", request, capability="session_fork")

    def _replace_session(self, method_name: str, request: Any,
                         capability: str = "") -> RuntimeSession:
        with self._operation_lock, self._condition:
            self._ensure_open()
            self._raise_stream_failure()
            if self._active is not None:
                raise RuntimeError("cannot replace a session during an active turn")
            if self._compaction is not None and not self._compaction.done:
                raise RuntimeError("cannot replace a session during compaction")
            method = getattr(self._runtime, method_name, None)
            supported = self.runtime_info().capabilities.supports(capability)
            if capability and not supported:
                raise UnsupportedOperation(method_name, capability)
            if not callable(method):
                raise UnsupportedOperation(method_name, capability)
            session = _checked_session(method(request))
            self._session = session
            return replace(session)

    def _iterate_tracker(self, tracker: _TurnTracker,
                         timeout: Optional[float]) -> Iterator[RuntimeEvent]:
        deadline = _deadline(timeout)
        while True:
            event = self._next_event(tracker, deadline)
            yield event
            if event.kind in TERMINAL_EVENT_KINDS:
                return

    def _next_event(self, tracker: _TurnTracker,
                    deadline: Optional[float]) -> RuntimeEvent:
        with self._condition:
            while not tracker.events:
                self._raise_stream_failure()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("timed out waiting for terminal runtime event")
                self._condition.wait(remaining)
            return tracker.events.popleft()

    def _raise_stream_failure(self) -> None:
        if self._stream_error is not None:
            raise RuntimeError("runtime event stream failed") from self._stream_error
        if self._closed:
            raise RuntimeError("agent session is closed")
        if self._stream_closed:
            raise RuntimeError(
                "runtime event stream closed without a terminal event")

    def _turn_result(self, tracker: _TurnTracker,
                     events: tuple[RuntimeEvent, ...]) -> TurnResult:
        terminal = tracker.terminal
        if terminal is None:
            raise RuntimeError("turn ended without a terminal runtime event")
        state = str(terminal.payload.get("state", "") or tracker.handle.state)
        return TurnResult(
            session_id=tracker.handle.session_id,
            run_id=tracker.handle.run_id,
            turn_id=tracker.handle.turn_id,
            state=state,
            text=tracker.text,
            terminal_event=terminal,
            events=events,
        )

    def _pump_events(self) -> None:
        try:
            for event in self._runtime.events():
                if not isinstance(event, RuntimeEvent):
                    raise TypeError("runtime.events() must yield RuntimeEvent")
                self._dispatch_event(event)
        except Exception as exc:
            with self._condition:
                self._stream_error = exc
                self._condition.notify_all()
        finally:
            with self._condition:
                self._stream_closed = True
                self._condition.notify_all()

    def _dispatch_event(self, event: RuntimeEvent) -> None:
        with self._condition:
            tracker = self._active
            if tracker is not None and _matches(tracker.handle, event):
                tracker.consume(event)
                if event.kind in TERMINAL_EVENT_KINDS:
                    self._active = None
                self._condition.notify_all()
            listeners = tuple(self._listeners.values())
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                continue

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("agent session is closed")

    def close(self) -> None:
        """Idempotently stop the runtime and wake all blocked consumers."""
        with self._operation_lock, self._condition:
            if self._closed:
                return
            self._closed = True
            self._listeners.clear()
            self._condition.notify_all()
        try:
            self._runtime.close()
        finally:
            if self._event_thread is not threading.current_thread():
                self._event_thread.join(timeout=1.0)

    def __enter__(self) -> AgentSession:
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def create_agent_session(
        options: Optional[AgentSessionOptions] = None, *,
        runtime: Optional[AgentRuntime] = None) -> AgentSession:
    """Create, initialize, and start a provider-neutral agent session."""
    normalized = _normalize_options(options or AgentSessionOptions())
    selected = runtime if runtime is not None else _create_runtime(normalized)
    return AgentSession(selected, normalized)


def _normalize_options(options: AgentSessionOptions) -> AgentSessionOptions:
    if not isinstance(options, AgentSessionOptions):
        raise TypeError("options must be AgentSessionOptions")
    from . import config

    cwd = str(Path(options.cwd or os.getcwd()).resolve())
    explicit_agent = str(options.agent or "").strip()
    configured_agent = str(config.AGENT_BIN or "").strip()
    runtime_name = str(options.runtime or "auto").strip().lower()
    agent, runtime_name = _normalized_pi_agent(
        explicit_agent, configured_agent, runtime_name)
    timeout = options.timeout or config.AGENT_DEFAULT_TIMEOUT
    if not agent:
        raise ValueError("agent must not be empty")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("timeout must be a positive integer")
    if not isinstance(options.allow_persistent_settings, bool):
        raise ValueError("allow_persistent_settings must be a bool")
    if runtime_name not in {"auto", "pi", "pi-rpc", "codex", "codex-exec",
                            "legacy"}:
        raise ValueError(f"unsupported runtime selection: {options.runtime!r}")
    return replace(
        options, cwd=cwd, agent=agent, timeout=timeout,
        workspace_id=str(options.workspace_id or cwd), runtime=runtime_name)


def _normalized_pi_agent(
        explicit: str, configured: str, runtime_name: str) -> tuple[str, str]:
    if explicit or not _is_pi_executable(configured):
        return explicit or configured, runtime_name
    from .pi_version import select_pi_executable_token

    agent = select_pi_executable_token(fallback=configured)
    selected = "pi" if runtime_name == "auto" else runtime_name
    return agent, selected


def _create_runtime(options: AgentSessionOptions) -> AgentRuntime:
    selected = options.runtime
    executable = _executable_basename(options.agent)
    if selected == "auto":
        selected = "pi" if _is_pi_executable(options.agent) else (
            "codex" if executable == "codex" else "legacy")
    if options.allow_persistent_settings and selected not in {"pi", "pi-rpc"}:
        raise ValueError("allow_persistent_settings is only valid for Pi RPC")
    if selected in {"pi", "pi-rpc"}:
        return _create_pi_runtime(options)
    if selected in {"codex", "codex-exec"}:
        return _create_codex_runtime(options)
    return _create_legacy_runtime(options)


def _create_pi_runtime(options: AgentSessionOptions) -> AgentRuntime:
    from .tui_adapter_pi_rpc import PiRpcAdapter

    return PiRpcAdapter(
        options.cwd, agent=options.agent, model=options.model,
        provider=options.provider, session_name=options.session_name,
        allow_persistent_settings=options.allow_persistent_settings)


def _create_codex_runtime(options: AgentSessionOptions) -> AgentRuntime:
    from .tui_adapter_codex import CodexExecAdapter, DEFAULT_ARGV_PREFIX

    if options.provider:
        raise ValueError("provider override is unsupported by Codex Structured Exec")
    prefix = list(DEFAULT_ARGV_PREFIX)
    prefix[0:1] = shlex.split(options.agent)
    if options.model:
        prefix[-1:-1] = ["--model", options.model]
    return CodexExecAdapter(options.cwd, argv_prefix=prefix)


def _create_legacy_runtime(options: AgentSessionOptions) -> AgentRuntime:
    from .tui_adapter_legacy import LegacyAdapter
    from .tui_backend import TUIBackend

    backend = TUIBackend(
        cwd=options.cwd, agent=options.agent, model=options.model,
        provider=options.provider, memory_mode=options.memory_mode,
        timeout=options.timeout, session_id=options.session_id,
        session_name=options.session_name)
    return LegacyAdapter(backend)


def _executable_basename(command: str) -> str:
    parts = shlex.split(command)
    return os.path.basename(parts[0]).lower() if parts else ""


def _is_pi_executable(command: str) -> bool:
    return Path(str(command or "")).name.lower() in {"pi", "pi.exe"}


def _normalize_behavior(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    aliases = {"steer": "steer", "followUp": "follow_up",
               "followup": "follow_up", "follow_up": "follow_up"}
    if normalized not in aliases:
        raise ValueError("streaming_behavior must be steer or followUp")
    return aliases[normalized]


def _checked_text(value: str, label: str) -> str:
    text = str(value)
    if not text.strip():
        raise ValueError(f"{label} must not be empty")
    return text


def _checked_session(value: Any) -> RuntimeSession:
    if not isinstance(value, RuntimeSession):
        raise TypeError("runtime session operation must return RuntimeSession")
    return value


def _matches(handle: TurnHandle, event: RuntimeEvent) -> bool:
    return (event.session_id == handle.session_id
            and event.run_id == handle.run_id
            and event.turn_id == handle.turn_id)


def _deadline(timeout: Optional[float]) -> Optional[float]:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    if timeout <= 0 or not float(timeout) < float("inf"):
        raise ValueError("timeout must be a positive finite number")
    return time.monotonic() + float(timeout)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


__all__ = [
    "AgentSession", "AgentSessionOptions", "CompactionTimeout", "PromptOutcome",
    "QueueReceipt", "TurnResult", "create_agent_session",
]
