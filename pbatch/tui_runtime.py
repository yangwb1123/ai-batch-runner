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
        super().__init__(f"legacy runtime does not support {operation}{suffix}")


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
class ApprovalDecision:
    request_id: str
    decision: str


@dataclass(frozen=True)
class RuntimeInfo:
    runtime: str
    protocol_version: int
    capabilities: RuntimeCapabilities


@dataclass
class RuntimeSession:
    session_id: str
    provider_session_id: str
    workspace_id: str
    started_at: str
    state: str


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

    def initialize(self, request: InitializeRequest) -> RuntimeInfo: ...

    def start_session(self, request: StartSessionRequest) -> RuntimeSession: ...

    def resume_session(self, request: ResumeSessionRequest) -> RuntimeSession: ...

    def start_turn(self, request: StartTurnRequest) -> TurnHandle: ...

    def steer_turn(self, request: SteerRequest) -> None: ...

    def interrupt_turn(self, request: InterruptRequest) -> None: ...

    def resolve_approval(self, decision: ApprovalDecision) -> None: ...

    def events(self) -> Iterator[RuntimeEvent]: ...

    def close(self) -> None: ...


