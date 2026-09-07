"""Synchronous, bounded presentation controller for TUI runtime events.

Runtime adapters remain the only trusted source of event kinds.  This module
folds accepted events into immutable views without interpreting body text as
control protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import queue
import re
import threading
from typing import Any, Optional

from .tui_actor_tree import (
    ActorHistorySnapshot, ActorTreeSnapshot, build_actor_history,
    build_actor_tree,
)
from .tui_events import EventBus, Projection, _child_actor_event
from .tui_findings import FindingsSnapshot, findings_snapshot
from .tui_journal import (
    JournalRecovery, RuntimeEventJournal, RuntimeEventJournalError,
    stable_plan_identity,
)
from .tui_session_summary import (
    TUISessionSummary, advance_session_summary, build_session_summary,
    write_session_summary_cache,
)
from .tui_messages import MessageProjection
from .tui_artifacts import ArtifactCatalogSnapshot, build_artifact_catalog
from .tui_approvals import ApprovalProjection, ApprovalSnapshot
from .tui_completion import CompletionSnapshot, build_completion_snapshot
from .tui_checkpoint import (
    build_projection_checkpoint, verify_projection_checkpoint,
    write_projection_checkpoint,
)
from .tui_protocol import (
    RUNTIME_IDENTITY_FIELDS, RuntimeEvent, TERMINAL_TURN_STATES, TurnState,
)
from .tui_policy import PolicyProjection, PolicySnapshot
from .tui_plan_evidence import PlanEvidenceProjection, PlanEvidenceSnapshot
from .tui_pipeline_checkpoint import (
    PipelineCheckpointProjection, PipelineCheckpointSnapshot,
)
from .tui_timeline import PlanSnapshot, TimelineEntry, TimelineLog


TEXT_BYTES = 64 * 1024
PATH_BYTES = 512
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LATE_MUTABLE_PREFIXES = ("message.", "command.", "tool.", "approval.")
_LATE_MUTABLE_KINDS = frozenset({
    "plan.updated", "policy.updated", "review.finding"})
_TIMELINE_LIFECYCLES = {
    "pipeline.started": ("pipeline", "started"),
    "pipeline.checkpoint": ("pipeline", "checkpoint"),
    "pipeline.completed": ("pipeline", "completed"),
    "pipeline.failed": ("pipeline", "failed"),
    "stage.started": ("stage", "started"),
    "stage.completed": ("stage", "completed"),
    "task.started": ("task", "started"),
    "task.completed": ("task", "completed"),
    "validation.started": ("validation", "started"),
    "validation.completed": ("validation", "completed"),
    "gate.verdict": ("gate", "completed"),
    "actor.spawned": ("actor", "spawned"),
    "actor.status": ("actor", "status"),
    "actor.completed": ("actor", "completed"),
    "review.finding": ("review", "completed"),
}


@dataclass(frozen=True)
class WorkspaceView:
    """Immutable current workspace and last-turn change projection."""

    phase: str = ""
    available: bool = False
    root: str = ""
    branch: str = ""
    head: str = ""
    changed: bool = False
    dirty_files: int = 0
    paths: tuple[str, ...] = ()
    observed_paths: tuple[str, ...] = ()
    head_changed: bool = False
    paths_truncated: bool = False


@dataclass(frozen=True)
class TimelineItemView:
    """Defensive immutable copy of one timeline entry."""

    kind: str
    ts: float | str
    summary: str
    detail: str = ""
    exit_code: Optional[int] = None
    collapsed: bool = False
    plan: Optional[PlanSnapshot] = None


@dataclass(frozen=True)
class TimelineView:
    """Immutable timeline snapshot with rendering parity helpers."""

    entries: tuple[TimelineItemView, ...] = ()
    summary: str = ""

    def _as_log(self) -> TimelineLog:
        log = TimelineLog(max_entries=max(1, len(self.entries)))
        for item in self.entries:
            log.append(TimelineEntry(
                item.kind, item.ts, item.summary, item.detail,
                item.exit_code, item.collapsed, item.plan))
        return log

    def render_lines(self, width: int) -> list[str]:
        """Render a fresh list; callers cannot mutate the stored view."""
        return self._as_log().render_lines(width)

    def render_summary(self, width: int) -> str:
        """Render the captured summary at a bounded display width."""
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            return ""
        return _truncate(self.summary, width)


@dataclass(frozen=True)
class RecoverySnapshot:
    """Bounded read-only metadata about journal reconstruction."""

    mode: str = "live"
    applied_events: int = 0
    skipped_events: int = 0
    warning_count: int = 0
    unresolved_state: str = ""
    degraded: bool = False
    read_only: bool = False


@dataclass(frozen=True)
class TUIViewSnapshot:
    """Immutable presentation state after one accepted runtime event."""

    revision: int
    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    actor_id: str
    turn_state: str
    assistant_text: str
    error_text: str
    assistant_text_start: int
    error_text_start: int
    assistant_generation: int
    workspace: WorkspaceView
    timeline: TimelineView
    completion: CompletionSnapshot
    artifacts: ArtifactCatalogSnapshot
    approvals: ApprovalSnapshot
    actors: ActorTreeSnapshot = field(default_factory=ActorTreeSnapshot)
    findings: FindingsSnapshot = field(default_factory=FindingsSnapshot)
    actor_history: ActorHistorySnapshot = field(
        default_factory=ActorHistorySnapshot)
    policy: PolicySnapshot = field(default_factory=PolicySnapshot)
    plan_evidence: PlanEvidenceSnapshot = field(
        default_factory=PlanEvidenceSnapshot)
    pipeline_checkpoint: PipelineCheckpointSnapshot = field(
        default_factory=PipelineCheckpointSnapshot)
    recovery: RecoverySnapshot = field(
        default_factory=RecoverySnapshot, compare=False)


@dataclass(frozen=True)
class TUIRecoveryReport:
    """Diagnostics for a projection reconstructed without runtime callbacks."""

    journal: JournalRecovery = JournalRecovery()
    applied_events: int = 0
    skipped_events: int = 0
    warnings: tuple[str, ...] = ()
    degraded: bool = False
    unresolved_state: str = ""
    read_only: bool = False


class TUIEventController:
    """Validate, order and synchronously project runtime presentation state."""

    def __init__(self, bus_max_events: int = 1024, recent_limit: int = 200,
                 entity_limit: int = 1024,
                 journal: Optional[RuntimeEventJournal] = None):
        _positive_int(entity_limit, "entity_limit")
        self.bus = EventBus(max_events=bus_max_events, journal=journal)
        self._journal = journal
        self._delivery = self.bus.subscribe()
        self.projection = _new_projection(recent_limit, entity_limit)
        self._entity_limit = entity_limit
        self._timeline = TimelineLog(max_entries=entity_limit)
        self._lock = threading.RLock()
        self._closed = False
        self._revision = 0
        self._workspace_id = self._session_id = self._run_id = self._turn_id = ""
        self._actor_id = self._turn_state = ""
        self._assistant_text = self._error_text = ""
        self._assistant_text_start = self._error_text_start = 0
        self._assistant_generation = 0
        self._messages = MessageProjection(
            TEXT_BYTES, max_items=min(entity_limit, 256))
        self._approvals = ApprovalProjection(max_pending=min(entity_limit, 32))
        self._policy = PolicyProjection()
        self._plan_evidence = PlanEvidenceProjection()
        self._pipeline_checkpoint = PipelineCheckpointProjection()
        self._workspace_phase = ""
        self._workspace_meta: dict[str, Any] = {}
        self._workspace_delta: dict[str, Any] = {}
        self._observed_paths: list[str] = []
        self._recovery_report = TUIRecoveryReport()
        self._recovery_blocked = False
        self._recovery_block_reason = ""
        self._session_summary: Optional[TUISessionSummary] = None
        self._restored_summary_events: tuple[RuntimeEvent, ...] = ()
        self._checkpoint_status = "disabled" if journal is None else "missing"
        if journal is not None:
            recovery = journal.recover()
            self._restore(recovery)
            self._rebuild_session_summary(recovery)
            self._verify_projection_checkpoint(recovery)

    def ingest(self, event: RuntimeEvent) -> Optional[TUIViewSnapshot]:
        """Synchronously publish, drain, core-project and presentation-fold."""
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        with self._lock:
            self._ensure_open()
            if self._recovery_blocked:
                raise RuntimeError(self._recovery_block_reason)
            _validate_event_identities(event)
            if self.projection.validate_event(event):
                self._validate_presentation(event)
            terminal_before = self._terminal_before(event)
            event_id = event.event_id
            try:
                published = self.bus.publish(event)
            except RuntimeEventJournalError as exc:
                if not exc.retryable:
                    self._block_mutation(
                        f"runtime event journal is read-only: {exc}",
                        mark_journal=exc.journal_fatal)
                raise
            if not published:
                return None
            delivered = self._canonical_delivery(event_id)
            self._drain_public_delivery(event_id)
            try:
                if not self.projection.register_event(delivered):
                    return None
            except (OverflowError, RuntimeError, ValueError):
                self._block_mutation(
                    "runtime event was journaled but projection commit failed; "
                    "session is read-only", mark_journal=True)
                raise
            if terminal_before and _late_mutable(delivered.kind):
                return None
            self._fold(delivered)
            self._revision += 1
            self._advance_session_summary(delivered.event_id)
            self._write_projection_checkpoint(delivered)
            return self._snapshot_unlocked()

    def _canonical_delivery(self, event_id: str) -> RuntimeEvent:
        try:
            delivered = self._delivery.get(timeout=0)
        except queue.Empty as exc:
            raise RuntimeError("TUI controller canonical delivery is missing") from exc
        if delivered.event_id != event_id:
            raise RuntimeError("TUI controller canonical delivery was reordered")
        return delivered

    def _drain_public_delivery(self, event_id: str) -> None:
        try:
            queued = self.bus.get(timeout=0)
        except queue.Empty:
            return  # a diagnostics consumer observed the canonical delivery
        if queued.event_id != event_id:
            raise RuntimeError("TUI controller EventBus queue was externally reordered")

    def prepare_turn(self) -> TUIViewSnapshot:
        """Clear current presentation while preserving EventBus/Projection history."""
        with self._lock:
            self._ensure_open()
            self._reset_turn_presentation()
            self._revision += 1
            return self._snapshot_unlocked()

    def snapshot(self) -> TUIViewSnapshot:
        """Return a defensive immutable view without changing the revision."""
        with self._lock:
            return self._snapshot_unlocked()

    @property
    def recovery(self) -> TUIRecoveryReport:
        return self._recovery_report

    @property
    def checkpoint_status(self) -> str:
        """Return cache status; it never changes execution authorization."""
        with self._lock:
            return self._checkpoint_status

    def _verify_projection_checkpoint(self, recovery: JournalRecovery) -> None:
        if self._journal is None or not recovery.events:
            return
        try:
            _, status = verify_projection_checkpoint(
                self._journal, self._snapshot_unlocked(), recovery.total_bytes)
        except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
            status = "unavailable"
        self._checkpoint_status = status

    def _write_projection_checkpoint(self, event: RuntimeEvent) -> None:
        if self._journal is None:
            return
        try:
            checkpoint = build_projection_checkpoint(
                self._journal, event, self._snapshot_unlocked())
            if write_projection_checkpoint(self._journal, checkpoint):
                self._checkpoint_status = "written"
        except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
            self._checkpoint_status = "unavailable"

    @property
    def read_only_reason(self) -> str:
        with self._lock:
            return self._recovery_block_reason if self._recovery_blocked else ""

    def close(self) -> None:
        """Idempotently refuse future mutation; existing snapshots stay readable."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.bus.close()

    def _terminal_before(self, event: RuntimeEvent) -> bool:
        turn = self.projection.turns.get(event.turn_id)
        return bool(turn is not None and turn.state in TERMINAL_TURN_STATES)

    def _fold(self, event: RuntimeEvent) -> None:
        if (event.kind in {"turn.queued", "turn.started"}
                and event.turn_id and event.turn_id != self._turn_id):
            self._reset_turn_presentation()
        if event.workspace_id:
            self._workspace_id = event.workspace_id
        if event.session_id:
            self._session_id = event.session_id
        if event.run_id:
            self._run_id = event.run_id
        if event.turn_id:
            self._turn_id = event.turn_id
        turn = self.projection.turns.get(event.turn_id)
        if event.actor_id and not _child_actor_event(
                event, turn, self.projection.actors):
            self._actor_id = event.actor_id
        if turn is not None:
            self._turn_state = turn.state
        self._approvals.consume(event)
        self._policy.consume(event)
        self._plan_evidence.validate(event)
        self._plan_evidence.consume(event)
        self._pipeline_checkpoint.validate(event)
        self._pipeline_checkpoint.consume(event)
        if (self._approvals.snapshot().waiting
                and self._turn_state == TurnState.RUNNING):
            self._turn_state = TurnState.WAITING_APPROVAL
        self._fold_message(event)
        self._fold_error(event)
        self._fold_workspace(event)
        self._fold_timeline(event)

    def _restore(self, recovery: JournalRecovery) -> None:
        warnings = list(recovery.warnings)
        applied = skipped = 0
        accepted: list[RuntimeEvent] = []
        degraded = recovery.degraded
        for index, event in enumerate(recovery.events, 1):
            try:
                if not self.bus.restore_event(event):
                    skipped += 1
                    continue
                if not self.projection.register_event(event):
                    skipped += 1
                    continue
                self._fold(event)
                self._revision += 1
                applied += 1
                accepted.append(event)
            except (OverflowError, RuntimeError, ValueError) as exc:
                detail = f"projection rebuild stopped at event {index}: {exc}"
                warnings.append(detail)
                self._block_mutation(detail, mark_journal=True)
                degraded = True
                break
        unresolved = self._mark_recovered_turns_unknown(warnings)
        self._recovery_blocked = degraded or bool(unresolved)
        self._recovery_block_reason = (
            "recovered TUI journal is degraded and read-only" if degraded else
            "recovered TUI turn requires reconciliation and is read-only"
            if unresolved else "")
        self._recovery_report = TUIRecoveryReport(
            journal=recovery, applied_events=applied, skipped_events=skipped,
            warnings=tuple(warnings), degraded=degraded,
            unresolved_state=unresolved,
            read_only=self._recovery_blocked)
        self._restored_summary_events = tuple(accepted)

    def _rebuild_session_summary(self, recovery: JournalRecovery) -> None:
        if self._journal is None or not recovery.events:
            return
        report = self._recovery_report
        if (report.degraded or report.skipped_events
                or len(self._restored_summary_events) != len(recovery.events)):
            self._session_summary = None
            return
        try:
            self._session_summary = build_session_summary(
                self._restored_summary_events,
                journal_bytes=recovery.total_bytes,
                turn_state=self._turn_state,
                requires_reconciliation=self._requires_reconciliation(),
                read_only=self._recovery_blocked,
            )
            self._write_session_summary(recovery.total_bytes)
        except (OSError, RuntimeError, TypeError, ValueError):
            self._session_summary = None

    def _advance_session_summary(self, event_id: str) -> None:
        if self._journal is None:
            return
        recovery = self._journal.recovery
        if not recovery.events or recovery.events[-1].event_id != event_id:
            return
        if not self._summary_is_adjacent(recovery):
            self._session_summary = None
            return
        try:
            self._session_summary = advance_session_summary(
                self._session_summary, recovery.events[-1],
                journal_bytes=recovery.total_bytes,
                turn_state=self._turn_state,
                requires_reconciliation=self._requires_reconciliation(),
                read_only=self._recovery_blocked,
            )
            self._write_session_summary(recovery.total_bytes)
        except (OSError, RuntimeError, TypeError, ValueError):
            self._session_summary = None

    def _summary_is_adjacent(self, recovery: JournalRecovery) -> bool:
        """Reject an incremental cache if another writer inserted an event."""
        current = recovery.events[-1]
        current_bytes = len(current.to_jsonl().encode("utf-8"))
        previous_bytes = recovery.total_bytes - current_bytes
        if previous_bytes < 0:
            return False
        if len(recovery.events) == 1:
            return self._session_summary is None and previous_bytes == 0
        summary = self._session_summary
        return bool(
            summary is not None
            and summary.last_event_id == recovery.events[-2].event_id
            and summary.journal_bytes == previous_bytes)

    def _write_session_summary(self, expected_bytes: int) -> None:
        if self._journal is None or self._session_summary is None:
            return
        write_session_summary_cache(
            self._journal, self._session_summary,
            expected_bytes=expected_bytes)

    def _requires_reconciliation(self) -> bool:
        return any(turn.state == TurnState.UNKNOWN
                   or turn.state not in TERMINAL_TURN_STATES
                   for turn in self.projection.turns.values())

    def _mark_recovered_turns_unknown(self, warnings: list[str]) -> str:
        unresolved: list[str] = []
        for turn_id, turn in tuple(self.projection.turns.items()):
            if turn.state in TERMINAL_TURN_STATES:
                continue
            state = turn.state
            self.projection.reconcile_unknown(turn_id)
            if turn_id == self._turn_id:
                self._turn_state = TurnState.UNKNOWN
            unresolved.append(state)
            warnings.append(
                f"recovered non-terminal turn {turn_id} state {state} as UNKNOWN")
        if unresolved:
            self._revision += 1
        return ",".join(dict.fromkeys(unresolved))

    def _validate_presentation(self, event: RuntimeEvent) -> None:
        item_id = _event_payload_value(event, "item_id", "")
        if event.kind.startswith("message."):
            self._messages.validate(event.kind, item_id)
        self._approvals.validate(event)
        self._policy.validate(event)
        self._plan_evidence.validate(event)
        self._pipeline_checkpoint.validate(event)

    def _block_mutation(self, reason: str, *, mark_journal: bool = False) -> None:
        self._recovery_blocked = True
        self._recovery_block_reason = reason
        if mark_journal and self._journal is not None:
            try:
                self._journal.mark_read_only(reason)
            except (OSError, RuntimeEventJournalError):
                pass

    def _reset_turn_presentation(self) -> None:
        self._run_id = self._turn_id = self._actor_id = self._turn_state = ""
        self._assistant_text = self._error_text = ""
        self._assistant_text_start = self._error_text_start = 0
        self._assistant_generation = 0
        self._messages.reset()
        self._approvals.reset()
        self._policy.reset()
        self._plan_evidence.reset()
        self._pipeline_checkpoint.reset()
        self._timeline = TimelineLog(max_entries=self._entity_limit)
        self._workspace_phase = ""
        self._workspace_delta = {}
        self._observed_paths = []

    def _fold_message(self, event: RuntimeEvent) -> None:
        if event.kind not in {
                "message.started", "message.delta", "message.completed"}:
            return
        text = _payload_text(event.payload, "text", "message")
        view = self._messages.consume(
            event.kind, _event_payload_value(event, "item_id", ""), text)
        self._assistant_text = view.text
        self._assistant_text_start = view.start
        self._assistant_generation = view.generation

    def _fold_error(self, event: RuntimeEvent) -> None:
        if event.kind in {"runtime.warning", "runtime.error"}:
            text = _payload_text(event.payload, "text", "message", "reason", "raw")
            self._error_text, dropped = _append_tail_window(
                self._error_text, text, TEXT_BYTES, separator="\n")
            self._error_text_start += dropped
        elif event.kind == "turn.failed":
            reason = _payload_text(event.payload, "reason")
            if _has_line(self._error_text, reason):
                return
            self._error_text, dropped = _append_tail_window(
                self._error_text, reason, TEXT_BYTES, separator="\n")
            self._error_text_start += dropped

    def _fold_workspace(self, event: RuntimeEvent) -> None:
        if event.kind == "workspace.snapshot":
            self._workspace_snapshot(event)
        elif event.kind == "file.changed":
            for path in _event_paths(event.payload):
                if path not in self._observed_paths:
                    self._observed_paths.append(path)
            del self._observed_paths[self._entity_limit:]

    def _workspace_snapshot(self, event: RuntimeEvent) -> None:
        payload = event.payload
        phase = _clean_line(payload.get("phase", ""), 32)
        workspace = payload.get("workspace", payload)
        if isinstance(workspace, dict):
            self._workspace_meta = _workspace_meta(workspace)
        authoritative = event.extensions.get("authoritative") is True
        if authoritative:
            self._workspace_phase = phase
        if authoritative and phase == "before":
            self._workspace_delta = {}
            self._observed_paths = []
        delta = payload.get("delta", payload.get("last_turn_workspace"))
        if authoritative and isinstance(delta, dict):
            self._workspace_delta = _workspace_delta(delta, self._entity_limit)

    def _fold_timeline(self, event: RuntimeEvent) -> None:
        notice = _timeline_notice(
            event, self._entity_limit, stable_plan=self._journal is not None)
        if notice is not None:
            kind, text = notice
            self._timeline.consume(kind, text, event.occurred_at)

    def _recovery_snapshot(self) -> RecoverySnapshot:
        report = self._recovery_report
        mode = "live" if self._journal is None else (
            "recovered" if report.journal.events else "empty")
        return RecoverySnapshot(
            mode=mode, applied_events=report.applied_events,
            skipped_events=report.skipped_events,
            warning_count=len(report.warnings),
            unresolved_state=report.unresolved_state,
            degraded=report.degraded,
            read_only=report.read_only or self._recovery_blocked)

    def _snapshot_unlocked(self) -> TUIViewSnapshot:
        workspace = self._workspace_view()
        timeline = self._timeline_view()
        stats = self.projection.stats_by_run.get(self._run_id)
        completion = build_completion_snapshot(
            source="runtime events", state=self._turn_state,
            run_id=self._run_id, turn_id=self._turn_id,
            actor_id=self._actor_id, timeline=timeline.entries,
            workspace=workspace, events=tuple(self.projection.recent_events),
            usage=stats.usage if stats is not None else None)
        artifacts = build_artifact_catalog(
            source="runtime events", run_id=self._run_id,
            turn_id=self._turn_id,
            events=tuple(self.projection.recent_events))
        return TUIViewSnapshot(
            revision=self._revision, workspace_id=self._workspace_id,
            session_id=self._session_id,
            run_id=self._run_id, turn_id=self._turn_id,
            actor_id=self._actor_id, turn_state=self._turn_state,
            assistant_text=self._assistant_text, error_text=self._error_text,
            assistant_text_start=self._assistant_text_start,
            error_text_start=self._error_text_start,
            assistant_generation=self._assistant_generation,
            workspace=workspace, timeline=timeline, completion=completion,
            artifacts=artifacts, approvals=self._approvals.snapshot(),
            actors=self._actor_tree_view(), findings=self._findings_view(),
            actor_history=self._actor_history_view(), policy=self._policy.snapshot(),
            plan_evidence=self._plan_evidence.snapshot(),
            pipeline_checkpoint=self._pipeline_checkpoint.snapshot(),
            recovery=self._recovery_snapshot())

    def _findings_view(self) -> FindingsSnapshot:
        return findings_snapshot(
            self.projection.findings.values(),
            limit=min(64, self._entity_limit),
            skipped=self.projection.finding_drop_count)

    def _actor_tree_view(self) -> ActorTreeSnapshot:
        if not self._turn_id:
            return ActorTreeSnapshot()
        snapshot = build_actor_tree(
            self.projection.actors.values(), turn_id=self._turn_id,
            limit=self._entity_limit)
        dropped = self.projection.actor_drop_count
        if not dropped:
            return snapshot
        return ActorTreeSnapshot(
            snapshot.nodes, truncated=True, skipped=snapshot.skipped + dropped)

    def _actor_history_view(self) -> ActorHistorySnapshot:
        if not self._turn_id:
            return ActorHistorySnapshot()
        snapshot = build_actor_history(
            self.projection.actors.values(), current_turn_id=self._turn_id,
            limit=min(32, self._entity_limit),
            node_limit=min(64, self._entity_limit),
            scan_limit=min(8192, max(64, self._entity_limit * 4)))
        dropped = self.projection.actor_drop_count
        if not dropped:
            return snapshot
        return ActorHistorySnapshot(
            snapshot.groups, True, snapshot.skipped + dropped)

    def _workspace_view(self) -> WorkspaceView:
        meta, delta = self._workspace_meta, self._workspace_delta
        return WorkspaceView(
            phase=self._workspace_phase,
            available=bool(meta.get("available", False)),
            root=str(meta.get("root", "")), branch=str(meta.get("branch", "")),
            head=str(meta.get("head", "")), changed=bool(delta.get("changed", False)),
            dirty_files=_safe_count(delta.get("dirty_files", meta.get("changed", 0))),
            paths=tuple(delta.get("paths", ())),
            observed_paths=tuple(self._observed_paths),
            head_changed=bool(delta.get("head_changed", False)),
            paths_truncated=bool(delta.get("paths_truncated", False)))

    def _timeline_view(self) -> TimelineView:
        entries = tuple(TimelineItemView(
            item.kind, item.ts, item.summary, item.detail,
            item.exit_code, item.collapsed, item.plan)
            for item in self._timeline.entries)
        return TimelineView(entries=entries, summary=self._timeline.to_summary())

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TUI event controller is closed")


def _new_projection(recent_limit: int, entity_limit: int) -> Projection:
    return Projection(recent_limit=recent_limit, entity_limit=entity_limit)


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _late_mutable(kind: str) -> bool:
    return kind in _LATE_MUTABLE_KINDS or kind.startswith(_LATE_MUTABLE_PREFIXES)


def _append_tail_window(current: str, value: Any, maximum: int,
                        separator: str = "") -> tuple[str, int]:
    text = _clean_text(value)
    if not text:
        return current, 0
    joiner = separator if (current and not current.endswith(separator)
                           and not text.startswith(separator)) else ""
    return _tail_window(current + joiner + text, maximum)


def _tail_window(value: Any, maximum: int) -> tuple[str, int]:
    text = str(value or "")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text, 0
    suffix = encoded[-maximum:].decode("utf-8", "ignore")
    return suffix, len(text) - len(suffix)


def _has_line(current: str, value: str) -> bool:
    current_tail = current.rstrip("\n")
    value_tail = value.rstrip("\n")
    return bool(value_tail and (
        current_tail == value_tail
        or current_tail.endswith("\n" + value_tail)
    ))


def _bound_text(value: Any, maximum: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    marker = "…"
    prefix = encoded[:maximum - len(marker.encode())].decode("utf-8", "ignore")
    return prefix + marker


def _clean_text(value: Any) -> str:
    text = _ANSI_ESCAPE.sub("", str(value or ""))
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _clean_line(value: Any, maximum: int) -> str:
    return _bound_text(" ".join(_clean_text(value).split()), maximum)


def _payload_text(payload: dict[str, Any], *names: str) -> str:
    return next((_clean_text(payload[name]) for name in names if payload.get(name)), "")


def _workspace_meta(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": value.get("available") is True,
        "root": _clean_line(value.get("root", ""), 2048),
        "branch": _clean_line(value.get("branch", ""), 256),
        "head": _clean_line(value.get("head", ""), 256),
        "changed": _safe_count(value.get("changed", 0)),
    }


def _workspace_delta(value: dict[str, Any], limit: int) -> dict[str, Any]:
    paths = _bounded_paths(value.get("paths", []), limit)
    return {
        "changed": value.get("changed") is True,
        "dirty_files": _safe_count(value.get("dirty_files", 0)),
        "paths": tuple(paths), "head_changed": value.get("head_changed") is True,
        "paths_truncated": bool(value.get("paths_truncated") or
                                len(_path_values(value.get("paths", []))) > len(paths)),
    }


def _event_paths(payload: dict[str, Any]) -> list[str]:
    values = _path_values(payload.get("paths", []))
    if payload.get("path"):
        values.append(payload["path"])
    changes = payload.get("changes", [])
    if isinstance(changes, list):
        for change in changes:
            path = change.get("path") if isinstance(change, dict) else change
            if path:
                values.append(path)
    return _bounded_paths(values, len(values) or 1)


def _path_values(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _bounded_paths(values: Any, limit: int) -> list[str]:
    result: list[str] = []
    for value in _path_values(values):
        path = _clean_line(value, PATH_BYTES)
        if path and path not in result:
            result.append(path)
        if len(result) >= limit:
            break
    return result


def _safe_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _timeline_notice(event: RuntimeEvent, limit: int, *,
                     stable_plan: bool = False) -> Optional[tuple[str, str]]:
    kind, payload = event.kind, event.payload
    if kind in {"command.started", "command.completed"}:
        value = dict(payload)
        value.setdefault("item_id", _event_payload_value(event, "item_id", ""))
        value["phase"] = kind.rsplit(".", 1)[-1]
        return "command", _json_notice(value)
    if kind == "command.output":
        value = {"item_id": _event_payload_value(event, "item_id", ""),
                 "output": payload.get("aggregated_output", payload.get("output", ""))}
        return "command-output", _json_notice(value)
    if kind == "file.changed":
        paths = _event_paths(payload)[:limit]
        return "file", _json_notice({"paths": paths,
                                      "truncated": len(paths) >= limit})
    if kind == "plan.updated":
        value = dict(payload)
        value.setdefault("item_id", _event_payload_value(event, "item_id", ""))
        if stable_plan:
            _add_stable_plan_identities(event, value)
        return "plan", _json_notice(value)
    if kind == "usage.updated":
        return "usage", _usage_notice(payload)
    lifecycle = _TIMELINE_LIFECYCLES.get(kind)
    if lifecycle is not None:
        return _lifecycle_notice(event, *lifecycle)
    if kind == "turn.steered":
        text = _payload_text(payload, "text", "message")
        return "status", "Turn steered" + (f"\n{text}" if text else "")
    if kind.startswith("approval."):
        phase = kind.rsplit(".", 1)[-1]
        approval_type = _clean_line(payload.get("approval_type", "approval"), 128)
        detail = _payload_text(payload, "target", "reason", "decision")
        headline = f"Approval {phase}: {approval_type}"
        return "status", headline + (f"\n{detail}" if detail else "")
    if kind in {"message.delta", "message.completed"}:
        return "stdout", _payload_text(payload, "text", "message")
    if kind in {"runtime.warning", "runtime.error"}:
        return "stderr", _payload_text(payload, "text", "message", "reason", "raw")
    return None


def _lifecycle_notice(event: RuntimeEvent, timeline_kind: str,
                      phase: str) -> tuple[str, str]:
    value = dict(event.payload)
    if timeline_kind == "actor":
        value.setdefault("actor_id", event.actor_id)
    else:
        value.setdefault("item_id", _event_payload_value(event, "item_id", ""))
    value["phase"] = phase
    return timeline_kind, _json_notice(value)


def _add_stable_plan_identities(event: RuntimeEvent,
                                value: dict[str, Any]) -> None:
    if not value.get("plan_identity"):
        raw = _event_payload_value(event, "item_id", "")
        raw = raw or _event_payload_value(event, "plan_id", "")
        identity = stable_plan_identity(raw)
        if identity:
            value["plan_identity"] = identity
    items = value.get("items")
    if not isinstance(items, list):
        return
    copied = []
    for item in items:
        if not isinstance(item, dict):
            copied.append(item)
            continue
        item = dict(item)
        if not item.get("step_identity") and "id" in item:
            identity = stable_plan_identity(item.get("id"))
            if identity:
                item["step_identity"] = identity
        if not item.get("actor_identity") and "actor_id" in item:
            identity = stable_plan_identity(item.get("actor_id"))
            if identity:
                item["actor_identity"] = identity
        copied.append(item)
    value["items"] = copied


def _event_payload_value(event: RuntimeEvent, name: str, default: Any) -> Any:
    if name in event.payload:
        return event.payload[name]
    identities = event.extensions.get("journal_payload_identities", {})
    if isinstance(identities, dict):
        return identities.get(name, default)
    return default


def _validate_identity_scalars(value: Any, field: str = "") -> None:
    if (field in RUNTIME_IDENTITY_FIELDS and value is not None
            and not isinstance(value, (str, int, float, bool))):
        raise ValueError(f"runtime event {field} identity must be a scalar")
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_identity_scalars(item, str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_identity_scalars(item, field)


def _validate_event_identities(event: RuntimeEvent) -> None:
    canonical = RuntimeEvent.from_jsonl(event.to_jsonl())
    for value in (
            canonical.payload, canonical.extensions, canonical.unknown_fields):
        _validate_identity_scalars(value)


def _usage_notice(payload: dict[str, Any]) -> str:
    usage = payload.get("usage", payload)
    usage = usage if isinstance(usage, dict) else {}
    clean = {str(key): value for key, value in usage.items()
             if _finite_number(value) is not None}
    total = clean.get("total_tokens")
    if total is None:
        values = [_finite_number(clean.get(name, 0)) or 0.0
                  for name in ("input_tokens", "output_tokens")]
        combined = sum(values)
        total = combined if math.isfinite(combined) else None
    notice = {"usage": clean}
    if _finite_number(total) is not None:
        notice["total_tokens"] = total
    return _json_notice(notice)


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _json_notice(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return "…" if width == 1 else value[:width - 1] + "…"
