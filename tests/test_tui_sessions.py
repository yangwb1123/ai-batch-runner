"""Scoped session index and capability-driven TUI resume contracts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading

import pytest
from textual.widgets import Input, Markdown, OptionList

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_backend import TUIBackend
from pbatch.tui_backend_structured import (
    _adopt_provider_session,
    _store_provider_thread,
)
from pbatch.tui_appserver_support import EventQueue, RuntimeEventStream
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal, RuntimeEventJournalError
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent
from pbatch.tui_runtime import (
    RuntimeInfo, RuntimeSession, RuntimeSessionPage, RuntimeSessionSummary,
    SessionReconciliationError,
)
from pbatch.tui_sessions import (
    TUISessionMixin,
    archive_tui_session,
    discover_tui_sessions,
    merge_provider_session_page,
    open_tui_session,
    recover_resume_target,
    restore_tui_session,
)
from pbatch.tui_session_picker import TUISessionPickerScreen
from pbatch.tui_shell import SLASH_HELP


def _event(session: str, seq: int, kind: str, *, payload=None,
           runtime: str = "codex-exec") -> RuntimeEvent:
    turn_id = "" if kind.startswith("session.") else "turn_one"
    return RuntimeEvent(
        event_id=f"evt_{session}_{seq}_{kind.replace('.', '_')}",
        seq=seq, occurred_at=f"2026-08-25T12:00:{seq:02d}Z",
        workspace_id="ws_one", session_id=session, run_id="run_one",
        turn_id=turn_id, actor_id="agent_main", kind=kind,
        payload=payload or {}, extensions={"runtime": runtime})


def _completed_session(cwd: Path, session: str, provider: str = "thread-one",
                       runtime: str = "codex-exec") -> RuntimeEventJournal:
    journal = RuntimeEventJournal(str(cwd), session)
    controller = TUIEventController(journal=journal)
    events = [
        _event(session, 1, "session.created", payload={
            "provider_session_id": provider, "state": "ACTIVE"}, runtime=runtime),
        _event(session, 2, "turn.started", runtime=runtime),
        _event(session, 3, "message.completed", payload={
            "item_id": "message-one", "text": f"answer from {session}"},
               runtime=runtime),
        _event(session, 4, "turn.completed", runtime=runtime),
    ]
    for event in events:
        controller.ingest(event)
    controller.close()
    return journal


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    return cwd


def test_session_index_is_scoped_bounded_and_rejects_symlinks(session_env):
    old = _completed_session(session_env, "ses_old")
    recent = _completed_session(session_env, "ses_recent")
    os.utime(old.path, ns=(1, 1))
    os.utime(recent.path, ns=(2, 2))
    outside = session_env.parent / "outside"
    outside.mkdir()
    (recent.directory.parent / "unsafe-link").symlink_to(
        outside, target_is_directory=True)

    index = discover_tui_sessions(str(session_env), limit=1)

    assert [item.session_id for item in index.sessions] == ["ses_recent"]
    assert index.truncated
    assert any("unsafe session entry" in warning for warning in index.warnings)

    other = session_env.parent / "other-project"
    other.mkdir()
    assert discover_tui_sessions(str(other)).sessions == ()


def test_open_session_requires_exact_existing_valid_metadata(session_env):
    journal = _completed_session(session_env, "ses_exact")

    assert open_tui_session(str(session_env), "ses_exact").directory == journal.directory
    missing = RuntimeEventJournal(str(session_env), "ses_missing")
    with pytest.raises(RuntimeEventJournalError, match="not found"):
        open_tui_session(str(session_env), "ses_missing")
    assert not missing.directory.exists()


def test_archive_is_visible_reversible_and_blocks_normal_open(session_env):
    journal = _completed_session(session_env, "ses_archive")
    original = journal.path.read_bytes()

    assert archive_tui_session(str(session_env), "ses_archive") is True
    assert archive_tui_session(str(session_env), "ses_archive") is False
    record = next(item for item in discover_tui_sessions(
        str(session_env)).sessions if item.session_id == "ses_archive")
    assert record.archived is True
    with pytest.raises(RuntimeEventJournalError, match="archived"):
        open_tui_session(str(session_env), "ses_archive")
    archived = open_tui_session(
        str(session_env), "ses_archive", allow_archived=True)
    assert archived.path.read_bytes() == original
    assert len(archived.recover().events) == 4

    assert restore_tui_session(str(session_env), "ses_archive") is True
    assert restore_tui_session(str(session_env), "ses_archive") is False
    assert open_tui_session(str(session_env), "ses_archive").archived is False


def test_archive_rejects_unresolved_turn_without_creating_marker(session_env):
    journal = RuntimeEventJournal(str(session_env), "ses_running")
    assert journal.append(_event("ses_running", 1, "session.created", payload={
        "provider_session_id": "thread-running", "state": "ACTIVE"}))
    assert journal.append(_event("ses_running", 2, "turn.started"))

    with pytest.raises(RuntimeEventJournalError, match="unresolved turn state"):
        archive_tui_session(str(session_env), "ses_running")
    assert not journal.archive_path.exists()


def test_resume_target_comes_only_from_trusted_non_redacted_session_event(
        session_env):
    journal = _completed_session(session_env, "ses_resume", "thread-resume")
    target = recover_resume_target(journal.recover())

    assert target is not None
    assert target.provider_session_id == "thread-resume"
    assert target.runtime == "codex-exec"

    secret = RuntimeEventJournal(str(session_env), "ses_redacted")
    secret.append(_event("ses_redacted", 1, "session.created", payload={
        "provider_session_id": "api_key=provider-secret"}))
    assert recover_resume_target(secret.recover()) is None
    assert b"provider-secret" not in secret.path.read_bytes()


def test_compacted_session_event_keeps_bounded_resume_handle(session_env):
    journal = RuntimeEventJournal(str(session_env), "ses_compacted")
    payload = {
        "provider_session_id": "thread-compacted",
        "items": [{"item_id": str(index)} for index in range(1_400)],
    }
    assert journal.append(_event(
        "ses_compacted", 1, "session.created", payload=payload))

    recovery = journal.recover()
    target = recover_resume_target(recovery)
    assert recovery.events[0].extensions["journal_payload_truncated"] is True
    assert target is not None
    assert target.provider_session_id == "thread-compacted"


def test_provider_page_exactly_enriches_local_appserver_summary_only(session_env):
    _completed_session(
        session_env, "ses_appserver", "thread-match",
        runtime="codex-app-server")
    index = discover_tui_sessions(str(session_env))
    page = RuntimeSessionPage(sessions=(
        RuntimeSessionSummary(
            "thread-match", name="Matched", preview="Provider preview",
            status="idle", active_flags=tuple(
                f"flag-{index}" for index in range(10)),
            model_provider="openai", updated_at=42),
        RuntimeSessionSummary(
            "thread-remote-only", name="Remote only", status="idle"),
    ), next_cursor="more")

    mismatched = merge_provider_session_page(index, page, runtime="codex-exec")
    assert mismatched.sessions[0].provider is None
    merged = merge_provider_session_page(
        index, page, runtime="codex-app-server")

    assert len(merged.sessions) == 1
    assert merged.sessions[0].provider is not None
    assert merged.sessions[0].provider.name == "Matched"
    assert merged.provider_truncated is True
    assert merged.provider_observed_at.endswith("Z")
    detail = TUISessionPickerScreen(merged)._detail(0)
    assert "Provider advisory" in detail
    assert "idle" in detail and "model provider openai" in detail
    assert "Provider preview" in detail
    assert "+7 more" in detail
    for width in (38, 72):
        compact = TUISessionPickerScreen(merged)._detail(0, width=width)
        assert len(compact.splitlines()) == 5
        assert all(len(line) <= width for line in compact.splitlines())


def test_slash_help_advertises_session_discovery_and_exact_resume():
    assert "`/sessions`" in SLASH_HELP
    assert "`/resume <session-id>`" in SLASH_HELP
    assert "`/fork [session-id]`" in SLASH_HELP
    assert "`/archive <session-id>`" in SLASH_HELP
    assert "`/unarchive <session-id>`" in SLASH_HELP
    assert "`/provider-archive <session-id>`" in SLASH_HELP
    assert "`/provider-unarchive <session-id>`" in SLASH_HELP


def test_backend_explicit_resume_is_fail_closed_until_provider_adopts(
        session_env):
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")

    snapshot = backend.resume_session("ses_target", "thread-target")

    assert backend.session_id == "ses_target"
    assert backend.provider_thread_id == "thread-target"
    assert backend.provider_resume_required is True
    assert snapshot["provider_resume_pending"] is True
    backend.new_session("fresh", session_id="ses_fresh")
    assert backend.provider_thread_id == ""
    assert backend.provider_resume_required is False
    assert backend.provider_fork_required is False


def test_backend_explicit_fork_stays_pending_until_child_handle_is_observed(
        session_env):
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")

    snapshot = backend.fork_session("ses_child", "thread-parent")

    assert backend.session_id == "ses_child"
    assert backend.provider_thread_id == "thread-parent"
    assert backend.provider_fork_required is True
    assert snapshot["provider_fork_pending"] is True

    class ForkAdapter:
        def __init__(self):
            self.session = type("Session", (), {
                "provider_session_id": "thread-parent",
            })()
            self.fork_calls = []

        def fork_session(self, request):
            self.fork_calls.append(request)
            return self.session

    adapter = ForkAdapter()
    _adopt_provider_session(adapter, backend, lambda *_args: None)
    assert adapter.fork_calls[0]["provider_session_id"] == "thread-parent"
    assert backend.provider_fork_required is True

    adapter.session.provider_session_id = "thread-child"
    _store_provider_thread(adapter, backend)
    assert backend.provider_thread_id == "thread-child"
    assert backend.provider_fork_required is False


def test_required_provider_resume_never_falls_back_to_fresh_session(session_env):
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")
    backend.resume_session("ses_target", "thread-target")

    class BrokenAdapter:
        start_calls = 0

        def resume_session(self, _request):
            raise RuntimeError("thread is gone")

        def start_session(self, _request):
            self.start_calls += 1

    adapter = BrokenAdapter()
    with pytest.raises(RuntimeError, match="required provider resume failed"):
        _adopt_provider_session(adapter, backend, lambda *_args: None)
    assert adapter.start_calls == 0
    assert backend.provider_resume_required


def test_required_provider_fork_never_falls_back_to_resume_or_fresh(session_env):
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")
    backend.fork_session("ses_child", "thread-parent")

    class BrokenAdapter:
        start_calls = 0
        resume_calls = 0

        def fork_session(self, _request):
            raise RuntimeError("fork rejected")

        def resume_session(self, _request):
            self.resume_calls += 1

        def start_session(self, _request):
            self.start_calls += 1

    adapter = BrokenAdapter()
    with pytest.raises(RuntimeError, match="required provider fork failed"):
        _adopt_provider_session(adapter, backend, lambda *_args: None)
    assert adapter.start_calls == 0
    assert adapter.resume_calls == 0
    assert backend.provider_fork_required


def test_tui_lists_and_resumes_completed_codex_session(session_env):
    _completed_session(session_env, "ses_history", "thread-history")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(session_env)), backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            before = app.query_one("#conversation", Markdown).source
            await _submit(app, pilot, "/sessions")
            await _wait_for_session_picker(app, pilot)
            assert app.query_one("#conversation", Markdown).source == before
            picker = app.screen.query_one("#session-picker-options", OptionList)
            picker.highlighted = next(
                index for index, record in enumerate(app.screen._records)
                if record.session_id == "ses_history")
            await pilot.press("enter")
            await pilot.pause()
            source = app.query_one("#conversation", Markdown).source
            assert "answer from ses_history" in source
            assert "Provider context will resume on the next turn" in source
            assert app._journal_session_id == "ses_history"
            assert backend.session_id == "ses_history"
            assert backend.provider_thread_id == "thread-history"
            assert backend.provider_resume_required
            assert not app._runtime_blocked_reason

    _run(scenario())


def test_tui_picker_archives_and_restores_without_transcript_noise(session_env):
    _completed_session(session_env, "ses_history", "thread-history")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(session_env)), backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            before = app.query_one("#conversation", Markdown).source
            await _submit(app, pilot, "/sessions")
            await _wait_for_session_picker(app, pilot)
            _select_session(app, "ses_history")
            await pilot.press("a")
            await _wait_for_session_picker(app, pilot)
            archived = next(record for record in app.screen._records
                            if record.session_id == "ses_history")
            assert archived.archived
            assert app.query_one("#conversation", Markdown).source == before

            _select_session(app, "ses_history")
            await pilot.press("enter")
            await _wait_for_session_picker(app, pilot)
            restored = next(record for record in app.screen._records
                            if record.session_id == "ses_history")
            assert not restored.archived
            assert app.query_one("#conversation", Markdown).source == before

    _run(scenario())


def test_tui_provider_archive_is_explicit_and_does_not_mark_local_history(
        session_env):
    _completed_session(
        session_env, "ses_history", "thread-history",
        runtime="codex-app-server")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")
    adapter = _ProviderActiveAdapter()

    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(session_env), runtime="app-server"),
            backend=backend, appserver_factory=lambda _cwd: adapter)
        async with app.run_test(size=(120, 40)) as pilot:
            await _submit(app, pilot, "/provider-archive ses_history")
            assert len(adapter.archive_calls) == 1
            assert adapter.archive_calls[0].provider_session_id == "thread-history"
            assert adapter.archive_calls[0].session_id == "ses_history"
            assert not RuntimeEventJournal(
                str(session_env), "ses_history").archived

            await _submit(app, pilot, "/provider-unarchive ses_history")
            assert len(adapter.unarchive_calls) == 1
            assert adapter.unarchive_calls[0].provider_session_id == "thread-history"
            assert not RuntimeEventJournal(
                str(session_env), "ses_history").archived

    _run(scenario())


def test_tui_fork_prepares_new_local_journal_without_copying_transcript(
        session_env):
    _completed_session(session_env, "ses_parent", "thread-parent")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(session_env)), backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await _submit(app, pilot, "/fork ses_parent")
            source = app.query_one("#conversation", Markdown).source
            assert "Forked" in source and "ses_parent" in source
            assert "answer from ses_parent" not in source
            assert app._journal_session_id.startswith("ses_")
            assert app._journal_session_id != "ses_parent"
            assert backend.session_id == app._journal_session_id
            assert backend.provider_thread_id == "thread-parent"
            assert backend.provider_fork_required
            assert not app._runtime_blocked_reason

    _run(scenario())


def test_tui_opens_incompatible_session_as_read_only_history(session_env):
    _completed_session(
        session_env, "ses_legacy", "legacy-provider", runtime="legacy")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="ses_current")

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(session_env)), backend=backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await _submit(app, pilot, "/resume ses_legacy")
            source = app.query_one("#conversation", Markdown).source
            assert "answer from ses_legacy" in source
            assert "Recovered history is read-only" in source
            assert app._runtime_blocked_reason
            assert backend.session_id == "ses_current"
            assert backend.provider_thread_id == ""
            assert app._runtime_view.session_id == "ses_legacy"

    _run(scenario())


class _ProviderActiveAdapter:
    def __init__(self):
        self.queue = EventQueue(16)
        self.resume_calls = []
        self.info = RuntimeInfo("codex-app-server", 1, RuntimeCapabilities(
            runtime="codex-app-server", capabilities={
                "structured_events": True, "session_resume": True,
                "session_read": True, "session_list": True,
                "session_archive": True, "message_stream": True,
                "interrupt": True,
            }))
        self.session = RuntimeSession(
            "ses_current", "thread-current", "ws_one",
            "2026-08-26T12:00:00Z", "ACTIVE")
        self.list_thread_id = 0
        self.list_query = None
        self.archive_calls = []
        self.unarchive_calls = []

    def initialize(self, _request):
        return self.info

    def runtime_info(self):
        return self.info

    def start_session(self, _request):
        return self.session

    def resume_session(self, request):
        self.resume_calls.append(dict(request))
        raise SessionReconciliationError(
            "active", "provider thread is active (waitingOnApproval)")

    def archive_session(self, request):
        self.archive_calls.append(request)

    def unarchive_session(self, request):
        self.unarchive_calls.append(request)

    def list_sessions(self, request):
        self.list_thread_id = threading.get_ident()
        self.list_query = request
        return RuntimeSessionPage(sessions=(RuntimeSessionSummary(
            "thread-history", name="History title", preview="History preview",
            status="active", active_flags=("waitingOnApproval",),
            model_provider="openai", updated_at=42),))

    def events(self):
        return RuntimeEventStream(self.queue)

    def close(self):
        self.queue.close()


class _DelayedSessionPickerHarness(TUISessionMixin):
    def __init__(self, cwd: Path):
        self.cwd = str(cwd)
        self.runtime = "codex-exec"
        self.supported = False
        self._adapter = object()
        self._session_index_generation = 0
        self.worker = None
        self.completion = None
        self.screens = []
        self.notifications = []
        self._last_status = ""

    def _session_cwd(self):
        return self.cwd

    def _runtime_capability(self, _name):
        return self.runtime, self.supported

    def _current_session_id(self):
        return ""

    def run_worker(self, worker, **_kwargs):
        self.worker = worker

    def call_from_thread(self, callback, *args):
        self.completion = callback, args

    def push_screen(self, screen, callback):
        self.screens.append((screen, callback))

    def notify(self, message, **_kwargs):
        self.notifications.append(message)

    def _refresh_status(self):
        return None


@pytest.mark.parametrize("changed_boundary", [
    "tenant", "sensitivity", "data-dir", "runtime", "capability", "adapter",
])
def test_late_session_index_result_is_discarded_across_every_boundary(
        session_env, monkeypatch, changed_boundary):
    monkeypatch.setenv("PBATCH_MEMORY_TENANT", "tenant-before")
    monkeypatch.setenv("PBATCH_MEMORY_SENSITIVITY", "internal")
    harness = _DelayedSessionPickerHarness(session_env)
    harness._open_session_picker()
    assert harness.worker is not None
    harness.worker()
    assert harness.completion is not None

    if changed_boundary == "tenant":
        monkeypatch.setenv("PBATCH_MEMORY_TENANT", "tenant-after")
    elif changed_boundary == "sensitivity":
        monkeypatch.setenv("PBATCH_MEMORY_SENSITIVITY", "confidential")
    elif changed_boundary == "data-dir":
        monkeypatch.setenv("PBATCH_DATA_DIR", str(session_env.parent / "other-data"))
    elif changed_boundary == "runtime":
        harness.runtime = "legacy"
    elif changed_boundary == "capability":
        harness.supported = True
    else:
        harness._adapter = object()

    callback, args = harness.completion
    callback(*args)

    assert harness.screens == []


def test_runtime_capability_never_reenters_initialize_on_ui_path(session_env):
    class InitializeOnlyAdapter:
        initialize_calls = 0

        def initialize(self, _request):
            self.initialize_calls += 1
            raise AssertionError("capability lookup must not handshake")

    class CapabilityHarness(TUISessionMixin):
        _structured_backend = False
        _adapter = InitializeOnlyAdapter()

        def _session_cwd(self):
            return str(session_env)

    harness = CapabilityHarness()

    assert harness._runtime_capability("session_list") == ("", False)
    assert harness._adapter.initialize_calls == 0


def test_appserver_provider_reconciliation_keeps_active_history_read_only(
        session_env):
    _completed_session(
        session_env, "ses_history", "thread-history",
        runtime="codex-app-server")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="backend-current")
    adapter = _ProviderActiveAdapter()

    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(session_env), runtime="app-server"),
            backend=backend, appserver_factory=lambda _cwd: adapter)
        async with app.run_test(size=(120, 40)) as pilot:
            await _submit(app, pilot, "/resume ses_history")
            source = app.query_one("#conversation", Markdown).source
            assert "answer from ses_history" in source
            assert "provider reports that the thread is still active" in source
            assert "Start `/new` to execute" in source
            assert app._runtime_blocked_reason
            assert app._runtime_view.session_id == "ses_history"
            assert app._adapter_session_id == "ses_current"
            assert adapter.resume_calls[0]["provider_session_id"] == "thread-history"

    _run(scenario())


def test_appserver_session_picker_loads_provider_page_off_ui_thread(session_env):
    _completed_session(
        session_env, "ses_history", "thread-history",
        runtime="codex-app-server")
    backend = TUIBackend(
        cwd=str(session_env), agent="codex", memory_mode="off",
        session_id="backend-current")
    adapter = _ProviderActiveAdapter()

    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(session_env), runtime="app-server"),
            backend=backend, appserver_factory=lambda _cwd: adapter)
        async with app.run_test(size=(120, 40)) as pilot:
            before = app.query_one("#conversation", Markdown).source
            await _submit(app, pilot, "/sessions")
            for _ in range(20):
                if isinstance(app.screen, TUISessionPickerScreen):
                    break
                await pilot.pause()
            assert isinstance(app.screen, TUISessionPickerScreen)
            record = next(item for item in app.screen._records
                          if item.session_id == "ses_history")
            assert record.provider is not None
            assert record.provider.status == "active"
            assert adapter.list_thread_id and adapter.list_thread_id != app._ui_thread_id
            assert adapter.list_query.limit == 100
            assert app.query_one("#conversation", Markdown).source == before
            detail = app.screen._detail(app.screen._records.index(record))
            assert "History preview" in detail
            assert "waitingOnApproval" in detail

    _run(scenario())


async def _submit(app: PiBatchTUI, pilot, command: str) -> None:
    composer = app.query_one("#composer", Input)
    composer.value = command
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for_session_picker(app: PiBatchTUI, pilot) -> None:
    for _ in range(60):
        if isinstance(app.screen, TUISessionPickerScreen):
            return
        await pilot.pause()
    assert isinstance(app.screen, TUISessionPickerScreen)


def _select_session(app: PiBatchTUI, session_id: str) -> None:
    assert isinstance(app.screen, TUISessionPickerScreen)
    picker = app.screen.query_one("#session-picker-options", OptionList)
    picker.highlighted = next(
        index for index, record in enumerate(app.screen._records)
        if record.session_id == session_id)


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()
