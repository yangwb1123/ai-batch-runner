"""Provider-neutral TUI runtime contract and the legacy CLI adapter."""

from __future__ import annotations

import hashlib
import math
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Optional, Protocol, runtime_checkable

from .tui_protocol import (
    RUNTIME_PROTOCOL_VERSION,
    RuntimeCapabilities,
    RuntimeEvent,
    TurnState,
)




class UnsupportedOperation(RuntimeError):
    """Raised when a runtime cannot truthfully provide an operation."""

    def __init__(self, operation: str, capability: str = ""):
        self.operation = operation
        self.capability = capability
        suffix = f" (requires {capability})" if capability else ""
        super().__init__(f"runtime does not support {operation}{suffix}")


class SessionReconciliationError(RuntimeError):
    """A provider thread cannot safely become the active local session."""

    def __init__(self, status: str, detail: str = "") -> None:
        self.status = str(status or "unverified")
        self.detail = str(detail or "provider session could not be verified")[:2048]
        super().__init__(self.detail)


@dataclass(frozen=True)
class InitializeRequest:
    workspace_id: str = ""

@dataclass(frozen=True)
class StartSessionRequest:
    workspace_id: str = ""
    name: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class ResumeSessionRequest:
    session_id: str
    workspace_id: str = ""


@dataclass(frozen=True)
class ReadSessionRequest:
    provider_session_id: str


@dataclass(frozen=True)
class ListSessionsRequest:
    """Bounded provider history query; adapters own the workspace boundary."""

    cursor: Optional[str] = None
    limit: int = 50
    sort_key: str = "updated_at"
    sort_direction: str = "desc"
    model_providers: tuple[str, ...] = ()
    source_kinds: tuple[str, ...] = (
        "cli", "vscode", "exec", "appServer",
    )
    archived: bool = False
    is_pinned: Optional[bool] = None
    search_term: str = ""
    use_state_db_only: bool = True


@dataclass(frozen=True)
class ForkSessionRequest:
    provider_session_id: str
    session_id: str = ""
    workspace_id: str = ""


@dataclass(frozen=True)
class ArchiveSessionRequest:
    """Exact provider thread target for remote archive lifecycle operations."""

    provider_session_id: str
    session_id: str = ""
    workspace_id: str = ""


@dataclass(frozen=True)
class StartTurnRequest:
    prompt: str
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    actor_id: str = "agent_main"


@dataclass(frozen=True)
class SteerRequest:
    turn_id: str
    text: str


@dataclass(frozen=True)
class InterruptRequest:
    turn_id: str


@dataclass(frozen=True)
class ActorSteerRequest:
    """Exact child-actor steering target; provider must confirm this identity."""

    turn_id: str
    actor_id: str
    text: str


@dataclass(frozen=True)
class ActorInterruptRequest:
    """Exact child-actor stop target; provider must confirm this identity."""

    turn_id: str
    actor_id: str


@dataclass(frozen=True)
class ActiveSessionRequest:
    """Target the runtime's active session without accepting provider paths."""

    session_id: str = ""


@dataclass(frozen=True)
class RuntimeModelRef:
    """Redacted model identity safe to expose across providers."""

    provider: str
    model_id: str
    name: str = ""


@dataclass(frozen=True)
class RuntimeModelCatalog:
    """Bounded model identities available to the active runtime session."""

    session_id: str
    models: tuple[RuntimeModelRef, ...] = ()


@dataclass(frozen=True)
class RuntimeThinkingLevels:
    """Provider-declared thinking levels for the active model."""

    session_id: str
    levels: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeSessionState:
    """Normalized active-session state; missing provider facts stay unknown."""

    session_id: str
    provider_session_id: str
    name: str = ""
    model: Optional[RuntimeModelRef] = None
    thinking_level: str = ""
    is_streaming: Optional[bool] = None
    is_compacting: Optional[bool] = None
    auto_compaction_enabled: Optional[bool] = None
    message_count: Optional[int] = None
    pending_message_count: Optional[int] = None


@dataclass(frozen=True)
class RuntimeTokenTotals:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass(frozen=True)
class RuntimeContextUsage:
    tokens: Optional[int] = None
    context_window: Optional[int] = None
    percent: Optional[float] = None


@dataclass(frozen=True)
class RuntimeSessionStats:
    session_id: str
    provider_session_id: str
    user_messages: Optional[int] = None
    assistant_messages: Optional[int] = None
    tool_calls: Optional[int] = None
    tool_results: Optional[int] = None
    total_messages: Optional[int] = None
    tokens: RuntimeTokenTotals = field(default_factory=RuntimeTokenTotals)
    cost: Optional[float] = None
    context_usage: Optional[RuntimeContextUsage] = None


@dataclass(frozen=True)
class SetModelRequest:
    provider: str
    model_id: str
    session_id: str = ""


@dataclass(frozen=True)
class SetThinkingLevelRequest:
    level: str
    session_id: str = ""


@dataclass(frozen=True)
class CompactSessionRequest:
    session_id: str = ""
    instructions: str = ""


@dataclass(frozen=True)
class RuntimeCompactionResult:
    session_id: str
    summary: str
    summary_truncated: bool = False
    tokens_before: Optional[int] = None
    estimated_tokens_after: Optional[int] = None
    tokens: RuntimeTokenTotals = field(default_factory=RuntimeTokenTotals)
    cost: Optional[float] = None


class CompactionHandle:
    """A non-cancellable provider compaction operation."""

    def __init__(self, operation_id: str, session_id: str, started_at: str):
        self.operation_id = operation_id
        self.session_id = session_id
        self.started_at = started_at
        self._state = "RUNNING"
        self._result: Optional[RuntimeCompactionResult] = None
        self._error: Optional[BaseException] = None
        self._done = threading.Event()
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if timeout is not None and (isinstance(timeout, bool)
                                    or not isinstance(timeout, (int, float))
                                    or timeout < 0 or not math.isfinite(timeout)):
            raise ValueError("timeout must be a non-negative finite number")
        return self._done.wait(timeout)

    def result(self, timeout: Optional[float] = None) -> RuntimeCompactionResult:
        if not self.wait(timeout):
            raise TimeoutError("timed out waiting for session compaction")
        with self._lock:
            error, result = self._error, self._result
        if error is not None:
            raise error
        if result is None:
            raise RuntimeError("session compaction ended without a result")
        return result

    def _set_result(self, result: RuntimeCompactionResult) -> None:
        with self._lock:
            if self._done.is_set():
                return
            self._result, self._state = result, "SUCCEEDED"
        self._done.set()

    def _set_exception(
            self, error: BaseException, *, state: str = "FAILED") -> None:
        with self._lock:
            if self._done.is_set():
                return
            self._error, self._state = error, state
        self._done.set()


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str | int
    decision: str


@dataclass(frozen=True)
class RuntimeInfo:
    runtime: str
    protocol_version: int
    capabilities: RuntimeCapabilities
    runtime_version: str = ""
    settings_scope: str = ""


@dataclass
class RuntimeSession:
    session_id: str
    provider_session_id: str
    workspace_id: str
    started_at: str
    state: str


@dataclass(frozen=True)
class RuntimeSessionObservation:
    """Bounded, non-subscribing observation of one provider session."""

    provider_session_id: str
    status: str
    active_flags: tuple[str, ...] = ()
    name: str = ""


@dataclass(frozen=True)
class RuntimeSessionSummary:
    """One bounded, display-only provider history observation."""

    provider_session_id: str
    name: str = ""
    preview: str = ""
    status: str = ""
    active_flags: tuple[str, ...] = ()
    model_provider: str = ""
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    ephemeral: Optional[bool] = None
    is_pinned: Optional[bool] = None


@dataclass(frozen=True)
class RuntimeSessionPage:
    """One provider page; a non-empty cursor means the view is incomplete."""

    sessions: tuple[RuntimeSessionSummary, ...] = ()
    next_cursor: Optional[str] = None
    warnings: tuple[str, ...] = ()


@dataclass
class TurnHandle:
    session_id: str
    run_id: str
    turn_id: str
    actor_id: str
    started_at: str
    state: str = TurnState.QUEUED
    _done: threading.Event = field(default_factory=threading.Event,
                                   repr=False, compare=False)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait until the adapter has observed a terminal result."""
        return self._done.wait(timeout)

    @property
    def done(self) -> bool:
        return self._done.is_set()


@runtime_checkable
class AgentRuntime(Protocol):
    """Semantic runtime boundary consumed by future TUI controllers."""

    def runtime_info(self) -> RuntimeInfo:
        """Return cached, non-blocking capability state without handshaking."""
        ...

    def initialize(self, request: InitializeRequest) -> RuntimeInfo: ...

    def start_session(self, request: StartSessionRequest) -> RuntimeSession: ...

    def resume_session(self, request: ResumeSessionRequest) -> RuntimeSession: ...

    def read_session(
            self, request: ReadSessionRequest) -> RuntimeSessionObservation: ...

    def list_sessions(
            self, request: ListSessionsRequest) -> RuntimeSessionPage: ...

    def fork_session(self, request: ForkSessionRequest) -> RuntimeSession: ...

    def start_turn(self, request: StartTurnRequest) -> TurnHandle: ...

    def steer_turn(self, request: SteerRequest) -> None: ...

    def interrupt_turn(self, request: InterruptRequest) -> None: ...

    def resolve_approval(self, decision: ApprovalDecision) -> None: ...

    def events(self) -> Iterator[RuntimeEvent]: ...

    def close(self) -> None: ...


@runtime_checkable
class SessionArchiveRuntime(Protocol):
    """Optional provider-owned archive controls with exact thread targets."""

    def archive_session(self, request: ArchiveSessionRequest) -> None: ...

    def unarchive_session(self, request: ArchiveSessionRequest) -> None: ...


@runtime_checkable
class ActorControlRuntime(Protocol):
    """Optional child-actor controls; implementations must verify both ids."""

    def steer_actor(self, request: ActorSteerRequest) -> None: ...

    def interrupt_actor(self, request: ActorInterruptRequest) -> None: ...


@runtime_checkable
class AgentControlRuntime(Protocol):
    """Optional typed controls discovered through RuntimeCapabilities."""

    def get_session_state(
            self, request: ActiveSessionRequest) -> RuntimeSessionState: ...

    def get_session_stats(
            self, request: ActiveSessionRequest) -> RuntimeSessionStats: ...

    def get_available_models(
            self, request: ActiveSessionRequest) -> RuntimeModelCatalog: ...

    def get_available_thinking_levels(
            self, request: ActiveSessionRequest) -> RuntimeThinkingLevels: ...

    def set_model(self, request: SetModelRequest) -> RuntimeSessionState: ...

    def set_thinking_level(
            self, request: SetThinkingLevelRequest) -> RuntimeSessionState: ...

    def start_compaction(
            self, request: CompactSessionRequest) -> CompactionHandle: ...
