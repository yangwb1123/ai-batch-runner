"""Read-only RuntimeEvent bridge for native pi-batch pipeline execution.

The observer adds no execution authority.  It turns the existing pipeline
lifecycle into bounded events only when a caller explicitly supplies a sink.
A failing presentation sink disables observation rather than changing the
pipeline result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import contextvars
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

from .tui_protocol import RuntimeEvent
from .tui_view_support import clean_text, one_line


EventSink = Callable[[RuntimeEvent], None]
_FIELD_BYTES = 2048
_NATIVE_RUNTIME = "pbatch-native"
_ACTOR_PARENT_LIMIT = 4096
_NATIVE_APPROVAL_TTL = 120.0
_NATIVE_APPROVAL_OUTPUT_LIMIT = 32
_NATIVE_TASK_LIMIT = 4096
_NATIVE_OBSERVER: contextvars.ContextVar[Optional["NativePipelineObserver"]] = \
    contextvars.ContextVar("pbatch_native_observer", default=None)
_ID_BYTES = 256
_NATIVE_DECISIONS = frozenset({"allow", "deny", "cancel"})
_NATIVE_DECISION_ALIASES = {
    "accept": "allow", "approved": "allow",
    "acceptforsession": "allow_session", "approved_for_session": "allow_session",
    "decline": "deny", "denied": "deny", "abort": "cancel",
    "interrupt": "cancel",
}


@dataclass
class _NativeApprovalEntry:
    request_id: str
    approval_type: str
    stage: str
    target: str
    received_at: str
    expires_at: str
    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    actor_id: str
    decision: str = ""


class NativePipelineApprovalChannel:
    """Run-scoped approval IPC for an explicitly attached native pipeline.

    The channel is deliberately separate from the read-only event attachment:
    events describe the request and outcome, while this object is the only
    capability that can unblock the pipeline's approval wait.
    """

    def __init__(self, *, ttl: float = _NATIVE_APPROVAL_TTL) -> None:
        if (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                or not ttl > 0 or ttl != ttl or ttl == float("inf")):
            raise ValueError("native approval ttl must be finite and positive")
        self.ttl = min(float(ttl), 24 * 60 * 60)
        self._condition = threading.Condition(threading.RLock())
        self._observer: Optional["NativePipelineObserver"] = None
        self._pending: Optional[_NativeApprovalEntry] = None
        self._closed = False
        self._next_id = 0

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def bind(self, observer: "NativePipelineObserver") -> None:
        if not isinstance(observer, NativePipelineObserver):
            raise TypeError("native approval channel requires a native observer")
        with self._condition:
            if self._closed:
                raise RuntimeError("native approval channel is closed")
            if self._observer is not None and self._observer is not observer:
                raise RuntimeError("native approval channel is already bound")
            self._observer = observer

    def request(self, stage: str, outputs: list[str]) -> bool:
        """Block the pipeline until an exact-scope decision or bounded expiry."""
        stage_name = _text(stage, _FIELD_BYTES)
        target = _approval_target(outputs)
        with self._condition:
            observer = self._observer
            if self._closed or observer is None or self._pending is not None:
                return False
            self._next_id += 1
            entry = _NativeApprovalEntry(
                request_id=f"native_approval_{self._next_id}_{uuid.uuid4().hex}",
                approval_type="pipeline_stage", stage=stage_name, target=target,
                received_at=_timestamp(), expires_at=_timestamp(self.ttl),
                workspace_id=observer._cwd, session_id=observer.session_id,
                run_id=observer.run_id, turn_id=observer.turn_id,
                actor_id=observer.actor_id)
            self._pending = entry
        if not observer.approval_requested(entry):
            self._remove_if_pending(entry)
            return False
        expired = False
        deadline = time.monotonic() + self.ttl
        with self._condition:
            while self._pending is entry:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending = None
                    entry.decision = "deny"
                    self._condition.notify_all()
                    expired = True
                    break
                self._condition.wait(min(remaining, 0.5))
            decision = entry.decision
        if expired:
            observer.approval_expired(entry, "native approval TTL expired")
        return decision == "allow"

    def resolve(self, decision: Any) -> bool:
        """Resolve only a still-pending request with all scope fields present."""
        if not isinstance(decision, dict):
            return False
        with self._condition:
            entry = self._pending
            if entry is None or self._closed:
                return False
            if not _native_scope_matches(entry, decision):
                return False
            if decision.get("approval_type") != entry.approval_type:
                return False
            value = str(decision.get("decision", "") or "").strip().lower()
            value = _NATIVE_DECISION_ALIASES.get(value, value)
            if value not in _NATIVE_DECISIONS:
                return False
            entry.decision = value
            self._pending = None
            observer = self._observer
        if observer is not None:
            observer.approval_resolved(entry, value)
        with self._condition:
            self._condition.notify_all()
        return True

    def matches(self, request: Any) -> bool:
        """Check the exact projected request scope before exposing the control."""
        with self._condition:
            entry = self._pending
            if entry is None or self._closed:
                return False
            return _native_request_matches(entry, request)

    def close(self, reason: str = "native pipeline ended") -> None:
        """Wake a waiter without sending a provider/API cancellation request."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            entry, observer = self._pending, self._observer
            self._pending = None
            if entry is not None:
                entry.decision = "cancel"
            self._condition.notify_all()
        if entry is not None and observer is not None:
            observer.approval_expired(entry, _text(reason, _FIELD_BYTES))

    def _remove_if_pending(self, entry: _NativeApprovalEntry) -> None:
        with self._condition:
            if self._pending is entry:
                self._pending = None
                self._condition.notify_all()


class _NativeTaskCancelEvent:
    """An event that wakes for either the whole run or one task."""

    def __init__(self, global_event: threading.Event,
                 task_event: threading.Event) -> None:
        self._global = global_event
        self._task = task_event

    def is_set(self) -> bool:
        return self._global.is_set() or self._task.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            remaining = 0.25 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            if self._task.wait(min(0.05, remaining)):
                return True
            if self._global.is_set():
                return True
        return True


class _NativePipelineCancelRouter:
    """Keep global cancellation compatible while routing task cancellation."""

    def __init__(self, channel: "NativePipelineControlChannel") -> None:
        self._channel = channel

    def is_set(self) -> bool:
        return self._channel.cancel_event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._channel.cancel_event.wait(timeout)

    def for_task(self, task: Any):
        return self._channel.cancel_event_for_task(task)


class NativePipelineControlChannel(NativePipelineApprovalChannel):
    """Approval channel plus exact-scope cooperative Pipeline cancellation."""

    def __init__(self, *, ttl: float = _NATIVE_APPROVAL_TTL) -> None:
        super().__init__(ttl=ttl)
        self._cancel_requested = False
        self._cancel_event = threading.Event()
        self._task_events: dict[str, threading.Event] = {}
        self._task_owners: dict[int, str] = {}
        self._cancel_router = _NativePipelineCancelRouter(self)

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def cancel_router(self) -> _NativePipelineCancelRouter:
        return self._cancel_router

    def register_task(self, actor_id: str, task: Any) -> bool:
        """Register one live task actor before its provider process starts."""
        safe_id = _text(actor_id, _ID_BYTES)
        if not safe_id or task is None:
            return False
        with self._condition:
            observer = self._observer
            if (self._closed or observer is None
                    or safe_id not in observer._task_parents
                    or (safe_id not in self._task_events
                        and len(self._task_events) >= _NATIVE_TASK_LIMIT)):
                return False
            event = self._task_events.setdefault(safe_id, threading.Event())
            self._task_owners[id(task)] = safe_id
            if self._cancel_requested:
                event.set()
        return True

    def unregister_task(self, actor_id: str, task: Any = None) -> None:
        safe_id = _text(actor_id, _ID_BYTES)
        with self._condition:
            self._task_events.pop(safe_id, None)
            if task is not None:
                self._task_owners.pop(id(task), None)

    def cancel_event_for_task(self, task: Any):
        with self._condition:
            actor_id = self._task_owners.get(id(task), "")
            event = self._task_events.get(actor_id)
            if event is None:
                return self._cancel_event
            return _NativeTaskCancelEvent(self._cancel_event, event)

    def request_actor_cancel(self, scope: Any, actor_id: Any) -> bool:
        """Cancel one registered local task, never the provider turn."""
        safe_id = _text(actor_id, _ID_BYTES)
        with self._condition:
            observer = self._observer
            event = self._task_events.get(safe_id)
            if (self._closed or observer is None or not observer.started
                    or self._cancel_requested or event is None
                    or not _observer_scope_matches(observer, scope)):
                return False
            event.set()
        observer.task_cancel_requested(safe_id)
        return True

    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancel_requested

    def resolve(self, decision: Any) -> bool:
        if isinstance(decision, dict) and str(
                decision.get("decision", "") or "").strip().lower() == "cancel":
            with self._condition:
                entry = self._pending
                valid = (entry is not None
                         and decision.get("request_id") == entry.request_id
                         and decision.get("approval_type") == entry.approval_type
                         and _native_scope_matches(entry, decision))
            if not valid:
                return False
            scope = {name: decision.get(name) for name in (
                "workspace_id", "session_id", "run_id", "turn_id", "actor_id")}
            return self.request_cancel(
                scope, request_id=decision.get("request_id"),
                approval_type=decision.get("approval_type"))
        return super().resolve(decision)

    def request_cancel(self, scope: Any, *, request_id: Any = None,
                       approval_type: str = "") -> bool:
        """Announce cancellation only for the channel's bound run identity."""
        with self._condition:
            observer = self._observer
            entry = self._pending
            if (self._closed or observer is None or not observer.started
                    or not _observer_scope_matches(observer, scope)):
                return False
            if request_id is not None and (
                    entry is None or entry.request_id != request_id
                    or approval_type != entry.approval_type):
                return False
            if self._cancel_requested:
                return True
            self._cancel_requested = True
            self._cancel_event.set()
            for task_event in self._task_events.values():
                task_event.set()
            entry = self._pending
            self._pending = None
            if entry is not None:
                entry.decision = "cancel"
        observer.pipeline_cancel_requested()
        if entry is not None:
            observer.approval_resolved(entry, "cancel")
        with self._condition:
            self._condition.notify_all()
        return True


class NativePipelineAttachment:
    """Validate a native observer stream before handing it to a TUI consumer."""

    def __init__(self, workspace_id: str, consumer: EventSink) -> None:
        expected = _text(workspace_id, _FIELD_BYTES)
        if not expected:
            raise ValueError("native pipeline workspace_id must not be empty")
        if not callable(consumer):
            raise TypeError("native pipeline consumer must be callable")
        self.workspace_id = expected
        self._consumer = consumer
        self._lock = threading.Lock()
        self._closed = False
        self.events_accepted = 0

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __call__(self, event: RuntimeEvent) -> None:
        """Accept only read-only events emitted by the native observer."""
        with self._lock:
            if self._closed:
                raise RuntimeError("native pipeline attachment is closed")
            try:
                if not isinstance(event, RuntimeEvent):
                    raise TypeError(
                        "native pipeline attachment requires RuntimeEvent")
                if event.workspace_id != self.workspace_id:
                    raise ValueError(
                        "native pipeline workspace does not match attachment")
                if (event.extensions.get("runtime") != _NATIVE_RUNTIME
                        or event.extensions.get("read_only") is not True):
                    raise ValueError(
                        "event is not a read-only native pipeline event")
                self._consumer(event)
            except Exception:
                self._closed = True
                raise
            self.events_accepted += 1

    def close(self) -> None:
        """Stop accepting events; closing never controls the pipeline itself."""
        with self._lock:
            self._closed = True


class NativePipelineObserver:
    """Emit a bounded pipeline event stream with trusted child lineage."""

    def __init__(self, sink: EventSink, cwd: str, *, session_id: str = "",
                 run_id: str = "", turn_id: str = "",
                 actor_id: str = "pipeline") -> None:
        if not callable(sink):
            raise TypeError("event sink must be callable")
        self._sink = sink
        self._cwd = _text(cwd, _FIELD_BYTES)
        identity = uuid.uuid4().hex
        self.session_id = _text(session_id, _ID_BYTES) or f"pipeline_{identity}"
        self.run_id = _text(run_id, _ID_BYTES) or f"run_pipeline_{identity}"
        self.turn_id = _text(turn_id, _ID_BYTES) or f"turn_pipeline_{identity}"
        self.actor_id = _text(actor_id, _ID_BYTES) or "pipeline"
        self._seq = 0
        self._lock = threading.Lock()
        self._stage_actors: dict[str, str] = {}
        self._task_parents: dict[str, str] = {}
        self._meta_parents: dict[str, str] = {}
        self._approval_channel: Optional[NativePipelineApprovalChannel] = None
        self._plan_id = self._plan_digest = ""
        self._started = False
        self._cancel_announced = False
        self._enabled = True
        self.disabled_reason = ""
        self.events_emitted = 0

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def approval_channel(self) -> Optional[NativePipelineApprovalChannel]:
        return self._approval_channel

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    def bind_approval_channel(self, channel: NativePipelineApprovalChannel) -> None:
        if not isinstance(channel, NativePipelineApprovalChannel):
            raise TypeError("native observer requires a native approval channel")
        if self._approval_channel is not None and self._approval_channel is not channel:
            raise RuntimeError("native observer already has an approval channel")
        self._approval_channel = channel

    def emit(self, kind: str, payload: Optional[dict[str, Any]] = None,
             *, actor_id: str = "") -> bool:
        """Send one event; observer failures never fail the pipeline."""
        with self._lock:
            if not self._enabled:
                return False
            self._seq += 1
            event = RuntimeEvent(
                event_id=f"evt_native_{uuid.uuid4().hex}", seq=self._seq,
                occurred_at=datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"),
                workspace_id=self._cwd, session_id=self.session_id,
                run_id=self.run_id, turn_id=self.turn_id,
                actor_id=_text(actor_id, _ID_BYTES) or self.actor_id, kind=kind,
                payload=dict(payload or {}),
                extensions=self._event_extensions(),
            )
            try:
                self._sink(event)
            except Exception as exc:
                self._enabled = False
                self.disabled_reason = _text(exc, _FIELD_BYTES)
                return False
            self.events_emitted += 1
            return True

    def _event_extensions(self) -> dict[str, Any]:
        extensions = {"runtime": "pbatch-native", "read_only": True}
        if self._plan_id:
            extensions["plan_id"] = self._plan_id
            extensions["plan_digest"] = self._plan_digest
        return extensions

    def start(self, name: str, stage_count: int) -> None:
        """Open the pipeline turn before any stage facts are emitted."""
        with self._lock:
            self._started = True
        self.emit("turn.queued", {"state": "QUEUED"})
        self.emit("turn.started", {"state": "RUNNING"})
        self.emit("pipeline.started", {
            "name": _text(name, _FIELD_BYTES),
            "stage_count": _count(stage_count),
        })

    def plan_updated(self, payload: dict[str, Any]) -> None:
        """Publish a bounded explicit plan without granting execution rights."""
        if not isinstance(payload, dict):
            return
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            return
        digest = hashlib.sha256(encoded).hexdigest()
        plan_id = _text(payload.get("plan_id"), _ID_BYTES) or f"plan_{digest[:24]}"
        self._plan_id, self._plan_digest = plan_id, digest
        bounded = dict(payload)
        bounded.update({"plan_id": plan_id, "plan_digest": digest})
        self.emit("plan.updated", bounded)

    def stage_started(self, name: str, index: int, total: int) -> None:
        actor_id = self.stage_id(name, index)
        self._remember_stage_actor(name, actor_id)
        self._actor_spawned(actor_id, self.actor_id, name, "pipeline.stage")
        self.emit("stage.started", self._child_payload({
            "item_id": actor_id, "name": _text(name, _FIELD_BYTES),
            "index": _count(index), "total": _count(total),
        }, actor_id, self.actor_id), actor_id=actor_id)

    def stage_completed(self, name: str, index: int, total: int,
                        success: bool, output_count: int = 0,
                        cancelled: bool = False) -> None:
        actor_id = self._stage_actors.get(
            _text(name, 256), self.stage_id(name, index))
        self.emit("stage.completed", self._child_payload({
            "item_id": actor_id, "name": _text(name, _FIELD_BYTES),
            "index": _count(index), "total": _count(total),
            "success": bool(success), "output_count": _count(output_count),
            "cancelled": bool(cancelled),
        }, actor_id, self.actor_id), actor_id=actor_id)
        if actor_id != self.actor_id:
            self._actor_completed(
                actor_id, self.actor_id, bool(success),
                "CANCELLED" if cancelled else "")

    def pipeline_checkpoint(self, stage: str, index: int, total: int,
                            success: bool, task_count: int = 0,
                            successful_tasks: int = 0,
                            cancelled: bool = False) -> None:
        """Publish a bounded stage boundary for diagnostic recovery only."""
        completed = _count(index) if success else max(0, _count(index) - 1)
        next_index = _count(index) + 1 if success else _count(index)
        self.emit("pipeline.checkpoint", {
            "checkpoint_id": _item_id("checkpoint", stage, index),
            "stage": _text(stage, _FIELD_BYTES),
            "stage_index": _count(index), "stage_total": _count(total),
            "completed_stages": completed, "next_stage_index": next_index,
            "task_count": _count(task_count),
            "successful_tasks": _count(successful_tasks),
            "success": bool(success), "cancelled": bool(cancelled),
            "resume_boundary": ("next_stage" if success else "retry_stage"),
        })

    def stage_id(self, name: str, index: int) -> str:
        return _item_id("stage", name, index)

    def task_id(self, stage: str, index: int, task: Any = None) -> str:
        return _item_id("task", stage, index)

    def meta_role_started(self, stage: str, iteration: int, role: str,
                          index: int, provider: str = "") -> str:
        actor_id = _stable_actor_id("meta", stage, iteration, role, index)
        parent = self._stage_actors.get(_text(stage, 256), self.actor_id)
        if len(self._meta_parents) < _ACTOR_PARENT_LIMIT:
            self._meta_parents[actor_id] = parent
        self._actor_spawned(actor_id, parent, role, "pipeline.meta-role",
                            provider)
        return actor_id

    def meta_role_completed(self, actor_id: str, stage: str,
                            success: bool, cancelled: bool = False) -> None:
        safe_id = _text(actor_id, _ID_BYTES)
        parent = (self._meta_parents.get(safe_id) or
                  self._stage_actors.get(_text(stage, 256), self.actor_id))
        if safe_id and parent and safe_id != self.actor_id:
            self._actor_completed(
                safe_id, parent, bool(success),
                "CANCELLED" if cancelled else "")

    def task_started(self, stage: str, index: int, total: int, task: Any) -> str:
        item_id = self.task_id(stage, index, task)
        parent = self._stage_actors.get(_text(stage, 256), self.actor_id)
        self._remember_task_parent(item_id, parent)
        self._actor_spawned(
            item_id, parent, f"{_text(stage, 256)} task {index}",
            "pipeline.task", _task_agent(task))
        self.emit("task.started", self._child_payload({
            "item_id": item_id, "name": f"{_text(stage, 256)} task {index}",
            "index": _count(index), "total": _count(total),
            "agent": _task_agent(task),
            "output": _text(getattr(task, "output", ""), _FIELD_BYTES),
        }, item_id, parent), actor_id=item_id)
        return item_id

    def task_cancel_requested(self, item_id: str) -> bool:
        actor_id, parent = self._task_actor(item_id)
        if not actor_id or not parent:
            return False
        return self.emit("actor.status", self._child_payload({
            "status": "INTERRUPTING", "phase": "local_cancel_requested",
        }, actor_id, parent), actor_id=actor_id)

    def task_completed(self, item_id: str, task: Any, result: Any, *,
                       stage: str = "") -> None:
        success = bool(getattr(result, "success", False))
        actor_id, parent = self._task_actor(item_id, stage)
        cancelled = _result_cancelled(result)
        payload: dict[str, Any] = self._child_payload({
            "item_id": _text(item_id, _ID_BYTES),
            "name": _text(getattr(task, "output", "") or "task", _FIELD_BYTES),
            "success": success,
            "cancelled": cancelled,
            "returncode": _integer(getattr(result, "returncode", 0)),
        }, actor_id, parent)
        if cancelled:
            payload["status"] = "CANCELLED"
        reason = _text(getattr(result, "reason", ""), _FIELD_BYTES)
        if reason:
            payload["reason"] = reason
        self.emit("task.completed", payload, actor_id=actor_id)
        if actor_id != self.actor_id:
            self._actor_completed(
                actor_id, parent, success, "CANCELLED" if cancelled else "")

    def pipeline_cancel_requested(self) -> bool:
        with self._lock:
            if self._cancel_announced:
                return True
            self._cancel_announced = True
        return self.emit("turn.interrupting", {
            "state": "INTERRUPTING",
            "reason": "native pipeline cancellation requested",
        }, actor_id=self.actor_id)

    def approval_requested(self, entry: _NativeApprovalEntry) -> bool:
        return self.emit("approval.requested", {
            "request_id": entry.request_id,
            "approval_type": entry.approval_type,
            "method": "pipeline.stage.approval",
            "target": entry.target,
            "received_at": entry.received_at,
            "expires_at": entry.expires_at,
            "available_decisions": ["allow", "deny", "cancel"],
        }, actor_id=self.actor_id)

    def approval_resolved(self, entry: _NativeApprovalEntry,
                          decision: str) -> bool:
        return self.emit("approval.resolved", {
            "request_id": entry.request_id,
            "approval_type": entry.approval_type,
            "decision": _text(decision, 64),
        }, actor_id=self.actor_id)

    def approval_expired(self, entry: _NativeApprovalEntry, reason: str) -> bool:
        return self.emit("approval.expired", {
            "request_id": entry.request_id,
            "approval_type": entry.approval_type,
            "reason": _text(reason, _FIELD_BYTES),
        }, actor_id=self.actor_id)

    def validation_started(self, item_id: str, task: Any, *,
                           stage: str = "") -> None:
        actor_id, parent = self._task_actor(item_id, stage)
        self.emit("validation.started", self._child_payload({
            "item_id": _text(item_id, _ID_BYTES),
            "name": _text(getattr(task, "validate", "") or "validation", 256),
        }, actor_id, parent), actor_id=actor_id)

    def validation_completed(self, item_id: str, task: Any, result: Any, *,
                             stage: str = "") -> None:
        actor_id, parent = self._task_actor(item_id, stage)
        cancelled = _result_cancelled(result)
        self.emit("validation.completed", self._child_payload({
            "item_id": _text(item_id, _ID_BYTES),
            "name": _text(getattr(task, "validate", "") or "validation", 256),
            "success": bool(getattr(result, "validation_ok", True)),
            "cancelled": cancelled,
            "validation_exit": _integer(getattr(result, "validation_exit", 0)),
        }, actor_id, parent), actor_id=actor_id)

    def _remember_stage_actor(self, name: str, actor_id: str) -> None:
        if len(self._stage_actors) < _ACTOR_PARENT_LIMIT:
            self._stage_actors[_text(name, 256)] = actor_id

    def _remember_task_parent(self, item_id: str, parent: str) -> None:
        if len(self._task_parents) < _ACTOR_PARENT_LIMIT:
            self._task_parents[_text(item_id, _ID_BYTES)] = parent

    def _task_actor(self, item_id: str, stage: str = "") -> tuple[str, str]:
        safe_id = _text(item_id, _ID_BYTES)
        parent = (self._task_parents.get(safe_id) or
                  self._stage_actors.get(_text(stage, 256), ""))
        if not safe_id or not parent:
            return self.actor_id, ""
        return safe_id, parent

    def _actor_spawned(self, actor_id: str, parent: str, name: Any,
                       role: str, provider: str = "") -> None:
        self.emit("actor.spawned", self._child_payload({
            "name": _text(name, _FIELD_BYTES) or actor_id,
            "role": _text(role, 256), "provider": _text(provider, 256),
            "status": "RUNNING",
        }, actor_id, parent), actor_id=actor_id)

    def _actor_completed(self, actor_id: str, parent: str,
                         success: bool, status: str = "") -> None:
        self.emit("actor.completed", self._child_payload({
            "success": bool(success),
            "status": _text(status, 64) or ("SUCCEEDED" if success else "FAILED"),
        }, actor_id, parent), actor_id=actor_id)

    def _child_payload(self, payload: dict[str, Any], actor_id: str,
                       parent: str) -> dict[str, Any]:
        if actor_id != self.actor_id:
            payload["actor_id"] = _text(actor_id, _ID_BYTES)
            payload["parent_actor_id"] = _text(parent, _ID_BYTES)
        return payload

    def gate_verdict(self, stage: str, verdict: str, reason: str = "",
                     artifact_count: int = 0) -> None:
        payload: dict[str, Any] = {
            "item_id": _item_id("gate", stage, 0),
            "gate": _text(stage, _FIELD_BYTES),
            "verdict": _text(verdict, 64).upper(),
            "artifact_count": _count(artifact_count),
        }
        if reason:
            payload["reason"] = _text(reason, _FIELD_BYTES)
        self.emit("gate.verdict", payload)

    def finish(self, success: bool, *, failed_stages: tuple[str, ...] = (),
               cancelled: bool = False) -> None:
        """Close pipeline facts with a terminal event exactly once."""
        if cancelled and not self._cancel_announced:
            self.pipeline_cancel_requested()
        payload: dict[str, Any] = {
            "name": "pipeline", "success": bool(success),
            "cancelled": bool(cancelled),
        }
        if failed_stages:
            payload["failed_stages"] = [_text(item, 256) for item in failed_stages[:32]]
        self.emit("pipeline.completed" if success and not cancelled
                  else "pipeline.failed", payload)
        if cancelled:
            self.emit("turn.interrupted", {
                "state": "INTERRUPTED", "reason": "pipeline cancelled",
            })
        else:
            self.emit("turn.completed" if success else "turn.failed", {
                "state": "SUCCEEDED" if success else "FAILED",
                "reason": "pipeline failed" if not success else "",
            })


def _observer_scope_matches(observer: NativePipelineObserver,
                             scope: Any) -> bool:
    if not isinstance(scope, dict):
        return False
    return (scope.get("workspace_id") == observer._cwd
            and all(scope.get(name) == getattr(observer, name)
                    for name in ("session_id", "run_id", "turn_id",
                                 "actor_id")))


def _native_scope_matches(entry: _NativeApprovalEntry,
                          decision: dict[str, Any]) -> bool:
    fields = ("workspace_id", "session_id", "run_id", "turn_id", "actor_id")
    return all(decision.get(name) == getattr(entry, name) for name in fields)


def _native_request_matches(entry: _NativeApprovalEntry, request: Any) -> bool:
    if getattr(request, "request_id", None) != entry.request_id:
        return False
    if type(getattr(request, "request_id", None)) is not str:
        return False
    return all(getattr(request, name, None) == getattr(entry, name)
               for name in ("workspace_id", "session_id", "run_id",
                            "turn_id", "actor_id", "approval_type"))


def _approval_target(outputs: Any) -> str:
    if not isinstance(outputs, (list, tuple)):
        return "stage outputs unavailable"
    values = [_text(item, _FIELD_BYTES) for item in outputs[:_NATIVE_APPROVAL_OUTPUT_LIMIT]]
    return ", ".join(item for item in values if item) or "stage outputs unavailable"


def _timestamp(offset: float = 0.0) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, offset))
    return value.isoformat().replace("+00:00", "Z")


def _result_cancelled(result: Any) -> bool:
    reason = str(getattr(result, "reason", "") or "").strip().lower()
    return reason == "cancelled" or reason.startswith("cancelled;")


def _task_agent(task: Any) -> str:
    value = getattr(task, "effective_agent", "")
    try:
        value = value() if callable(value) else value
    except Exception:
        value = ""
    return _text(value, 256)


def _item_id(kind: str, name: Any, index: int) -> str:
    value = one_line(clean_text(name))[:160] or "anonymous"
    return f"{kind}_{index}_{value}"


def _stable_actor_id(kind: str, *values: Any) -> str:
    material = "\x1f".join(_text(value, 256) for value in values)
    digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()
    return f"{kind}_{digest[:24]}"


def _text(value: Any, maximum: int) -> str:
    text = one_line(clean_text(value))
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", errors="ignore") + "..."


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def current_native_observer() -> Optional[NativePipelineObserver]:
    return _NATIVE_OBSERVER.get()


def current_native_approval() -> Optional[NativePipelineApprovalChannel]:
    observer = current_native_observer()
    return observer.approval_channel if observer is not None else None


def open_native_observation(event_sink: Optional[EventSink], cwd: str,
                            dry_run: bool,
                            approval_channel: Optional[NativePipelineApprovalChannel] = None):
    if approval_channel is not None and event_sink is None:
        raise ValueError("native approval channel requires an event sink")
    observer = (NativePipelineObserver(event_sink, cwd)
                if event_sink is not None else None)
    if observer is not None and approval_channel is not None:
        approval_channel.bind(observer)
        observer.bind_approval_channel(approval_channel)
    token = _NATIVE_OBSERVER.set(observer)
    return observer, token, observer is not None and not dry_run


def close_native_observation(observer: Optional[NativePipelineObserver], token,
                             observed: bool, finished: bool,
                             failed_stages: list[str],
                             cancelled: bool = False) -> None:
    try:
        if observer is not None and observer.approval_channel is not None:
            observer.approval_channel.close()
        if observed and observer is not None:
            observer.finish(
                finished and not failed_stages and not cancelled,
                failed_stages=tuple(failed_stages), cancelled=cancelled)
    finally:
        _NATIVE_OBSERVER.reset(token)


__all__ = [
    "NativePipelineApprovalChannel", "NativePipelineAttachment",
    "NativePipelineControlChannel",
    "NativePipelineObserver", "close_native_observation",
    "current_native_approval", "current_native_observer",
    "open_native_observation",
]
