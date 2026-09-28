"""Textual bridge from trusted runtime events to immutable TUI snapshots."""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import uuid

from . import config
from .tui_controller import TUIEventController, TUIViewSnapshot
from .tui_journal import RuntimeEventJournal, RuntimeEventJournalError
from .tui_native import (
    NativePipelineAttachment, NativePipelineControlChannel)
from .tui_protocol import RuntimeEvent, TERMINAL_TURN_STATES, TurnState


_PRESENTATION_CALLBACKS = frozenset({
    "stdout", "stdout-replace", "command", "command-output", "file", "plan",
    "usage",
})
_STATUS_LABELS = {
    TurnState.QUEUED: "Queued",
    TurnState.RUNNING: "Running",
    TurnState.WAITING_APPROVAL: "Approval required",
    TurnState.INTERRUPTING: "Cancellation requested",
    TurnState.SUCCEEDED: "Ready",
    TurnState.FAILED: "Failed",
    TurnState.INTERRUPTED: "Interrupted",
    TurnState.DENIED: "Denied",
    TurnState.DISCONNECTED: "Disconnected",
    TurnState.RECONCILING: "Reconciling",
    TurnState.UNKNOWN: "Unknown",
}
_IDLE_QUARANTINE_LIMIT = 256
_IDLE_PENDING_LIMIT = 256
_IDLE_DRAIN_LIMIT = 1024


class TUIProjectionMixin:
    """Keep runtime folding off the UI thread and render snapshots on it."""

    def _init_runtime_projection(self) -> None:
        self._event_controller = TUIEventController()
        self._adapter_workspace_id = ""
        self._journal_session_id = ""
        self._journal_identity: tuple = ()
        self._runtime_view = self._event_controller.snapshot()
        self._projection_driven = False
        self._rendered_assistant_end = 0
        self._rendered_error_end = 0
        self._rendered_assistant_generation = 0
        self._runtime_finish_view: Optional[TUIViewSnapshot] = None
        self._quarantined_runtime_events: list[RuntimeEvent] = []
        self._quarantined_runtime_event_count = 0
        self._pending_idle_runtime_events: list[RuntimeEvent] = []
        self._runtime_submission: Optional[RuntimeEvent] = None
        self._runtime_submission_queue_pending = False
        self._runtime_submission_queue_notice: Optional[RuntimeEvent] = None
        self._pending_runtime_failure: Optional[RuntimeEvent] = None
        self._native_pipeline_attachment: Optional[NativePipelineAttachment] = None
        self._native_pipeline_approval: Optional[NativePipelineControlChannel] = None
        self._native_pipeline_controller: Optional[TUIEventController] = None
        self._native_pipeline_journal: Optional[RuntimeEventJournal] = None
        self._native_pipeline_previous: Optional[tuple] = None
        self._native_pipeline_generation = 0

    def attach_native_pipeline(
            self, workspace_id: str = "", *, session_id: str = ""
            ) -> NativePipelineAttachment:
        """Attach a read-only native stream, optionally recovering its journal."""
        if getattr(self, "_busy", False):
            raise RuntimeError("cannot attach a native pipeline during an active operation")
        if self._native_pipeline_attachment is not None:
            raise RuntimeError("a native pipeline is already attached")
        expected = (str(workspace_id or "") or
                    str(getattr(getattr(self, "_backend", None), "cwd", "") or
                        getattr(getattr(self, "options", None), "cwd", "") or ""))
        controller, journal = self._native_pipeline_attach_controller(
            expected, session_id)
        generation = self._native_pipeline_generation + 1
        attachment = NativePipelineAttachment(
            expected, lambda event: self._ingest_native_pipeline_event(
                controller, generation, event))
        self._native_pipeline_previous = (
            self._event_controller, self._runtime_view, self._projection_driven)
        self._native_pipeline_generation = generation
        self._native_pipeline_controller = controller
        self._native_pipeline_journal = journal
        self._native_pipeline_attachment = attachment
        self._event_controller = controller
        self._runtime_view = controller.snapshot()
        self._projection_driven = True
        self._last_status = "Native pipeline attached (read-only)"
        if getattr(self, "is_mounted", False):
            self._refresh_status()
        return attachment

    def _native_pipeline_attach_controller(
            self, workspace_id: str, session_id: str
            ) -> tuple[TUIEventController, Optional[RuntimeEventJournal]]:
        if not session_id:
            return TUIEventController(), None
        if not config.TUI_JOURNAL_ENABLED:
            raise RuntimeError(
                "explicit native session recovery requires journaling")
        if not Path(workspace_id).is_dir():
            raise RuntimeError(
                "explicit native session recovery requires a workspace directory")
        journal = RuntimeEventJournal(workspace_id, session_id)
        controller = TUIEventController(journal=journal)
        historical_workspace = controller.snapshot().workspace_id
        if historical_workspace and historical_workspace != workspace_id:
            controller.close()
            raise RuntimeError("native session workspace does not match recovery scope")
        if controller.recovery.read_only:
            detail = "; ".join(controller.recovery.warnings) or "journal is read-only"
            controller.close()
            raise RuntimeError(f"native pipeline journal is read-only: {detail}")
        return controller, journal

    def detach_native_pipeline(self) -> bool:
        """Detach observation without sending a stop or cancel request."""
        attachment = self._native_pipeline_attachment
        if attachment is None:
            return False
        attachment.close()
        self._native_pipeline_generation += 1
        controller = self._native_pipeline_controller
        previous = self._native_pipeline_previous
        if controller is not None:
            controller.close()
        self._native_pipeline_attachment = None
        self._native_pipeline_approval = None
        self._native_pipeline_controller = None
        self._native_pipeline_journal = None
        self._native_pipeline_previous = None
        if previous is not None:
            self._event_controller, self._runtime_view, self._projection_driven = previous
        if (getattr(self, "is_mounted", False)
                and not getattr(self, "_unmounting", False)):
            self._refresh_status()
            self._refresh_timeline()
        return True

    def _recover_native_session_on_start(self) -> None:
        self._initialize_native_session_view()

    def _initialize_native_session_view(self, session_id: str = "") -> None:
        session_id = str(session_id or
                          getattr(self.options, "native_session", "") or "")
        if not session_id:
            return
        if getattr(self.options, "pipeline", ""):
            raise ValueError("--native-session cannot be combined with --pipeline")
        self.attach_native_pipeline(
            getattr(self.options, "cwd", "") or os.getcwd(),
            session_id=session_id)
        self._runtime_blocked_reason = "Native session recovery is read-only"
        self._last_status = "Native pipeline recovered (read-only)"
        self.query_one("#composer").disabled = True

    def _slash_native_recover(self, args: tuple[str, ...]) -> None:
        if len(args) != 1 or not args[0]:
            self._usage("native-recover", "<session-id>")
            return
        if getattr(self, "_adapter", None) is not None:
            self.notify(
                "Restart with --native-session for provider-free recovery.",
                severity="warning")
            return
        try:
            self._initialize_native_session_view(args[0])
            self._refresh_status()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.notify(f"Native recovery refused: {exc}", severity="error")

    def _native_pipeline_recovery_status_line(self) -> str:
        if (not self._projection_driven
                or self._native_pipeline_attachment is None):
            return ""
        recovery = self._runtime_view.recovery
        if recovery.mode != "recovered":
            return ""
        controller = self._native_pipeline_controller
        cache = controller.checkpoint_status if controller else "disabled"
        warning = f" warnings={recovery.warning_count}" if recovery.warning_count else ""
        return (f"recovery {recovery.applied_events} events "
                f"checkpoint={cache}{warning}")

    def _native_pipeline_checkpoint_status_line(self) -> str:
        if (not self._projection_driven
                or self._native_pipeline_attachment is None):
            return ""
        checkpoint = self._runtime_view.pipeline_checkpoint
        if not checkpoint.stage:
            return ""
        return (f"checkpoint {checkpoint.stage} "
                f"{checkpoint.completed_stages}/{checkpoint.stage_total} "
                f"-> {checkpoint.resume_boundary}")

    def _native_pipeline_is_running(self) -> bool:
        if self._native_pipeline_attachment is None:
            return False
        return bool(self._runtime_view.turn_id and
                    self._runtime_view.turn_state not in TERMINAL_TURN_STATES)

    def _ingest_native_pipeline_event(
            self, controller: TUIEventController, generation: int,
            event: RuntimeEvent) -> None:
        if (self._native_pipeline_attachment is None
                or generation != self._native_pipeline_generation):
            raise RuntimeError("native pipeline attachment is no longer active")
        controller = self._native_pipeline_controller or controller
        controller = self._native_pipeline_durable_controller(controller, event)
        view = controller.ingest(event)
        if view is None:
            return
        if not getattr(self, "is_mounted", False):
            self._runtime_view = view
            return
        if threading.get_ident() == getattr(self, "_ui_thread_id", 0):
            self._apply_runtime_snapshot(view)
            return
        try:
            self.call_from_thread(
                self._apply_native_pipeline_snapshot, generation, view)
        except RuntimeError:
            pass

    def _native_pipeline_durable_controller(
            self, controller: TUIEventController,
            event: RuntimeEvent) -> TUIEventController:
        if self._native_pipeline_journal is not None:
            return controller
        if (not config.TUI_JOURNAL_ENABLED
                or not Path(event.workspace_id).is_dir()):
            return controller
        journal = RuntimeEventJournal(event.workspace_id, event.session_id)
        durable = TUIEventController(journal=journal)
        if durable.recovery.read_only:
            detail = "; ".join(durable.recovery.warnings) or "journal is read-only"
            durable.close()
            raise RuntimeError(f"native pipeline journal is read-only: {detail}")
        self._native_pipeline_journal = journal
        self._native_pipeline_controller = durable
        self._event_controller = durable
        self._runtime_view = durable.snapshot()
        controller.close()
        return durable

    def _apply_native_pipeline_snapshot(
            self, generation: int, view: TUIViewSnapshot) -> None:
        if (generation != self._native_pipeline_generation
                or self._native_pipeline_attachment is None):
            return
        self._apply_runtime_snapshot(view)

    def _capture_runtime_finish(self) -> None:
        """Retain a terminal worker snapshot until its UI finish callback."""
        if not self._projection_driven:
            return
        view = self._event_controller.snapshot()
        if view.turn_state in TERMINAL_TURN_STATES:
            self._runtime_finish_view = view

    def _apply_runtime_finish(self) -> None:
        view, self._runtime_finish_view = self._runtime_finish_view, None
        if (view is not None and view.revision > self._runtime_view.revision):
            self._apply_runtime_snapshot(view)

    def _enable_runtime_projection(self) -> None:
        self._projection_driven = True
        self._ensure_runtime_journal()

    def _disable_runtime_projection(self) -> None:
        self._projection_driven = False
        reset = getattr(self, "_reset_approval_controls", None)
        if callable(reset):
            reset()

    def _prepare_runtime_projection(self) -> None:
        if not self._projection_driven:
            return
        self._ensure_runtime_journal()
        self._runtime_finish_view = None
        self._runtime_view = self._event_controller.prepare_turn()
        reset = getattr(self, "_reset_approval_controls", None)
        if callable(reset):
            reset()
        self._rendered_assistant_end = 0
        self._rendered_error_end = 0
        self._rendered_assistant_generation = 0

    def _prepare_runtime_action(self, *, session_switch: bool = False,
                                review: bool = False) -> bool:
        """Keep failed journal preparation outside UI mutation and task launch."""
        try:
            if session_switch:
                self._prepare_runtime_session_switch()
            else:
                if self._runtime_blocked_reason:
                    raise RuntimeError(self._runtime_blocked_reason)
                self._prepare_runtime_projection()
                reason = (getattr(self, "_runtime_blocked_reason", "") or
                          self._event_controller.read_only_reason)
                if reason:
                    raise RuntimeError(reason)
                self._prepare_runtime_submission(review=review)
        except (OSError, OverflowError, RuntimeError, TypeError, ValueError) as exc:
            self._append_entry(
                "error", f"Unable to prepare session history: {exc}. "
                "Resolve the journal error, then retry this action or use `/new`. "
                "No task was started.")
            self._last_status = "Session history unavailable"
            self._refresh_status()
            return False
        return True

    def _prepare_runtime_submission(self, *, review: bool = False) -> None:
        """Persist local dispatch intent before a managed start RPC can block."""
        self._runtime_submission = None
        self._runtime_submission_queue_pending = False
        self._runtime_submission_queue_notice = None
        if not self._strict_adapter or not self._projection_driven:
            return
        view = self._runtime_view
        run_prefix, turn_prefix = (("review-run-", "review-turn-") if review
                                   else ("run_", "turn_"))
        event = RuntimeEvent(
            event_id=f"evt_{uuid.uuid4().hex}", seq=0,
            occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            workspace_id=self._adapter_workspace_id or view.workspace_id or str(self._backend.cwd),
            session_id=self._adapter_session_id,
            run_id=f"{run_prefix}{uuid.uuid4().hex}",
            turn_id=f"{turn_prefix}{uuid.uuid4().hex}",
            actor_id="agent_main", kind="turn.queued",
            payload={"state": TurnState.QUEUED},
            extensions={"runtime": "tui-coordinator"})
        self._ingest_runtime_event(event)
        self._runtime_submission = event
        self._runtime_submission_queue_pending = True

    def _runtime_submission_request(self) -> dict[str, str]:
        event = self._runtime_submission
        return ({key: getattr(event, key) for key in (
            "session_id", "run_id", "turn_id", "actor_id")}
                if event is not None else {})

    def _bind_runtime_submission(self, handle: Any) -> None:
        if any(getattr(handle, key, None) != value
               for key, value in self._runtime_submission_request().items()):
            raise ValueError("runtime handle does not match the submitted turn")

    def _submitted_runtime_event(
            self, event: RuntimeEvent, controller: Optional[TUIEventController] = None
            ) -> Optional[RuntimeEvent]:
        """Merge local intent with a strictly ordered, matching provider stream."""
        submitted = self._runtime_submission
        if (submitted is None
                or any(getattr(event, key) != getattr(submitted, key) for key in (
                    "workspace_id", "session_id", "run_id", "turn_id"))):
            return event
        notice = self._runtime_submission_queue_notice
        if notice is not None and event.event_id == notice.event_id:
            return None
        provider_event = event.extensions.get("runtime") != "tui-coordinator"
        delivered = event
        if event.seq > 0 and provider_event:
            # Reserve sequence 1 for local intent; retain source ordering and
            # evidence without mutating the adapter's queued object.
            delivered = replace(event, seq=event.seq + 1, extensions={
                **event.extensions, "runtime_source_seq": event.seq})
        bus = (controller or self._event_controller).bus
        if (provider_event and notice is not None and event.seq > 0
                and event.seq <= notice.seq and bus.validate_sequence(delivered)):
            raise ValueError("non-monotonic runtime seq after the queued notice")
        if (self._runtime_submission_queue_pending
                and event.kind.startswith("turn.")
                and event.actor_id == submitted.actor_id):
            self._runtime_submission_queue_pending = False
            if (event.kind == "turn.queued"
                    and self._runtime_view.turn_state == TurnState.QUEUED):
                bus.validate_sequence(delivered)
                self._runtime_submission_queue_notice = event
                return None  # one provider notice repeats the durable local intent
        return delivered

    def _ensure_runtime_journal(self) -> None:
        """Bind the active runtime session to its scoped durable controller."""
        if self._native_pipeline_attachment is not None:
            return
        if not config.TUI_JOURNAL_ENABLED:
            return
        backend = getattr(self, "_backend", None)
        cwd = str(getattr(backend, "cwd", "") or "")
        session_id = str(
            getattr(self, "_adapter_session_id", "")
            or getattr(backend, "session_id", "") or "")
        if not cwd or not session_id:
            return
        journal = RuntimeEventJournal(cwd, session_id)
        if journal.identity == self._journal_identity:
            self._reuse_runtime_journal(session_id)
            return
        self._rotate_runtime_journal(journal, session_id)

    def _reuse_runtime_journal(self, session_id: str) -> None:
        self._drain_previous_idle_events(session_id)
        reason = self._event_controller.read_only_reason
        if reason:
            self._runtime_blocked_reason = reason

    def _prepare_runtime_session_switch(self) -> None:
        """Drain or quarantine the old session before mutating its backend."""
        invalidate = getattr(self, "_invalidate_session_controls", None)
        if callable(invalidate):
            invalidate()
        if not self._projection_driven:
            return
        self._save_pending_runtime_failure()
        self._ensure_runtime_journal()
        session_id = str(getattr(self, "_adapter_session_id", "") or
                         self._journal_session_id)
        self._drain_previous_idle_events(session_id)

    def _drain_previous_idle_events(self, session_id: str) -> None:
        for pending in self._idle_runtime_batches():
            self._validate_idle_sessions(pending, {session_id} if session_id else set())
            self._route_previous_idle_events(pending, session_id)

    def _idle_runtime_batches(self):
        processed = 0
        while pending := self._collect_idle_runtime_events():
            if processed >= _IDLE_DRAIN_LIMIT:
                raise RuntimeError(
                    "Session event backlog exceeds one action's limit; "
                    "retry this action to continue saving pending events.")
            processed += len(pending)
            yield pending

    def _preflight_runtime_session(self, session_id: str) -> None:
        if not self._projection_driven or not config.TUI_JOURNAL_ENABLED:
            return
        backend = getattr(self, "_backend", None)
        cwd = str(getattr(backend, "cwd", "") or "")
        if not cwd:
            return
        controller = TUIEventController(
            journal=RuntimeEventJournal(cwd, session_id))
        try:
            if controller.recovery.read_only:
                detail = ("; ".join(controller.recovery.warnings)
                          or "reconciliation is required")
                raise RuntimeError(
                    f"TUI journal recovery is read-only: {detail}")
        finally:
            controller.close()

    def _rotate_runtime_journal(
            self, journal: RuntimeEventJournal, session_id: str) -> None:
        if (self._journal_identity and session_id == self._journal_session_id):
            raise RuntimeError(
                "one TUI session cannot cross cwd, data_dir, or memory scope")
        controller = TUIEventController(journal=journal)
        try:
            self._adopt_runtime_controller(
                controller, journal, session_id, allow_read_only=False)
        except Exception:
            controller.close()
            raise

    def _adopt_runtime_controller(
            self, controller: TUIEventController,
            journal: RuntimeEventJournal, session_id: str, *,
            allow_read_only: bool) -> None:
        if controller.recovery.read_only and not allow_read_only:
            detail = ("; ".join(controller.recovery.warnings)
                      or "reconciliation is required")
            raise RuntimeError(f"TUI journal recovery is read-only: {detail}")
        self._save_pending_runtime_failure()
        allowed = {value for value in (self._journal_session_id, session_id) if value}
        for pending in self._idle_runtime_batches():
            self._validate_idle_sessions(pending, allowed)
            self._route_previous_idle_events(pending)
            for event in pending:
                if event.session_id == session_id:
                    self._ingest_pending_idle_event(event, controller)
        previous = self._event_controller
        self._event_controller = controller
        self._journal_session_id = session_id
        self._journal_identity = journal.identity
        self._runtime_view = controller.snapshot()
        reset = getattr(self, "_reset_approval_controls", None)
        if callable(reset):
            reset()
        previous.close()
        self._pending_runtime_failure = None
        self._runtime_blocked_reason = controller.read_only_reason

    def _show_recovered_runtime(self, notice: str) -> None:
        """Replace visible transcript with the selected trusted projection."""
        view = self._event_controller.snapshot()
        self._runtime_view = view
        reset = getattr(self, "_reset_approval_controls", None)
        if callable(reset):
            reset()
        self._entries.clear()
        self._transcript_omitted = False
        self._assistant_index = self._error_index = None
        self._append_entry("system", notice, render=False)
        if view.assistant_text:
            self._append_entry("assistant", view.assistant_text, render=False)
        if view.error_text:
            self._append_entry("error", view.error_text, render=False)
        self._rendered_assistant_end = view.assistant_text_start + len(view.assistant_text)
        self._rendered_error_end = view.error_text_start + len(view.error_text)
        self._rendered_assistant_generation = view.assistant_generation
        self._timeline_summary = view.timeline.summary
        self._render_conversation()
        self._refresh_timeline()

    def _route_previous_idle_events(
            self, events: list[RuntimeEvent], session_id: str = "") -> None:
        session_id = session_id or self._journal_session_id
        pending = [event for event in events
                   if session_id and event.session_id == session_id]
        if not pending:
            return
        if self._event_controller.read_only_reason:
            self._quarantine_idle_events(pending)
            return
        for index, event in enumerate(pending):
            try:
                self._ingest_pending_idle_event(event)
            except (OverflowError, RuntimeError, ValueError):
                if not self._event_controller.read_only_reason:
                    raise
                self._quarantine_idle_events(pending[index:])
                return

    def _ingest_pending_idle_event(
            self, event: RuntimeEvent, controller: Optional[TUIEventController] = None
            ) -> None:
        try:
            if controller is None:
                self._ingest_runtime_event(event)
            else:
                delivered = self._submitted_runtime_event(event, controller)
                if delivered is not None:
                    controller.ingest(delivered)
        except (OSError, RuntimeEventJournalError) as exc:
            raise RuntimeEventJournalError(
                f"Pending session events could not be saved: {exc}. "
                "Restore storage access, then retry this action or use `/new`.",
                retryable=getattr(exc, "retryable", True),
                journal_fatal=getattr(exc, "journal_fatal", False)) from exc
        self._acknowledge_idle_events((event,))

    def _acknowledge_idle_events(self, events) -> None:
        confirmed = {event.event_id for event in events}
        self._pending_idle_runtime_events[:] = [
            event for event in self._pending_idle_runtime_events
            if event.event_id not in confirmed]

    def _quarantine_idle_events(self, events: list[RuntimeEvent]) -> None:
        self._quarantined_runtime_event_count += len(events)
        self._quarantined_runtime_events.extend(events)
        del self._quarantined_runtime_events[:-_IDLE_QUARANTINE_LIMIT]
        self._acknowledge_idle_events(events)

    @staticmethod
    def _validate_idle_sessions(
            events: list[RuntimeEvent], allowed: set[str]) -> None:
        if any(event.session_id not in allowed for event in events):
            raise RuntimeError("idle runtime event belongs to an unknown session")

    def _collect_idle_runtime_events(self) -> list[RuntimeEvent]:
        """Retain one bounded batch until every event has an explicit outcome."""
        events = self._pending_idle_runtime_events
        if events:
            return list(events)
        adapter = getattr(self, "_adapter", None)
        if adapter is None:
            return []
        try:
            stream = adapter.events()
        except (AttributeError, RuntimeError, TypeError):
            return []
        while len(events) < _IDLE_PENDING_LIMIT:
            try:
                event = stream.get(timeout=0)
            except queue.Empty:
                break
            if event is None:
                break
            if not isinstance(event, RuntimeEvent):
                raise TypeError("idle runtime stream yielded a non-RuntimeEvent")
            events.append(event)
        return list(events)

    def _abort_runtime_stream(self, adapter: Any, handle: Any,
                              error: Exception) -> Exception:
        """Request handle-scoped stop before a failed stream exposes idle UI."""
        detail = ""
        try:
            adapter.interrupt_turn({"turn_id": handle.turn_id})
        except Exception as exc:
            detail = f"; runtime interrupt failed: {exc}"
        waiter = getattr(handle, "wait", None)
        stopped = bool(waiter()) if callable(waiter) else False
        if not stopped:
            detail += "; runtime did not confirm stop"
        return RuntimeError(f"{error}{detail}") if detail else error

    @staticmethod
    def _runtime_event_context(handle: Any, event: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(event, RuntimeEvent):
            raise TypeError("runtime stream yielded a non-RuntimeEvent")
        if event.session_id != handle.session_id:
            raise RuntimeError("runtime event does not belong to the active handle")
        if not event.turn_id and event.kind.startswith("session."):
            return event.kind, event.payload
        expected = (handle.session_id, handle.run_id, handle.turn_id)
        actual = (event.session_id, event.run_id, event.turn_id)
        if actual != expected:
            raise RuntimeError("runtime event does not belong to the active handle")
        payload = event.payload if isinstance(event.payload, dict) else {}
        return event.kind, payload

    def _ingest_runtime_event(self, event: RuntimeEvent) -> None:
        """Fold a trusted envelope synchronously, then cross the UI boundary."""
        event = self._submitted_runtime_event(event)
        if event is None:
            return
        try:
            view = self._event_controller.ingest(event)
        except (OverflowError, RuntimeError, ValueError):
            reason = self._event_controller.read_only_reason
            if reason:
                self._runtime_blocked_reason = reason
            raise
        if view is None:
            return
        if threading.get_ident() == self._ui_thread_id:
            self._apply_runtime_snapshot(view)
            return
        try:
            self.call_from_thread(self._apply_runtime_snapshot, view)
        except RuntimeError:
            pass  # the application closed while a runtime was unwinding

    def _apply_runtime_snapshot(self, view: TUIViewSnapshot) -> None:
        """The only runtime-event path allowed to mutate Textual presentation."""
        if view.revision <= self._runtime_view.revision:
            return
        self._runtime_view = view
        self._apply_assistant_snapshot(view)
        self._append_projected_text("error", view.error_text, view.error_text_start)
        self._timeline_summary = view.timeline.summary
        label = _STATUS_LABELS.get(view.turn_state)
        if label:
            self._last_status = label
        self._refresh_timeline()
        refresh_inspector = getattr(self, "_refresh_plan_inspector", None)
        if callable(refresh_inspector):
            refresh_inspector()
        refresh_completion = getattr(self, "_refresh_completion_inspector", None)
        if callable(refresh_completion):
            refresh_completion()
        refresh_artifacts = getattr(self, "_refresh_artifact_inspector", None)
        if callable(refresh_artifacts):
            refresh_artifacts()
        refresh_findings = getattr(self, "_refresh_findings_inspector", None)
        if callable(refresh_findings):
            refresh_findings()
        refresh_agents = getattr(self, "_refresh_agents_inspector", None)
        if callable(refresh_agents):
            refresh_agents()
        refresh_approvals = getattr(self, "_refresh_approval_ui", None)
        if callable(refresh_approvals):
            refresh_approvals(view)
        self._refresh_status()

    def _apply_assistant_snapshot(self, view: TUIViewSnapshot) -> None:
        if view.assistant_generation != self._rendered_assistant_generation:
            text = view.assistant_text
            if view.assistant_text_start:
                text = "… earlier runtime output truncated …\n" + text
            if text:
                self._turn_stdout_seen = True
            self._replace_projected_text("assistant", text)
            self._rendered_assistant_generation = view.assistant_generation
            self._rendered_assistant_end = (
                view.assistant_text_start + len(view.assistant_text))
            return
        self._append_projected_text(
            "assistant", view.assistant_text, view.assistant_text_start)

    def _replace_projected_text(self, role: str, text: str) -> None:
        index_name = "_assistant_index" if role == "assistant" else "_error_index"
        index = getattr(self, index_name)
        if index is None:
            self._append_active(role, text)
            return
        self._entries[index].text = text
        self._trim_transcript()
        self._render_conversation()

    def _apply_stdout_callback(self, text: str, *, replace: bool = False) -> None:
        self._turn_stdout_seen = self._turn_stdout_seen or bool(text)
        if replace:
            self._replace_projected_text("assistant", text)
        else:
            self._append_active("assistant", text)

    def _append_projected_text(self, role: str, text: str, start: int) -> None:
        offset_name = ("_rendered_assistant_end" if role == "assistant"
                       else "_rendered_error_end")
        rendered_end = getattr(self, offset_name)
        window_end = start + len(text)
        if rendered_end < start:
            tail = "\n… earlier runtime output truncated …\n" + text
        elif rendered_end <= window_end:
            tail = text[max(0, rendered_end - start):]
        else:
            tail = ""
        setattr(self, offset_name, max(rendered_end, window_end))
        if not tail:
            return
        if role == "assistant":
            self._turn_stdout_seen = True
        else:
            self._turn_stderr_seen = True
        self._append_active(role, tail)

    def _projection_owns_callback(self, kind: str) -> bool:
        return self._projection_driven and kind in _PRESENTATION_CALLBACKS

    def _active_timeline(self) -> Any:
        if self._projection_driven:
            return self._runtime_view.timeline
        return self._timeline

    def _active_timeline_summary(self) -> str:
        timeline = self._active_timeline()
        summary = getattr(timeline, "summary", None)
        return str(summary if summary is not None else timeline.to_summary())

    def _projection_status_state(self, fallback: str) -> str:
        state = self._runtime_view.turn_state if self._projection_driven else ""
        return state.lower() if state else fallback

    def _projected_terminal_label(self) -> str:
        state = self._runtime_view.turn_state if self._projection_driven else ""
        return _STATUS_LABELS.get(state, "") if state in TERMINAL_TURN_STATES else ""

    def _reconcile_runtime_failure(self, reason: str) -> bool:
        """Close a projected turn when its trusted stream fails before terminal."""
        view = self._runtime_view
        if (not self._projection_driven or not view.turn_id
                or view.turn_state in TERMINAL_TURN_STATES):
            return False
        unknown_allowed = view.turn_state in {
            TurnState.QUEUED, TurnState.RUNNING, TurnState.INTERRUPTING, TurnState.WAITING_APPROVAL,
            TurnState.RECONCILING,
        }
        payload = {"reason": str(reason or "runtime event stream failed")[:2048]}
        if unknown_allowed:
            payload["state"] = TurnState.UNKNOWN
        event = self._coordinator_event("turn.failed", payload)
        try:
            self._ingest_runtime_event(event)
        except (OSError, OverflowError, RuntimeError, ValueError) as exc:
            self._pending_runtime_failure = event
            self._runtime_blocked_reason = (
                f"Unable to confirm the saved failure state: {exc}. "
                "The turn outcome remains unconfirmed. "
                "Restore storage access, then use `/new` before another task.")
            self._append_entry("error", self._runtime_blocked_reason)
            return False
        self._pending_runtime_failure = None
        return self._runtime_view.turn_state in TERMINAL_TURN_STATES

    def _save_pending_runtime_failure(self) -> None:
        """Save an interrupted observation before draining late provider events.

        A failed append can leave QUEUED in the journal after the consumer has
        already discarded turn.started. Preserve UNKNOWN before accepting any
        later events; they cannot supply the missing observation retroactively.
        Read-only journals retain their trusted prefix and quarantine late events.
        """
        event = self._pending_runtime_failure
        if event is None or self._event_controller.read_only_reason:
            return
        self._ingest_runtime_event(event)
        self._pending_runtime_failure = None

    def _coordinator_event(self, kind: str, payload: dict[str, Any]) -> RuntimeEvent:
        view = self._runtime_view
        return RuntimeEvent(
            event_id=f"evt_{uuid.uuid4().hex}", seq=0,
            occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            workspace_id=view.workspace_id, session_id=view.session_id,
            run_id=view.run_id, turn_id=view.turn_id,
            actor_id=view.actor_id or "agent_main", kind=kind, payload=payload,
            extensions={"runtime": "tui-coordinator"})

    def _projected_workspace_delta(self) -> Optional[dict[str, Any]]:
        workspace = self._runtime_view.workspace
        if not self._projection_driven or workspace.phase != "after":
            return None
        return {
            "changed": workspace.changed, "dirty_files": workspace.dirty_files,
            "paths": list(workspace.paths), "head_changed": workspace.head_changed,
            "paths_truncated": workspace.paths_truncated,
        }

    def _projection_clear_visible(self) -> None:
        if not self._projection_driven:
            return
        self._rendered_assistant_end = (
            self._runtime_view.assistant_text_start
            + len(self._runtime_view.assistant_text))
        self._rendered_assistant_generation = self._runtime_view.assistant_generation
        self._rendered_error_end = (
            self._runtime_view.error_text_start + len(self._runtime_view.error_text))

    def on_unmount(self) -> None:
        adapter = getattr(self, "_adapter", None)
        try:
            if adapter is not None:
                adapter.close()
            setter = getattr(getattr(self, "_backend", None),
                             "set_runtime_event_sink", None)
            if callable(setter):
                setter(None)
        except (AttributeError, RuntimeError, TypeError):
            pass
        finally:
            self._event_controller.close()
