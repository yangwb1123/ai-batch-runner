"""Thread-safe runtime event bus and disposable TUI projections."""

from __future__ import annotations

import math
import queue
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol

from .tui_actor_tree import (
    ActorAuthority, ActorState, normalize_actor_authority)
from .tui_findings import ReviewFinding, finding_from_event_payload
from .tui_protocol import (
    RuntimeEvent, TERMINAL_TURN_STATES, TurnState, turn_transition,
)


DEFAULT_BUS_MAX_EVENTS = 1_024
DEFAULT_RECENT_EVENTS = 200
DEFAULT_INDEX_LIMIT = 4_096
DEFAULT_ENTITY_LIMIT = 1_024
MAX_USAGE_KEYS = 64
_TURN_SCOPED_KINDS = frozenset({
    "pipeline.started", "pipeline.checkpoint", "pipeline.completed",
    "pipeline.failed",
    "stage.started", "stage.completed", "task.started", "task.completed",
    "message.started", "message.delta", "message.completed", "plan.updated",
    "policy.updated", "command.started", "command.output", "command.completed",
    "tool.started",
    "tool.completed", "tool.failed", "file.changed", "validation.started",
    "validation.completed", "gate.verdict", "approval.requested",
    "approval.resolved", "approval.expired", "review.finding",
    "artifact.created",
})


class EventJournal(Protocol):
    """Durable sink committed before an event becomes visible."""

    def append(self, event: RuntimeEvent) -> bool:
        ...


class _CloseableQueue:
    """A bounded FIFO whose close wakes waiters without dropping queued events."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self._items: deque[RuntimeEvent] = deque()
        self._closed = False
        self._condition = threading.Condition()

    def full(self) -> bool:
        with self._condition:
            return len(self._items) >= self.maximum

    def put_nowait(self, event: RuntimeEvent) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("runtime event queue is closed")
            if len(self._items) >= self.maximum:
                raise queue.Full
            self._items.append(event)
            self._condition.notify()

    def get(self, timeout: Optional[float] = None) -> RuntimeEvent:
        with self._condition:
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if self._items:
                return self._items.popleft()
            raise RuntimeError("runtime event queue is closed")

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()


def _canonical_event(event: RuntimeEvent) -> RuntimeEvent:
    """Detach one validated event from producer-owned mutable mappings."""
    if not isinstance(event, RuntimeEvent):
        raise TypeError("event must be a RuntimeEvent")
    return RuntimeEvent.from_jsonl(event.to_jsonl())


class EventSubscription:
    """One independent, strongly held EventBus consumer."""

    def __init__(self, maximum: int):
        self.token = f"sub_{uuid.uuid4().hex}"
        self._queue = _CloseableQueue(maximum)

    def get(self, timeout: Optional[float] = None) -> RuntimeEvent:
        """Wait for one event, using ``queue.Empty`` for poll timeouts."""
        try:
            return self._queue.get(timeout=timeout)
        except RuntimeError as exc:
            raise RuntimeError("event subscription is closed") from exc

    def _full(self) -> bool:
        return self._queue.full()

    def _put(self, event: RuntimeEvent) -> None:
        self._queue.put_nowait(event)

    def _close(self) -> None:
        self._queue.close()


class EventBus:
    """Validate, order, deduplicate and fan out bounded runtime events."""

    def __init__(self, max_events: int = DEFAULT_BUS_MAX_EVENTS,
                 index_limit: int = DEFAULT_INDEX_LIMIT,
                 journal: Optional[EventJournal] = None):
        if (isinstance(max_events, bool) or not isinstance(max_events, int)
                or max_events <= 0):
            raise ValueError("max_events must be a positive integer")
        if (isinstance(index_limit, bool) or not isinstance(index_limit, int)
                or index_limit <= 0):
            raise ValueError("index_limit must be a positive integer")
        self.max_events = max_events
        self.index_limit = index_limit
        self._events = _CloseableQueue(max_events)
        self._subscribers: set[EventSubscription] = set()
        self._event_ids: OrderedDict[str, None] = OrderedDict()
        self._last_seq_by_run: OrderedDict[str, int] = OrderedDict()
        self._journal = journal
        self._lock = threading.Lock()
        self._closed = False
        self._published = False

    def publish(self, event: RuntimeEvent) -> bool:
        """Publish atomically; duplicates return False and overflow is explicit."""
        canonical = _canonical_event(event)
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime event bus is closed")
            if canonical.event_id in self._event_ids:
                return False
            self._prepare_sequence(canonical)
            destinations = [self._events]
            destinations.extend(item._queue for item in self._subscribers)
            if any(destination.full() for destination in destinations):
                raise OverflowError("runtime event bus is full")
            deliveries = [_canonical_event(canonical) for _item in destinations]
            if self._journal is not None and not self._journal.append(canonical):
                return False
            self._remember(canonical)
            if event.seq == 0:
                event.seq = canonical.seq
            for destination, delivery in zip(destinations, deliveries):
                destination.put_nowait(delivery)
            self._published = True
            return True

    def restore_event(self, event: RuntimeEvent) -> bool:
        """Restore ordering/id indexes without journaling or notifying consumers."""
        canonical = _canonical_event(event)
        if canonical.seq <= 0:
            raise ValueError("restored runtime events require a positive seq")
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime event bus is closed")
            if self._published:
                raise RuntimeError("cannot restore EventBus after live publication")
            if canonical.event_id in self._event_ids:
                return False
            self._prepare_sequence(canonical)
            self._remember(canonical)
            return True

    def validate_sequence(self, event: RuntimeEvent) -> bool:
        """Preview ordering without reserving a sequence or changing evidence."""
        canonical = _canonical_event(event)
        with self._lock:
            if self._closed:
                raise RuntimeError("runtime event bus is closed")
            if canonical.event_id in self._event_ids:
                return False
            self._prepare_sequence(canonical)
            return True

    def _prepare_sequence(self, event: RuntimeEvent) -> None:
        previous = self._last_seq_by_run.get(event.run_id)
        if previous is None and len(self._last_seq_by_run) >= self.index_limit:
            raise OverflowError("runtime event run sequence index is full")
        if event.seq == 0:
            event.seq = (previous or 0) + 1
            event.to_jsonl()
            return
        if previous is not None and event.seq <= previous:
            raise ValueError(
                f"non-monotonic seq for run {event.run_id!r}: "
                f"{event.seq} <= {previous}")

    def _remember(self, event: RuntimeEvent) -> None:
        self._event_ids[event.event_id] = None
        self._event_ids.move_to_end(event.event_id)
        self._last_seq_by_run[event.run_id] = event.seq
        while len(self._event_ids) > self.index_limit:
            self._event_ids.popitem(last=False)

    def subscribe(self) -> EventSubscription:
        """Create an independent future-events subscription."""
        subscription = EventSubscription(self.max_events)
        with self._lock:
            if self._closed:
                subscription._close()
                raise RuntimeError("runtime event bus is closed")
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

    def close(self) -> None:
        """Close the bus and every subscription, waking all blocked consumers."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions = tuple(self._subscribers)
            self._subscribers.clear()
            self._events.close()
            for subscription in subscriptions:
                subscription._close()

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
    workspace_id: str = ""
    actor_id: str = ""


@dataclass
class RuntimeStatistics:
    counts: dict[str, int] = field(default_factory=lambda: {
        "command": 0, "tool": 0, "validation": 0, "file": 0,
        "approval": 0,
    })
    usage: dict[str, float] = field(default_factory=dict)
    usage_limit: int = MAX_USAGE_KEYS

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
            if name not in self.usage and len(self.usage) >= self.usage_limit:
                continue
            try:
                numeric = float(value)
            except (OverflowError, ValueError):
                continue
            if not math.isfinite(numeric):
                continue
            combined = self.usage.get(name, 0.0) + numeric
            if math.isfinite(combined):
                self.usage[name] = combined


class Projection:
    """Fold runtime events into bounded, rebuildable presentation state."""

    def __init__(self, recent_limit: int = DEFAULT_RECENT_EVENTS,
                 entity_limit: int = DEFAULT_ENTITY_LIMIT):
        if (isinstance(recent_limit, bool) or not isinstance(recent_limit, int)
                or recent_limit <= 0):
            raise ValueError("recent_limit must be a positive integer")
        if (isinstance(entity_limit, bool) or not isinstance(entity_limit, int)
                or entity_limit <= 0):
            raise ValueError("entity_limit must be a positive integer")
        self.recent_limit, self.entity_limit = recent_limit, entity_limit
        self.sessions: OrderedDict[str, SessionProjection] = OrderedDict()
        self.turns: OrderedDict[str, TurnProjection] = OrderedDict()
        self.current_turns: OrderedDict[str, TurnProjection] = OrderedDict()
        self.actors: OrderedDict[tuple[str, str], ActorState] = OrderedDict()
        self._actor_limit = min(4096, max(64, entity_limit * 4))
        self.actor_drop_count = 0
        self.findings: OrderedDict[str, ReviewFinding] = OrderedDict()
        self._finding_limit = min(256, max(64, entity_limit * 2))
        self.finding_drop_count = 0
        self.stats = RuntimeStatistics()
        self.stats_by_run: OrderedDict[str, RuntimeStatistics] = OrderedDict()
        self.recent_events: deque[RuntimeEvent] = deque(maxlen=recent_limit)
        self.drop_count = 0
        self.duplicate_count = 0
        self._event_ids: OrderedDict[str, str] = OrderedDict()
        self._retired_turn_ids: OrderedDict[str, None] = OrderedDict()
        self._retired_turn_limit = max(64, entity_limit * 4)
        self._run_bindings: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._run_binding_limit = max(64, entity_limit * 4)
        self._lock = threading.RLock()

    @property
    def runtime_stats(self) -> RuntimeStatistics:
        return self.stats

    @property
    def dropped_late_turn_events(self) -> int:
        return self.drop_count

    def register_event(self, event: RuntimeEvent) -> bool:
        """Fold one event; terminal turn state is strictly one-way."""
        canonical = _canonical_event(event)
        with self._lock:
            if canonical.event_id in self._event_ids:
                self.duplicate_count += 1
                return False
            turn = self.turns.get(canonical.turn_id) if canonical.turn_id else None
            if canonical.turn_id in self._retired_turn_ids:
                self._remember_event(canonical)
                self.drop_count += 1
                return False
            self._validate_bindings(canonical, turn)
            if self._late_terminal_event(canonical, turn):
                self._remember_event(canonical)
                self.drop_count += 1
                return False
            next_state = self._next_turn_state(canonical, turn)
            self._reserve_entities(canonical, turn, next_state)
            if canonical.run_id:
                self._run_bindings.setdefault(
                    canonical.run_id, (canonical.workspace_id, canonical.session_id))
            self._remember_event(canonical)
            session = self._session(canonical)
            if next_state is not None:
                self._register_turn(canonical, session, next_state)
            self._register_actor(canonical, next_state)
            self._register_finding(canonical)
            self._register_session_event(canonical, session)
            self.stats.register(canonical)
            run_stats = self.stats_by_run.setdefault(
                canonical.run_id, RuntimeStatistics())
            run_stats.register(canonical)
            self.stats_by_run.move_to_end(canonical.run_id)
            return True

    def validate_event(self, event: RuntimeEvent) -> bool:
        """Preview semantic acceptance without mutating projection state."""
        canonical = _canonical_event(event)
        with self._lock:
            if canonical.event_id in self._event_ids:
                return False
            turn = self.turns.get(canonical.turn_id) if canonical.turn_id else None
            if canonical.turn_id in self._retired_turn_ids:
                return False
            self._validate_bindings(canonical, turn)
            if self._late_terminal_event(canonical, turn):
                return False
            next_state = self._next_turn_state(canonical, turn)
            self._entity_evictions(canonical, turn, next_state)
            return True

    def _validate_bindings(self, event: RuntimeEvent,
                           turn: Optional[TurnProjection]) -> None:
        self._validate_event_scope(event)
        session = self.sessions.get(event.session_id)
        if session is not None and session.workspace_id != event.workspace_id:
            raise ValueError("session workspace binding mismatch")
        self._validate_run_binding(event)
        self._validate_actor_event(event, turn)
        current = self.current_turns.get(event.run_id)
        if current is not None:
            actual_run = (event.workspace_id, event.session_id)
            expected_run = (current.workspace_id, current.session_id)
            if actual_run != expected_run:
                raise ValueError("run workspace/session binding mismatch")
            starts_new = (turn is None and event.kind in {
                "turn.queued", "turn.started"})
            if starts_new and current.state not in TERMINAL_TURN_STATES:
                raise ValueError("run already has an active turn")
        if turn is None:
            turn_scoped = bool(event.turn_id and not event.kind.startswith("turn."))
            if turn_scoped:
                raise ValueError("turn-scoped event requires a known turn")
            return
        actual = (event.workspace_id, event.session_id, event.run_id, event.actor_id)
        expected = (turn.workspace_id, turn.session_id, turn.run_id, turn.actor_id)
        if actual != expected and not _child_actor_event(
                event, turn, self.actors):
            raise ValueError("turn workspace/session/run/actor binding mismatch")

    @staticmethod
    def _validate_event_scope(event: RuntimeEvent) -> None:
        if event.kind.startswith("turn.") and not event.turn_id:
            raise ValueError("turn lifecycle event requires a turn_id")
        if event.kind in _TURN_SCOPED_KINDS and not event.turn_id:
            raise ValueError("turn-scoped event requires a turn_id")

    def _validate_run_binding(self, event: RuntimeEvent) -> None:
        if not event.run_id:
            return
        binding = self._run_bindings.get(event.run_id)
        if (binding is not None
                and binding != (event.workspace_id, event.session_id)):
            raise ValueError("run workspace/session binding mismatch")

    def _validate_actor_event(self, event: RuntimeEvent,
                              turn: Optional[TurnProjection]) -> None:
        if not event.kind.startswith("actor."):
            return
        if turn is None or not event.actor_id:
            raise ValueError("actor event requires a known actor turn")
        payload_actor = event.payload.get("actor_id")
        if payload_actor is not None and payload_actor != event.actor_id:
            raise ValueError("actor event actor_id does not match envelope")
        root = event.actor_id == turn.actor_id
        parent = event.payload.get("parent_actor_id", "")
        if root:
            if parent not in ("", None):
                raise ValueError("root actor must not declare a parent")
            return
        if not _child_actor_event(event, turn, self.actors):
            raise ValueError("actor binding or lineage mismatch")
        key = _actor_key(turn.turn_id, event.actor_id)
        exists = key in self.actors
        if event.kind == "actor.spawned" and exists:
            raise ValueError("actor was spawned more than once")
        if event.kind != "actor.spawned" and not exists:
            raise ValueError("actor event requires a prior actor.spawned")

    @staticmethod
    def _late_terminal_event(event: RuntimeEvent,
                             turn: Optional[TurnProjection]) -> bool:
        return bool(turn is not None and turn.state in TERMINAL_TURN_STATES
                    and event.kind != "workspace.snapshot")

    @staticmethod
    def _next_turn_state(event: RuntimeEvent,
                         turn: Optional[TurnProjection]) -> Optional[str]:
        if not event.kind.startswith("turn."):
            return None
        target = _turn_state(event)
        if turn is None:
            if target == TurnState.QUEUED:
                return target
            if target == TurnState.RUNNING and event.kind == "turn.started":
                return turn_transition(TurnState.QUEUED, target)
            raise ValueError(f"turn must begin with queued or started, not {event.kind}")
        if event.kind == "turn.steered":
            if turn.state == TurnState.RUNNING:
                return TurnState.RUNNING
            raise ValueError("turn.steered requires a running turn")
        return turn_transition(turn.state, target)

    def _remember_event(self, event: RuntimeEvent) -> None:
        self._event_ids[event.event_id] = event.turn_id
        self._event_ids.move_to_end(event.event_id)
        self.recent_events.append(event)
        while len(self._event_ids) > self.entity_limit:
            victim = self._terminal_event_id()
            self._event_ids.pop(victim, None)

    def _terminal_event_id(self) -> str:
        for event_id, turn_id in self._event_ids.items():
            turn = self.turns.get(turn_id)
            if not turn_id or turn is None or turn.state in TERMINAL_TURN_STATES:
                return event_id
        return next(iter(self._event_ids))

    def _reserve_entities(self, event: RuntimeEvent,
                          turn: Optional[TurnProjection],
                          next_state: Optional[str]) -> None:
        evictions = self._entity_evictions(event, turn, next_state)
        self._apply_entity_evictions(*evictions)

    def _entity_evictions(self, event: RuntimeEvent,
                          turn: Optional[TurnProjection],
                          next_state: Optional[str]
                          ) -> tuple[Optional[str], Optional[str], Optional[str],
                                       Optional[tuple[str, str]]]:
        if (event.run_id and event.run_id not in self._run_bindings
                and len(self._run_bindings) >= self._run_binding_limit):
            raise OverflowError("projection run binding index is full")
        session_victim = self._required_session_victim(event)
        excluded = {item.turn_id for item in self.turns.values()
                    if item.session_id == session_victim}
        turn_victim = self._required_turn_victim(turn, next_state, excluded)
        if turn_victim is not None:
            excluded.add(turn_victim)
        freed_runs = self._runs_freed_by(excluded)
        stats_victim = self._required_stats_victim(event, freed_runs)
        new_retired = [turn_id for turn_id in excluded
                       if turn_id not in self._retired_turn_ids]
        if len(self._retired_turn_ids) + len(new_retired) > self._retired_turn_limit:
            raise OverflowError("projection retired turn index is full")
        actor_victim = self._required_actor_victim(event, excluded)
        return session_victim, turn_victim, stats_victim, actor_victim

    def _required_session_victim(self, event: RuntimeEvent) -> Optional[str]:
        if event.session_id in self.sessions or len(self.sessions) < self.entity_limit:
            return None
        victim = self._session_victim()
        if victim is None:
            raise OverflowError("projection session limit has no terminal victim")
        return victim

    def _required_turn_victim(self, turn: Optional[TurnProjection],
                              next_state: Optional[str],
                              excluded: set[str]) -> Optional[str]:
        new_turn = next_state is not None and turn is None
        if not new_turn or len(self.turns) - len(excluded) < self.entity_limit:
            return None
        victim = self._turn_victim(excluded)
        if victim is None:
            raise OverflowError("projection turn limit has no terminal victim")
        return victim

    def _required_stats_victim(self, event: RuntimeEvent,
                               freed_runs: set[str]) -> Optional[str]:
        new_stats = event.run_id not in self.stats_by_run
        if not new_stats or len(self.stats_by_run) - len(freed_runs) < self.entity_limit:
            return None
        victim = self._stats_victim(event.run_id, freed_runs)
        if victim is None:
            raise OverflowError("projection run limit has no terminal victim")
        return victim

    def _required_actor_victim(
            self, event: RuntimeEvent,
            excluded_turns: set[str]) -> Optional[tuple[str, str]]:
        if not event.kind.startswith("actor.") or not event.actor_id:
            return None
        turn = self.turns.get(event.turn_id)
        if turn is not None and event.actor_id == turn.actor_id:
            return None
        key = _actor_key(event.turn_id, event.actor_id)
        if key in self.actors or len(self.actors) < self._actor_limit:
            return None
        victim = self._actor_victim(excluded_turns)
        if victim is None:
            raise OverflowError("projection actor tree is full")
        return victim

    def _apply_entity_evictions(self, session_victim: Optional[str],
                                turn_victim: Optional[str],
                                stats_victim: Optional[str],
                                actor_victim: Optional[tuple[str, str]]) -> None:
        if session_victim is not None:
            self._evict_session(session_victim)
        if turn_victim is not None:
            self._evict_turn(turn_victim)
        if stats_victim is not None:
            self.stats_by_run.pop(stats_victim, None)
        if actor_victim is not None:
            self.actors.pop(actor_victim, None)

    def _session_victim(self) -> Optional[str]:
        safe_states = TERMINAL_TURN_STATES | {"ARCHIVED"}
        for session_id, session in self.sessions.items():
            related = [turn for turn in self.turns.values()
                       if turn.session_id == session_id]
            if (session.terminal_state in safe_states
                    and all(turn.state in TERMINAL_TURN_STATES for turn in related)):
                return session_id
        return None

    def _turn_victim(self, excluded: set[str]) -> Optional[str]:
        return next((turn_id for turn_id, turn in self.turns.items()
                     if turn_id not in excluded
                     and turn.state in TERMINAL_TURN_STATES), None)

    def _stats_victim(self, protected: str,
                      excluded: set[str]) -> Optional[str]:
        for run_id in self.stats_by_run:
            if run_id == protected or run_id in excluded:
                continue
            current = self.current_turns.get(run_id)
            if current is None or current.state in TERMINAL_TURN_STATES:
                return run_id
        return None

    def _actor_victim(self, excluded_turns: set[str]) -> Optional[tuple[str, str]]:
        for key, actor in self.actors.items():
            if actor.turn_id in excluded_turns or not actor.terminal:
                continue
            has_child = any(
                child.parent_actor_id == actor.actor_id
                and child.turn_id == actor.turn_id
                for child in self.actors.values())
            if not has_child:
                return key
        return None

    def _runs_freed_by(self, turn_ids: set[str]) -> set[str]:
        candidates = {self.turns[turn_id].run_id for turn_id in turn_ids
                      if turn_id in self.turns}
        return {run_id for run_id in candidates
                if all(turn.turn_id in turn_ids for turn in self.turns.values()
                       if turn.run_id == run_id)}

    def _evict_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        victims = [turn_id for turn_id, turn in self.turns.items()
                   if turn.session_id == session_id]
        for turn_id in victims:
            self._evict_turn(turn_id)

    def _evict_turn(self, turn_id: str) -> None:
        turn = self.turns.pop(turn_id, None)
        if turn is None:
            return
        self._retired_turn_ids[turn_id] = None
        session = self.sessions.get(turn.session_id)
        if session is not None:
            session._turn_ids.discard(turn_id)
        if self.current_turns.get(turn.run_id) is turn:
            self.current_turns.pop(turn.run_id, None)
        if not any(item.run_id == turn.run_id for item in self.turns.values()):
            self.stats_by_run.pop(turn.run_id, None)
        for key, actor in tuple(self.actors.items()):
            if actor.turn_id == turn_id:
                self.actors.pop(key, None)
        for event_id, finding in tuple(self.findings.items()):
            if finding.turn_id == turn_id:
                self.findings.pop(event_id, None)

    def _session(self, event: RuntimeEvent) -> SessionProjection:
        session = self.sessions.get(event.session_id)
        if session is None:
            session = SessionProjection(event.session_id, event.workspace_id)
            self.sessions[event.session_id] = session
        else:
            self.sessions.move_to_end(event.session_id)
        return session

    def _register_finding(self, event: RuntimeEvent) -> None:
        if event.kind != "review.finding":
            return
        finding = finding_from_event_payload(
            event.payload, event_id=event.event_id,
            turn_id=event.turn_id, actor_id=event.actor_id)
        if finding is None:
            self.finding_drop_count += 1
            return
        self.findings[event.event_id] = finding
        self.findings.move_to_end(event.event_id)
        while len(self.findings) > self._finding_limit:
            self.findings.popitem(last=False)
            self.finding_drop_count += 1

    def _register_actor(self, event: RuntimeEvent,
                        turn_state: Optional[str]) -> None:
        if not event.actor_id:
            return
        if event.kind.startswith("turn."):
            state = turn_state or "UNKNOWN"
            terminal = state in TERMINAL_TURN_STATES
            actor = self._upsert_actor(
                event, parent_actor_id="", status=state,
                terminal=terminal,
                success=state == TurnState.SUCCEEDED if terminal else None)
            if actor is not None and "authority" in event.payload:
                actor.authority = normalize_actor_authority(
                    event.payload.get("authority"))
            return
        if not event.kind.startswith("actor."):
            return
        parent = event.payload.get("parent_actor_id", "")
        actor = self._upsert_actor(
            event, parent_actor_id=parent if isinstance(parent, str) else "",
            status=_actor_status(event), terminal=_actor_terminal(event),
            success=_actor_success(event))
        if actor is None:
            self.actor_drop_count += 1

    def _upsert_actor(self, event: RuntimeEvent, *, parent_actor_id: str,
                      status: str, terminal: bool,
                      success: Optional[bool]) -> Optional[ActorState]:
        key = _actor_key(event.turn_id, event.actor_id)
        actor = self.actors.get(key)
        if actor is None:
            if len(self.actors) >= self._actor_limit:
                victim = self._actor_victim(set())
                if victim is None:
                    return None
                self.actors.pop(victim, None)
            actor = ActorState(
                actor_id=event.actor_id, parent_actor_id=parent_actor_id,
                workspace_id=event.workspace_id, session_id=event.session_id,
                run_id=event.run_id, turn_id=event.turn_id,
                started_at=event.occurred_at)
            self.actors[key] = actor
        else:
            if parent_actor_id and not actor.parent_actor_id:
                actor.parent_actor_id = parent_actor_id
        payload = event.payload
        actor.name = _actor_text(payload.get("name"), actor.name, 512)
        actor.role = _actor_text(payload.get("role"), actor.role, 512)
        actor.provider = _actor_text(payload.get("provider"), actor.provider, 256)
        actor.status = status or actor.status
        if "authority" in payload:
            parent_authority = _parent_actor_authority(
                self.actors, actor.turn_id, actor.parent_actor_id)
            actor.authority = normalize_actor_authority(
                payload.get("authority"), actor.parent_actor_id,
                parent_authority)
        actor.terminal = bool(actor.terminal or terminal)
        if success is not None:
            actor.success = success
        actor.last_event_at = event.occurred_at
        self.actors.move_to_end(key)
        return actor

    def _register_turn(self, event: RuntimeEvent,
                       session: SessionProjection, state: str) -> None:
        turn = self.turns.get(event.turn_id)
        if turn is None:
            turn = TurnProjection(
                turn_id=event.turn_id, run_id=event.run_id,
                session_id=event.session_id, state=state,
                last_event_at=event.occurred_at, workspace_id=event.workspace_id,
                actor_id=event.actor_id,
            )
            self.turns[event.turn_id] = turn
            if event.turn_id not in session._turn_ids:
                session._turn_ids.add(event.turn_id)
                session.turn_count += 1
        else:
            turn.state = state
            turn.last_event_at = event.occurred_at
            self.turns.move_to_end(event.turn_id)
        self.current_turns[event.run_id] = turn
        self.current_turns.move_to_end(event.run_id)
        session.terminal_state = state if state in TERMINAL_TURN_STATES else ""

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

    def reconcile_unknown(self, turn_id: str) -> bool:
        """Fail closed when restart cannot determine a non-terminal outcome."""
        with self._lock:
            turn = self.turns.get(turn_id)
            if turn is None or turn.state in TERMINAL_TURN_STATES:
                return False
            turn.state = TurnState.UNKNOWN
            session = self.sessions.get(turn.session_id)
            if session is not None:
                session.terminal_state = TurnState.UNKNOWN
            return True

    @classmethod
    def rebuild(cls, events_iterable: Iterable[RuntimeEvent], *,
                recent_limit: int = DEFAULT_RECENT_EVENTS,
                entity_limit: int = DEFAULT_ENTITY_LIMIT) -> "Projection":
        """Recreate the disposable view solely from its event stream."""
        projection = cls(recent_limit=recent_limit, entity_limit=entity_limit)
        for event in events_iterable:
            projection.register_event(event)
        return projection


def _child_actor_event(event: RuntimeEvent,
                       turn: Optional[TurnProjection],
                       actors: Optional[Mapping] = None) -> bool:
    """Allow a child actor with a known parent, not a forged root binding."""
    if turn is None or event.kind.startswith("turn."):
        return False
    parent = event.payload.get("parent_actor_id")
    child = event.payload.get("actor_id", event.actor_id)
    if (event.actor_id == turn.actor_id or not isinstance(parent, str)
            or not parent or not isinstance(child, str)
            or child != event.actor_id or not child):
        return False
    if parent == turn.actor_id:
        return True
    if actors is None:
        return False
    parent_actor = _actor_mapping_get(actors, turn.turn_id, parent)
    return bool(parent_actor and parent_actor.turn_id == turn.turn_id
                and parent_actor.run_id == turn.run_id)


def _actor_mapping_get(actors: Mapping, turn_id: str,
                       actor_id: str) -> Optional[ActorState]:
    direct = actors.get((turn_id, actor_id))
    if isinstance(direct, ActorState):
        return direct
    legacy = actors.get(actor_id)
    return legacy if isinstance(legacy, ActorState) else None


def _actor_key(turn_id: str, actor_id: str) -> tuple[str, str]:
    return turn_id, actor_id


def _parent_actor_authority(actors: Mapping, turn_id: str,
                            parent_actor_id: str) -> ActorAuthority:
    if not parent_actor_id:
        return ActorAuthority()
    parent = actors.get(_actor_key(turn_id, parent_actor_id))
    return (parent.authority if isinstance(parent, ActorState)
            else ActorAuthority())


def _actor_text(value: Any, fallback: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    return value[:maximum]


def _actor_status(event: RuntimeEvent) -> str:
    payload = event.payload
    if event.kind == "actor.spawned":
        return _actor_text(payload.get("status"), "RUNNING", 512).upper()
    if event.kind == "actor.completed":
        explicit = _actor_text(payload.get("status"), "", 512).upper()
        if explicit:
            return explicit
        return "SUCCEEDED" if payload.get("success") is True else \
            "FAILED" if payload.get("success") is False else "COMPLETED"
    return _actor_text(
        payload.get("state", payload.get("status", payload.get("phase", ""))),
        "RUNNING", 512).upper()


def _actor_terminal(event: RuntimeEvent) -> bool:
    if event.kind == "actor.completed":
        return True
    phase = event.payload.get("phase", event.payload.get("status", ""))
    return isinstance(phase, str) and phase.lower() in {
        "completed", "finished", "failed", "stopped", "interrupted",
        "cancelled", "canceled",
    }


def _actor_success(event: RuntimeEvent) -> Optional[bool]:
    value = event.payload.get("success")
    return value if isinstance(value, bool) else None


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


def _turn_state(event: RuntimeEvent) -> str:
    kind = event.kind
    state = {
        "turn.queued": TurnState.QUEUED,
        "turn.started": TurnState.RUNNING,
        "turn.steered": TurnState.RUNNING,
        "turn.interrupting": TurnState.INTERRUPTING,
        "turn.completed": TurnState.SUCCEEDED,
        "turn.failed": TurnState.FAILED,
        "turn.interrupted": TurnState.INTERRUPTED,
    }.get(kind)
    if state is None:
        raise ValueError(f"unknown turn event kind: {kind}")
    if kind == "turn.failed" and event.payload.get("state") == TurnState.UNKNOWN:
        return TurnState.UNKNOWN
    return state
