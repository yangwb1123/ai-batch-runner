"""Thread-safe runtime event bus and disposable TUI projections."""

from __future__ import annotations

import queue
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .tui_protocol import RuntimeEvent, TERMINAL_TURN_STATES, TurnState


DEFAULT_BUS_MAX_EVENTS = 1_024
DEFAULT_RECENT_EVENTS = 200
_CLOSED = object()


class EventSubscription:
    """One independent, strongly held EventBus consumer."""

    def __init__(self, maximum: int):
        self.token = f"sub_{uuid.uuid4().hex}"
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=maximum)
        self._closed = False
        self._lock = threading.Lock()

    def get(self, timeout: Optional[float] = None) -> RuntimeEvent:
        """Wait for one event, using ``queue.Empty`` for poll timeouts."""
        item = self._queue.get(timeout=timeout)
        if item is _CLOSED:
            raise RuntimeError("event subscription is closed")
        return item

    def _full(self) -> bool:
        return self._queue.full()

    def _put(self, event: RuntimeEvent) -> None:
        self._queue.put_nowait(event)

    def _close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(_CLOSED)


class EventBus:
    """Validate, order, deduplicate and fan out bounded runtime events."""

    def __init__(self, max_events: int = DEFAULT_BUS_MAX_EVENTS):
        if (isinstance(max_events, bool) or not isinstance(max_events, int)
                or max_events <= 0):
            raise ValueError("max_events must be a positive integer")
        self.max_events = max_events
        self._events: queue.Queue[RuntimeEvent] = queue.Queue(maxsize=max_events)
        self._subscribers: set[EventSubscription] = set()
        self._event_ids: set[str] = set()
        self._last_seq_by_run: dict[str, int] = {}
        self._lock = threading.Lock()

    def publish(self, event: RuntimeEvent) -> bool:
        """Publish atomically; duplicates return False and overflow is explicit."""
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        event.to_jsonl()
        with self._lock:
            if event.event_id in self._event_ids:
                return False
            self._prepare_sequence(event)
            destinations = [self._events]
            destinations.extend(item._queue for item in self._subscribers)
            if any(destination.full() for destination in destinations):
                raise OverflowError("runtime event bus is full")
            self._event_ids.add(event.event_id)
            self._last_seq_by_run[event.run_id] = event.seq
            for destination in destinations:
                destination.put_nowait(event)
            return True

    def _prepare_sequence(self, event: RuntimeEvent) -> None:
        previous = self._last_seq_by_run.get(event.run_id)
        if event.seq == 0:
            event.seq = (previous or 0) + 1
            event.to_jsonl()
            return
        if previous is not None and event.seq <= previous:
            raise ValueError(
                f"non-monotonic seq for run {event.run_id!r}: "
                f"{event.seq} <= {previous}")

    def subscribe(self) -> EventSubscription:
        """Create an independent future-events subscription."""
        subscription = EventSubscription(self.max_events)
        with self._lock:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, token: EventSubscription) -> bool:
        """Remove and release a subscription; repeated removal is a no-op."""
        if not isinstance(token, EventSubscription):
            raise TypeError("token must be an EventSubscription")
        with self._lock:
            if token not in self._subscribers:
                return False
            self._subscribers.remove(token)
        token._close()
        return True

    def get(self, timeout: Optional[float] = None) -> RuntimeEvent:
        """Consume the bus' own queue; timeout follows ``queue.Queue.get``."""
        return self._events.get(timeout=timeout)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


@dataclass
class SessionProjection:
    session_id: str
    workspace_id: str
    turn_count: int = 0
    last_event_at: str = ""
    terminal_state: str = ""
    _turn_ids: set[str] = field(default_factory=set, repr=False, compare=True)


@dataclass
class TurnProjection:
    turn_id: str
    run_id: str
    session_id: str
    state: str
    last_event_at: str


@dataclass
class RuntimeStatistics:
    counts: dict[str, int] = field(default_factory=lambda: {
        "command": 0, "tool": 0, "validation": 0, "file": 0,
        "approval": 0,
    })
    usage: dict[str, float] = field(default_factory=dict)

    def __getitem__(self, name: str) -> int:
        return self.counts[name]

    def register(self, event: RuntimeEvent) -> None:
        category = _event_category(event.kind)
        if category:
            self.counts[category] += 1
        if event.kind == "usage.updated":
            usage = event.payload.get("usage", event.payload)
            if isinstance(usage, dict):
                self._merge_usage(usage)

    def _merge_usage(self, usage: dict[str, Any]) -> None:
        for name, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            self.usage[name] = self.usage.get(name, 0.0) + float(value)


class Projection:
    """Fold runtime events into bounded, rebuildable presentation state."""

    def __init__(self, recent_limit: int = DEFAULT_RECENT_EVENTS):
        if (isinstance(recent_limit, bool) or not isinstance(recent_limit, int)
                or recent_limit <= 0):
            raise ValueError("recent_limit must be a positive integer")
        self.recent_limit = recent_limit
        self.sessions: dict[str, SessionProjection] = {}
        self.turns: dict[str, TurnProjection] = {}
        self.current_turns: dict[str, TurnProjection] = {}
        self.stats = RuntimeStatistics()
        self.stats_by_run: dict[str, RuntimeStatistics] = {}
        self.recent_events: deque[RuntimeEvent] = deque(maxlen=recent_limit)
        self.drop_count = 0
        self.duplicate_count = 0
        self._event_ids: set[str] = set()
        self._lock = threading.RLock()

    @property
    def runtime_stats(self) -> RuntimeStatistics:
        return self.stats

    @property
    def dropped_late_turn_events(self) -> int:
        return self.drop_count

    def register_event(self, event: RuntimeEvent) -> bool:
        """Fold one event; terminal turn state is strictly one-way."""
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        event.to_jsonl()
        with self._lock:
            if event.event_id in self._event_ids:
                self.duplicate_count += 1
                return False
            self._event_ids.add(event.event_id)
            self.recent_events.append(event)
            session = self._session(event)
            if event.kind.startswith("turn."):
                if not self._register_turn(event, session):
                    self.drop_count += 1
                    return False
            self._register_session_event(event, session)
            self.stats.register(event)
            run_stats = self.stats_by_run.setdefault(
                event.run_id, RuntimeStatistics())
            run_stats.register(event)
            return True

    def _session(self, event: RuntimeEvent) -> SessionProjection:
        session = self.sessions.get(event.session_id)
        if session is None:
            session = SessionProjection(event.session_id, event.workspace_id)
            self.sessions[event.session_id] = session
        return session

    def _register_turn(self, event: RuntimeEvent,
                       session: SessionProjection) -> bool:
        turn = self.turns.get(event.turn_id)
        if turn is not None and turn.state in TERMINAL_TURN_STATES:
            return False
        state = _turn_state(event.kind, turn.state if turn else TurnState.QUEUED)
        if turn is None:
            turn = TurnProjection(
                turn_id=event.turn_id, run_id=event.run_id,
                session_id=event.session_id, state=state,
                last_event_at=event.occurred_at,
            )
            self.turns[event.turn_id] = turn
            if event.turn_id not in session._turn_ids:
                session._turn_ids.add(event.turn_id)
                session.turn_count += 1
        else:
            turn.state = state
            turn.last_event_at = event.occurred_at
        self.current_turns[event.run_id] = turn
        session.terminal_state = state if state in TERMINAL_TURN_STATES else ""
        return True

    @staticmethod
    def _register_session_event(event: RuntimeEvent,
                                session: SessionProjection) -> None:
        session.last_event_at = event.occurred_at
        if event.kind == "session.archived":
            session.terminal_state = "ARCHIVED"
        elif event.kind == "session.disconnected":
            session.terminal_state = "DISCONNECTED"

    def current_turn(self, run_id: str) -> Optional[TurnProjection]:
        with self._lock:
            return self.current_turns.get(run_id)

    @classmethod
    def rebuild(cls, events_iterable: Iterable[RuntimeEvent], *,
                recent_limit: int = DEFAULT_RECENT_EVENTS) -> "Projection":
        """Recreate the disposable view solely from its event stream."""
        projection = cls(recent_limit=recent_limit)
        for event in events_iterable:
            projection.register_event(event)
        return projection


def _event_category(kind: str) -> str:
    if kind.startswith("command."):
        return "command"
    if kind.startswith("tool."):
        return "tool"
    if kind.startswith("validation."):
        return "validation"
    if kind == "file.changed":
        return "file"
    if kind.startswith("approval."):
        return "approval"
    return ""


def _turn_state(kind: str, current: str) -> str:
    return {
        "turn.queued": TurnState.QUEUED,
        "turn.started": TurnState.RUNNING,
        "turn.steered": TurnState.RUNNING,
        "turn.interrupting": TurnState.INTERRUPTING,
        "turn.completed": TurnState.SUCCEEDED,
        "turn.failed": TurnState.FAILED,
        "turn.interrupted": TurnState.INTERRUPTED,
    }.get(kind, current)
