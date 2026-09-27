"""Scoped session discovery and recovery commands for the product TUI."""

from __future__ import annotations

import os
import re
import stat
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .tui_controller import TUIEventController
from .tui_journal import (
    JOURNAL_SCHEMA_VERSION,
    JournalRecovery,
    RuntimeEventJournal,
    RuntimeEventJournalError,
    _read_json,
)
from .tui_runtime import (
    ArchiveSessionRequest, ListSessionsRequest, RuntimeSessionPage,
    RuntimeSessionSummary, SessionReconciliationError,
)
from .tui_session_controls import session_compaction_active
from .tui_model_controls import runtime_setting_change_active
from .tui_session_summary import TUISessionSummary, read_session_summary
from .tui_view_support import inline_label


SESSION_INDEX_LIMIT = 50
SESSION_SCAN_LIMIT = 4_096
_SESSION_EVENT_KINDS = frozenset({
    "session.created", "session.resumed", "session.forked",
})


@dataclass(frozen=True)
class TUISessionRecord:
    """One validated journal directory in the current memory scope."""

    session_id: str
    updated_ns: int
    journal_bytes: int
    read_only: bool
    archived: bool = False
    summary: Optional[TUISessionSummary] = None
    summary_status: str = "missing"
    provider: Optional[RuntimeSessionSummary] = None


@dataclass(frozen=True)
class TUISessionIndex:
    """Bounded discovery result; warnings never become executable input."""

    sessions: tuple[TUISessionRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    provider_truncated: bool = False
    provider_warnings: tuple[str, ...] = ()
    provider_observed_at: str = ""


@dataclass(frozen=True)
class _SessionIndexBoundary:
    """Immutable worker token for storage, runtime and adapter identity."""

    cwd: str
    data_root: str
    scope: tuple[tuple[str, str], ...]
    runtime: str
    session_list_supported: bool
    adapter_identity: int

    @property
    def storage(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return self.cwd, self.data_root, self.scope


@dataclass(frozen=True)
class TUIResumeTarget:
    """Provider handle recovered from a trusted session event."""

    provider_session_id: str
    runtime: str


def discover_tui_sessions(
        cwd: str, *, limit: int = SESSION_INDEX_LIMIT,
        scan_limit: int = SESSION_SCAN_LIMIT) -> TUISessionIndex:
    """List recent journals inside exactly one cwd/memory scope."""
    _positive_limit(limit, "limit")
    _positive_limit(scan_limit, "scan_limit")
    root = _session_root(cwd)
    if not root.exists():
        return TUISessionIndex()
    if root.is_symlink() or not root.is_dir():
        return TUISessionIndex(warnings=("session index root is not a safe directory",))
    records: list[TUISessionRecord] = []
    warnings: list[str] = []
    truncated = False
    try:
        iterator = os.scandir(root)
    except OSError as exc:
        return TUISessionIndex(warnings=(f"cannot scan session index: {exc}",))
    with iterator:
        for position, entry in enumerate(iterator, 1):
            if position > scan_limit:
                truncated = True
                break
            record, warning = _inspect_entry(cwd, Path(entry.path), entry)
            if record is not None:
                records.append(record)
            elif warning:
                warnings.append(warning)
    records.sort(key=lambda item: (-item.updated_ns, item.session_id))
    if len(records) > limit:
        records = records[:limit]
        truncated = True
    records, summary_warnings = _load_index_summaries(cwd, records)
    warnings.extend(summary_warnings)
    return TUISessionIndex(
        sessions=tuple(records), warnings=tuple(warnings),
        truncated=truncated)


def merge_provider_session_page(
        index: TUISessionIndex, page: RuntimeSessionPage, *, runtime: str
        ) -> TUISessionIndex:
    """Join advisory provider observations onto exact local summary handles."""
    if not isinstance(index, TUISessionIndex):
        raise TypeError("index must be a TUISessionIndex")
    if not isinstance(page, RuntimeSessionPage):
        raise TypeError("page must be a RuntimeSessionPage")
    runtime_name = str(runtime or "")
    by_id = {item.provider_session_id: item for item in page.sessions}
    records = []
    for record in index.sessions:
        summary = record.summary
        provider = None
        if (summary is not None and summary.runtime == runtime_name
                and summary.provider_session_id):
            provider = by_id.get(summary.provider_session_id)
        records.append(replace(record, provider=provider))
    observed = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    return replace(
        index, sessions=tuple(records),
        provider_truncated=page.next_cursor is not None,
        provider_warnings=page.warnings,
        provider_observed_at=observed,
    )


def open_tui_session(
        cwd: str, session_id: str, *,
        allow_archived: bool = False) -> RuntimeEventJournal:
    """Resolve an exact existing session without creating an empty journal."""
    journal = RuntimeEventJournal(cwd, session_id)
    directory = journal.directory
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeEventJournalError("TUI session was not found in this scope")
    actual = _validated_session_id(cwd, directory)
    if actual != journal.session_id:
        raise RuntimeEventJournalError("journal metadata session mismatch")
    if journal.archived and not allow_archived:
        raise RuntimeEventJournalError(
            "TUI session is archived; restore it before opening")
    return journal


def archive_tui_session(cwd: str, session_id: str) -> bool:
    """Archive one exact idle journal without mutating its event history."""
    journal = open_tui_session(cwd, session_id, allow_archived=True)
    if journal.archived:
        return False
    controller = TUIEventController(journal=journal)
    try:
        unresolved = controller.recovery.unresolved_state
        expected_bytes = controller.recovery.journal.total_bytes
        if unresolved:
            raise RuntimeEventJournalError(
                f"cannot archive a session with unresolved turn state: {unresolved}")
    finally:
        controller.close()
    return journal.mark_archived(expected_bytes=expected_bytes)


def restore_tui_session(cwd: str, session_id: str) -> bool:
    """Restore one exact local archive marker; provider state is unchanged."""
    journal = open_tui_session(cwd, session_id, allow_archived=True)
    return journal.restore_archived()


def recover_resume_target(recovery: JournalRecovery) -> Optional[TUIResumeTarget]:
    """Read the latest non-redacted provider handle from a trusted prefix."""
    for event in reversed(recovery.events):
        if event.kind not in _SESSION_EVENT_KINDS:
            continue
        provider_id = event.payload.get("provider_session_id")
        runtime = event.extensions.get("runtime", "")
        if not _usable_provider_id(provider_id) or not isinstance(runtime, str):
            return None
        return TUIResumeTarget(str(provider_id), runtime.strip())
    return None


def render_session_index(index: TUISessionIndex, current_session: str = "") -> str:
    """Render a compact picker contract usable over small SSH terminals."""
    lines = ["## Sessions", ""]
    if not index.sessions:
        lines.append("No recoverable TUI sessions exist in this workspace scope.")
    for item in index.sessions:
        states = ["current"] if item.session_id == current_session else []
        if item.archived:
            states.append("archived")
        else:
            states.append("read-only" if item.read_only else "candidate")
        marker = ", ".join(states)
        lines.append(
            f"- {_inline_code(item.session_id)} — {_format_time(item.updated_ns)}; "
            f"{_format_bytes(item.journal_bytes)}; {marker}")
    if index.truncated:
        lines.extend(["", "_The bounded session index was truncated._"])
    if index.warnings:
        lines.extend(["", f"_{len(index.warnings)} local index warning(s)._"])
    if index.provider_warnings:
        lines.extend(["", f"_{len(index.provider_warnings)} provider item warning(s)._"])
    if index.provider_truncated:
        lines.extend(["", "_The provider page has additional results._"])
    lines.extend(["", "Open one with `/resume <session-id>`."])
    return "\n".join(lines)


class TUISessionMixin:
    """Expose scoped history and capability-driven execution resume."""

    def _slash_sessions(self, args: tuple[str, ...] = ()) -> None:
        if not self._require_no_args("sessions", args):
            return
        self._open_session_picker()

    def _open_session_picker(self) -> None:
        generation = int(getattr(self, "_session_index_generation", 0)) + 1
        self._session_index_generation = generation
        active_adapter = getattr(self, "_adapter", None)
        try:
            runtime, supported = self._runtime_capability("session_list")
            boundary = _session_index_boundary(
                self._session_cwd(), runtime, supported, active_adapter)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.notify(f"Session index failed: {exc}", severity="error")
            self._last_status = "Session index failed"
            self._refresh_status()
            return
        provider_adapter = active_adapter if supported else None
        self._last_status = "Loading sessions…"
        self._refresh_status()
        self.run_worker(
            lambda: self._load_session_index_worker(
                boundary, generation, provider_adapter, active_adapter),
            name="session-index", group="session-index",
            exit_on_error=False, exclusive=True, thread=True)

    def _load_session_index_worker(
            self, boundary: _SessionIndexBoundary, generation: int,
            provider_adapter: Any, active_adapter: Any) -> None:
        index: Optional[TUISessionIndex] = None
        error: Optional[Exception] = None
        try:
            _require_storage_boundary(boundary)
            index = discover_tui_sessions(boundary.cwd)
            _require_storage_boundary(boundary)
            if provider_adapter is not None:
                try:
                    page = provider_adapter.list_sessions(
                        ListSessionsRequest(limit=100))
                    index = merge_provider_session_page(
                        index, page, runtime=boundary.runtime)
                except (OSError, RuntimeError, TypeError, ValueError):
                    index = replace(
                        index,
                        provider_warnings=(
                            "provider session summary is unavailable",),
                    )
            _require_storage_boundary(boundary)
        except (OSError, RuntimeEventJournalError, ValueError) as exc:
            error = exc
        try:
            self.call_from_thread(
                self._finish_session_index, boundary, generation,
                active_adapter, index, error)
        except RuntimeError:
            pass

    def _finish_session_index(
            self, boundary: _SessionIndexBoundary, generation: int,
            active_adapter: Any,
            index: Optional[TUISessionIndex], error: Optional[Exception]) -> None:
        if generation != getattr(self, "_session_index_generation", 0):
            return
        current_adapter = getattr(self, "_adapter", None)
        try:
            runtime, supported = self._runtime_capability("session_list")
            current = _session_index_boundary(
                self._session_cwd(), runtime, supported, current_adapter)
        except (OSError, RuntimeError, TypeError, ValueError):
            return
        if (boundary != current or active_adapter is not current_adapter):
            return
        if error is not None or index is None:
            self.notify(f"Session index failed: {error}", severity="error")
            self._last_status = "Session index failed"
            self._refresh_status()
            return
        from .tui_session_picker import TUISessionPickerScreen

        screen = TUISessionPickerScreen(index, self._current_session_id())
        self.push_screen(screen, self._handle_session_picker_action)
        self._last_status = f"Sessions refreshed ({len(index.sessions)})"
        self._refresh_status()

    def _handle_session_picker_action(self, action: Any) -> None:
        if action is None:
            return
        operation = getattr(action, "operation", "")
        session_id = str(getattr(action, "session_id", "") or "")
        if operation == "resume":
            self._resume_tui_session(session_id)
        elif operation == "native-recover":
            self._slash_native_recover((session_id,))
        elif operation == "fork":
            self._fork_tui_session(session_id)
        elif operation == "archive":
            self._set_tui_session_archived(session_id, archived=True)
            self.call_later(self._open_session_picker)
        elif operation == "restore":
            self._set_tui_session_archived(session_id, archived=False)
            self.call_later(self._open_session_picker)
        elif operation == "new":
            self._new_session()
        elif operation == "refresh":
            self.call_later(self._open_session_picker)

    def _slash_resume(self, args: tuple[str, ...]) -> None:
        if len(args) != 1:
            self._usage("resume", "<session-id>")
            return
        if (self._busy or session_compaction_active(self)
                or runtime_setting_change_active(self)):
            self.notify("Cancel the active operation before restoring a session.",
                        severity="warning")
            return
        self._resume_tui_session(args[0])

    def _slash_fork(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._usage("fork", "[session-id]")
            return
        session_id = args[0] if args else self._current_session_id()
        if not session_id:
            self._append_entry("error", "No current session is available to fork.")
            return
        if (self._busy or session_compaction_active(self)
                or runtime_setting_change_active(self)):
            self.notify("Cancel the active operation before forking a session.",
                        severity="warning")
            return
        self._fork_tui_session(session_id)

    def _slash_archive(self, args: tuple[str, ...]) -> None:
        if len(args) != 1:
            self._usage("archive", "<session-id>")
            return
        self._set_tui_session_archived(args[0], archived=True)

    def _slash_unarchive(self, args: tuple[str, ...]) -> None:
        if len(args) != 1:
            self._usage("unarchive", "<session-id>")
            return
        self._set_tui_session_archived(args[0], archived=False)

    def _slash_provider_archive(self, args: tuple[str, ...]) -> None:
        if len(args) != 1:
            self._usage("provider-archive", "<session-id>")
            return
        self._set_provider_session_archived(args[0], archived=True)

    def _slash_provider_unarchive(self, args: tuple[str, ...]) -> None:
        if len(args) != 1:
            self._usage("provider-unarchive", "<session-id>")
            return
        self._set_provider_session_archived(args[0], archived=False)

    def _resume_tui_session(self, session_id: str) -> None:
        if (self._busy or session_compaction_active(self)
                or runtime_setting_change_active(self)):
            self.notify("Wait for the active operation before restoring a session.",
                        severity="warning")
            return
        if session_id == self._current_session_id():
            self._append_entry("system", f"Session {_inline_code(session_id)} is already active.")
            return
        controller: Optional[TUIEventController] = None
        execution_bound = False
        try:
            journal = open_tui_session(self._session_cwd(), session_id)
            controller = TUIEventController(journal=journal)
            target = recover_resume_target(controller.recovery.journal)
            mode = self._resume_execution_mode(target, controller)
            self._prepare_runtime_session_switch()
            if mode == "executable":
                try:
                    self._bind_resumed_backend(session_id, target)
                except SessionReconciliationError as exc:
                    allowed = {"active", "systemError", "unavailable", "unverified"}
                    status = exc.status if exc.status in allowed else "unverified"
                    mode = f"provider-{status}"
                else:
                    execution_bound = True
            self._adopt_runtime_controller(
                controller, journal, session_id, allow_read_only=True)
            controller = None
            self._reset_follow_ups_for_session_switch()
            self._present_resumed_session(session_id, mode)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if execution_bound:
                self._runtime_blocked_reason = (
                    "Provider session changed but TUI recovery did not complete; "
                    "start `/new` before running another turn.")
            self._append_entry("error", f"Unable to restore session: {exc}")
        finally:
            if controller is not None:
                controller.close()

    def _fork_tui_session(self, source_session_id: str) -> None:
        if (self._busy or session_compaction_active(self)
                or runtime_setting_change_active(self)):
            self.notify("Wait for the active operation before forking a session.",
                        severity="warning")
            return
        source: Optional[TUIEventController] = None
        child: Optional[TUIEventController] = None
        execution_bound = False
        try:
            source_journal = open_tui_session(
                self._session_cwd(), source_session_id)
            source = TUIEventController(journal=source_journal)
            target = self._fork_execution_target(source)
            source.close()
            source = None
            self._prepare_runtime_session_switch()
            requested_id = f"ses_{uuid.uuid4().hex}"
            if self._adapter is None:
                self._preflight_runtime_session(requested_id)
            child_id = self._bind_forked_backend(requested_id, target)
            execution_bound = True
            if self._adapter is not None:
                self._preflight_runtime_session(child_id)
            journal = RuntimeEventJournal(self._session_cwd(), child_id)
            child = TUIEventController(journal=journal)
            self._adopt_runtime_controller(
                child, journal, child_id, allow_read_only=False)
            child = None
            self._reset_follow_ups_for_session_switch()
            self._present_forked_session(source_session_id, child_id)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if execution_bound:
                self._runtime_blocked_reason = (
                    "Provider session changed but TUI fork did not complete; "
                    "start `/new` before running another turn.")
            self._append_entry("error", f"Unable to fork session: {exc}")
        finally:
            if source is not None:
                source.close()
            if child is not None:
                child.close()

    def _fork_execution_target(
            self, controller: TUIEventController) -> TUIResumeTarget:
        target = recover_resume_target(controller.recovery.journal)
        runtime, supported = self._runtime_capability("session_fork")
        if controller.recovery.read_only:
            raise RuntimeError("source session history is read-only")
        if target is None:
            raise RuntimeError("no trusted provider fork handle is available")
        if not supported or not runtime:
            raise RuntimeError("the active runtime does not support session fork")
        if target.runtime != runtime:
            raise RuntimeError("the historical and active runtimes do not match")
        return target

    def _bind_forked_backend(
            self, session_id: str, target: TUIResumeTarget) -> str:
        adapter = getattr(self, "_adapter", None)
        request = {
            "provider_session_id": target.provider_session_id,
            "session_id": session_id,
            "workspace_id": self._session_cwd(),
        }
        if adapter is not None:
            session = adapter.fork_session(request)
            child_id = str(session.session_id)
            if not child_id:
                raise RuntimeError("provider fork returned no local session id")
            self._adapter_session_id = child_id
            self._adapter_workspace_id = str(session.workspace_id)
            return child_id
        fork = getattr(self._backend, "fork_session", None)
        if not callable(fork):
            raise RuntimeError("backend does not implement session fork")
        fork(session_id, target.provider_session_id)
        return session_id

    def _present_forked_session(self, source_id: str, child_id: str) -> None:
        self._show_recovered_runtime(
            f"Forked {_inline_code(child_id)} from {_inline_code(source_id)}. "
            "Provider context will fork on the next turn.")
        self._last_task_prompt = ""
        self._last_status = "Session fork prepared"
        self._refresh_status()

    def _set_tui_session_archived(
            self, session_id: str, *, archived: bool) -> bool:
        if self._busy:
            self.notify("Cancel the active operation before changing session storage.",
                        severity="warning")
            return False
        if archived and session_id == self._current_session_id():
            self._append_entry("error", "The active session cannot be archived.")
            return False
        try:
            changed = (archive_tui_session(self._session_cwd(), session_id)
                       if archived else
                       restore_tui_session(self._session_cwd(), session_id))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            verb = "archive" if archived else "restore"
            self._append_entry("error", f"Unable to {verb} session: {exc}")
            return False
        state = "archived" if archived else "restored"
        qualifier = "" if changed else " already"
        self.notify(f"Session {session_id}{qualifier} {state}.")
        self._last_status = f"Session {state}"
        self._refresh_status()
        return True

    def _set_provider_session_archived(
            self, session_id: str, *, archived: bool) -> bool:
        """Change remote thread state explicitly, never as a local fallback."""
        if self._busy:
            self.notify("Cancel the active operation before changing provider state.",
                        severity="warning")
            return False
        if archived and session_id == self._current_session_id():
            self._append_entry("error", "The active provider session cannot be archived.")
            return False
        try:
            method, request = _provider_archive_operation(self, session_id, archived)
            method(request)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            verb = "archive" if archived else "restore"
            self._append_entry("error", f"Unable to provider-{verb} session: {exc}")
            return False
        verb = "archived" if archived else "restored"
        self.notify(f"Provider session {session_id} {verb}.")
        self._last_status = f"Provider session {verb}"
        self._refresh_status()
        return True

    def _resume_execution_mode(
            self, target: Optional[TUIResumeTarget],
            controller: TUIEventController) -> str:
        runtime, supported = self._runtime_resume_capability()
        if controller.recovery.read_only:
            return "journal-read-only"
        if target is None:
            return "missing-handle"
        if not supported or not runtime:
            return "unsupported"
        if target.runtime != runtime:
            return "runtime-mismatch"
        return "executable"

    def _runtime_resume_capability(self) -> tuple[str, bool]:
        return self._runtime_capability("session_resume")

    def _runtime_capability(self, name: str) -> tuple[str, bool]:
        if getattr(self, "_structured_backend", False):
            from .tui_codex_support import _runtime_info
            info = _runtime_info()
        else:
            adapter = getattr(self, "_adapter", None)
            if adapter is None:
                return "", False
            runtime_info = getattr(adapter, "runtime_info", None)
            if not callable(runtime_info):
                return "", False
            info = runtime_info()
        capabilities = getattr(info, "capabilities", None)
        supports = getattr(capabilities, "supports", None)
        enabled = bool(supports(name)) if callable(supports) else False
        return str(getattr(info, "runtime", "") or ""), enabled

    def _bind_resumed_backend(
            self, session_id: str, target: Optional[TUIResumeTarget]) -> None:
        if target is None:
            raise RuntimeError("provider resume handle is unavailable")
        adapter = getattr(self, "_adapter", None)
        if adapter is not None:
            session = adapter.resume_session({
                "provider_session_id": target.provider_session_id,
                "session_id": session_id,
                "workspace_id": self._session_cwd(),
            })
            self._adapter_session_id = str(session.session_id)
            self._adapter_workspace_id = str(session.workspace_id)
            return
        resume = getattr(self._backend, "resume_session", None)
        if not callable(resume):
            raise RuntimeError("backend does not implement session resume")
        resume(session_id, target.provider_session_id)

    def _present_resumed_session(self, session_id: str, mode: str) -> None:
        executable = mode == "executable"
        if not executable:
            reasons = {
                "journal-read-only": "the journal requires reconciliation",
                "missing-handle": "no trusted provider resume handle is available",
                "unsupported": "the active runtime does not support session resume",
                "runtime-mismatch": "the historical and active runtimes do not match",
                "provider-active": "the provider reports that the thread is still active",
                "provider-systemError": "the provider thread is in systemError state",
                "provider-unavailable": "the provider thread status could not be read",
                "provider-unverified": "the provider returned an invalid thread summary",
            }
            self._runtime_blocked_reason = (
                f"Recovered history is read-only: {reasons.get(mode, 'resume is unavailable')}. "
                "Start `/new` to execute.")
        notice = ("Provider context will resume on the next turn."
                  if executable else self._runtime_blocked_reason)
        self._show_recovered_runtime(
            f"Restored session {_inline_code(session_id)}. {notice}")
        self._last_task_prompt = ""
        self._last_status = "Session resumed" if executable else "Session history (read-only)"
        self._refresh_status()

    def _session_cwd(self) -> str:
        return str(getattr(self._backend, "cwd", "") or self.options.cwd or os.getcwd())

    def _current_session_id(self) -> str:
        return str(getattr(self, "_journal_session_id", "") or
                   getattr(self, "_adapter_session_id", "") or
                   getattr(self._backend, "session_id", "") or
                   "")

    def _palette_sessions(self) -> None:
        self._slash_sessions(())


def _provider_archive_operation(
        owner: Any, session_id: str, archived: bool):
    journal = open_tui_session(owner._session_cwd(), session_id,
                               allow_archived=True)
    controller = TUIEventController(journal=journal)
    try:
        recovery = controller.recovery
        if recovery.read_only:
            raise RuntimeError("local session history is read-only")
        if getattr(recovery, "unresolved_state", ""):
            raise RuntimeError("local session has unresolved turn state")
        target = recover_resume_target(recovery.journal)
        if target is None:
            raise RuntimeError("no trusted provider session handle is available")
        runtime, supported = owner._runtime_capability("session_archive")
        if not supported or not runtime:
            raise RuntimeError("the active runtime does not support provider archive")
        if target.runtime != runtime:
            raise RuntimeError("the historical and active runtimes do not match")
        adapter = getattr(owner, "_adapter", None)
        method = getattr(
            adapter, "archive_session" if archived else "unarchive_session", None)
        if not callable(method):
            raise RuntimeError("the active runtime has no provider archive operation")
        runtime_session = getattr(adapter, "session", None)
        workspace_id = str(
            getattr(runtime_session, "workspace_id", "") or owner._session_cwd())
        return method, ArchiveSessionRequest(
            provider_session_id=target.provider_session_id,
            session_id=session_id, workspace_id=workspace_id)
    finally:
        controller.close()


def _session_root(cwd: str) -> Path:
    return RuntimeEventJournal(cwd, "session-index-probe").directory.parent


def _session_index_boundary(
        cwd: str, runtime: str, supported: bool,
        adapter: Any) -> _SessionIndexBoundary:
    journal = RuntimeEventJournal(cwd, "session-index-probe")
    return _SessionIndexBoundary(
        cwd=journal.cwd,
        data_root=str(journal.data_root),
        scope=tuple(sorted(
            (str(key), str(value)) for key, value in journal.scope.items())),
        runtime=str(runtime or ""),
        session_list_supported=bool(supported),
        adapter_identity=id(adapter) if adapter is not None else 0,
    )


def _require_storage_boundary(boundary: _SessionIndexBoundary) -> None:
    current = _session_index_boundary(
        boundary.cwd, boundary.runtime,
        boundary.session_list_supported, None)
    if current.storage != boundary.storage:
        raise ValueError("session index storage scope changed during discovery")


def _load_index_summaries(
        cwd: str, records: list[TUISessionRecord]
        ) -> tuple[list[TUISessionRecord], list[str]]:
    """Hydrate only the already-bounded top records; never replay journals."""
    hydrated: list[TUISessionRecord] = []
    warnings: list[str] = []
    for record in records:
        journal = RuntimeEventJournal(cwd, record.session_id)
        summary, status = read_session_summary(journal, record.journal_bytes)
        if summary is None:
            hydrated.append(replace(record, summary_status=status))
            if status == "invalid":
                warnings.append(
                    f"invalid session summary cache: {record.session_id}")
            continue
        hydrated.append(replace(
            record,
            read_only=bool(
                record.read_only or summary.read_only
                or summary.requires_reconciliation),
            summary=summary,
            summary_status="available",
        ))
    return hydrated, warnings


def _inspect_entry(cwd: str, path: Path, entry: Any
                   ) -> tuple[Optional[TUISessionRecord], str]:
    try:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            return None, f"unsafe session entry: {entry.name}"
        session_id = _validated_session_id(cwd, path)
        updated_ns, journal_bytes = _journal_stat(path)
        read_only = _regular_marker(path / "read-only.json", "read-only")
        archived = RuntimeEventJournal(cwd, session_id).archived
        return TUISessionRecord(
            session_id, updated_ns, journal_bytes, read_only, archived), ""
    except (OSError, RuntimeEventJournalError, ValueError) as exc:
        return None, f"invalid session entry {entry.name}: {exc}"


def _validated_session_id(cwd: str, directory: Path) -> str:
    metadata = _read_json(directory / "meta.json")
    session_id = metadata.get("session_id")
    if not isinstance(session_id, str):
        raise RuntimeEventJournalError("journal metadata has no session_id")
    journal = RuntimeEventJournal(cwd, session_id)
    expected = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "scope": journal.scope,
        "session_id": journal.session_id,
        "cwd_id": _cwd_digest(journal),
    }
    if directory != journal.directory or metadata != expected:
        raise RuntimeEventJournalError("journal metadata boundary mismatch")
    return journal.session_id


def _cwd_digest(journal: RuntimeEventJournal) -> str:
    from .tui_journal import _digest
    return _digest(journal.cwd)


def _journal_stat(directory: Path) -> tuple[int, int]:
    journal_path = directory / "events.jsonl"
    path = journal_path if journal_path.exists() or journal_path.is_symlink() else (
        directory / "meta.json")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeEventJournalError("journal data is not a regular file")
    return info.st_mtime_ns, info.st_size if path == journal_path else 0


def _regular_marker(path: Path, label: str) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeEventJournalError(f"journal {label} marker is unsafe")
    return True


def _usable_provider_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if any(marker in value for marker in ("[REDACTED]", "...[truncated]")):
        return False
    return "\n" not in value and "\r" not in value and len(value.encode()) <= 2_048


def _positive_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _format_time(updated_ns: int) -> str:
    try:
        timestamp = datetime.fromtimestamp(updated_ns / 1_000_000_000, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return "unknown time"
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _inline_code(value: str) -> str:
    label = inline_label(value)
    longest = max((len(run) for run in re.findall(r"`+", label)), default=0)
    fence = "`" * max(1, longest + 1)
    padding = " " if label.startswith("`") or label.endswith("`") else ""
    return f"{fence}{padding}{label}{padding}{fence}"
