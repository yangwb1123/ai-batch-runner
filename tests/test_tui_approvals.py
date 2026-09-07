"""Typed approval projection, modal and App Server wiring contracts."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_approvals import ApprovalRequest, TUIApprovalScreen
from pbatch.tui_appserver_support import EventQueue, RuntimeEventStream
from pbatch.tui_controller import TUIEventController
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle


def _event(seq: int, kind: str, payload=None, *, request_turn: str = "turn_approval"):
    return RuntimeEvent(
        event_id=f"evt_approval_{seq}", seq=seq,
        occurred_at=f"2026-08-26T12:00:{seq:02d}Z",
        workspace_id="ws_approval", session_id="ses_approval",
        run_id="run_approval", turn_id=request_turn,
        actor_id="agent_main", kind=kind, payload=payload or {},
        extensions={"runtime": "codex-app-server"})


def _request_payload(request_id="request-1"):
    return {
        "request_id": request_id,
        "approval_type": "command_execution",
        "method": "item/commandExecution/requestApproval",
        "target": 'command: ["git","status"]',
        "received_at": "2026-08-26T12:00:01Z",
        "expires_at": "2026-08-26T12:02:01Z",
        "available_decisions": ["allow", "allow_session", "deny", "cancel"],
    }


def test_projection_waits_on_typed_event_and_uses_exact_request_id_type():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))

    view = controller.ingest(_event(2, "approval.requested", _request_payload("1")))

    assert view is not None
    assert view.turn_state == TurnState.WAITING_APPROVAL
    assert view.approvals.active.request_id == "1"
    assert view.assistant_text == ""
    assert "git" in view.approvals.active.target

    wrong_type = controller.ingest(_event(
        3, "approval.resolved", {
            "request_id": 1, "approval_type": "command_execution"}))
    assert wrong_type.approvals.waiting is True

    resolved = controller.ingest(_event(
        4, "approval.resolved", {
            "request_id": "1", "approval_type": "command_execution",
            "decision": "allow"}))
    assert resolved.approvals.waiting is False
    assert resolved.turn_state == TurnState.RUNNING


def test_projection_rejects_duplicate_or_mismatched_typed_request():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "approval.requested", _request_payload()))

    with pytest.raises(ValueError, match="duplicate pending"):
        controller.ingest(_event(3, "approval.requested", _request_payload()))
    with pytest.raises(ValueError, match="type mismatch"):
        controller.ingest(_event(4, "approval.resolved", {
            "request_id": "request-1", "approval_type": "file_change"}))


def test_terminal_event_clears_pending_card_without_inventing_a_decision():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "approval.requested", _request_payload()))

    view = controller.ingest(_event(3, "turn.failed", {"reason": "provider stopped"}))

    assert view.turn_state == TurnState.FAILED
    assert view.approvals.pending == ()


class _Backend:
    def __init__(self, emit, **options):
        self.emit = emit
        self.cwd = options.get("cwd", "")
        self.agent = options.get("agent", "pi")
        self.provider = options.get("provider", "")
        self.session_id = "backend-session"

    def run_turn(self, _prompt):
        raise AssertionError("explicit App Server runtime must own the turn")

    def snapshot(self):
        return {"status": "idle", "session_id": self.session_id}

    def new_session(self, _name=""):
        return {"session_id": self.session_id}

    def cancel(self):
        return True


class _ApprovalAdapter:
    def __init__(self):
        self.queue = EventQueue(32)
        self.initialize_calls = 0
        self.decisions = []
        self.handle = None
        self.closed = False
        caps = RuntimeCapabilities(
            runtime="codex-app-server",
            capabilities={
                "structured_events": True, "message_stream": True,
                "interrupt": True, "typed_approvals": True,
                "turn_steer": True, "session_resume": True,
                "session_fork": True,
            })
        self.info = RuntimeInfo("codex-app-server", 1, caps)
        self.session = RuntimeSession(
            "ses_approval", "thread-approval", "ws_approval",
            "2026-08-26T12:00:00Z", "ACTIVE")

    def initialize(self, _request):
        self.initialize_calls += 1
        return self.info

    def runtime_info(self):
        return self.info

    def start_session(self, _request):
        return self.session

    def events(self):
        return RuntimeEventStream(self.queue)

    def start_turn(self, _request):
        self.handle = TurnHandle(
            self.session.session_id, "run_approval", "turn_approval",
            "agent_main", "2026-08-26T12:00:00Z", TurnState.RUNNING)
        self.queue.put(_event(1, "turn.started"))
        self.queue.put(_event(2, "approval.requested", _request_payload()))
        return self.handle

    def resolve_approval(self, decision):
        expected = {
            "workspace_id": "ws_approval", "session_id": "ses_approval",
            "run_id": "run_approval", "turn_id": "turn_approval",
            "actor_id": "agent_main",
        }
        if any(decision.get(name) != value for name, value in expected.items()):
            return False
        self.decisions.append(dict(decision))
        self.queue.put(_event(3, "approval.resolved", {
            "request_id": "request-1", "approval_type": "command_execution",
            "decision": decision["decision"]}))
        self.queue.put(_event(4, "turn.completed", {"state": TurnState.SUCCEEDED}))
        self.handle.state = TurnState.SUCCEEDED
        self.handle._done.set()
        return True

    def interrupt_turn(self, _request):
        if self.handle is not None and not self.handle.done:
            self.queue.put(_event(5, "turn.interrupted"))
            self.handle.state = TurnState.INTERRUPTED
            self.handle._done.set()

    def close(self):
        self.closed = True
        self.queue.close()


def test_explicit_appserver_runtime_opens_modal_and_sends_scoped_decision(tmp_path):
    async def scenario():
        adapter = _ApprovalAdapter()
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), runtime="app-server"),
            backend_factory=_Backend, appserver_factory=lambda _cwd: adapter)
        async with app.run_test(size=(110, 34)) as pilot:
            assert app._adapter is adapter
            assert adapter.initialize_calls == 1
            app._start_turn("inspect safely")
            await _wait_until(lambda: isinstance(app.screen, TUIApprovalScreen), pilot)

            assert app._runtime_view.turn_state == TurnState.WAITING_APPROVAL
            assert "git" in app.screen.request.target
            await pilot.press("a")
            await _wait_until(lambda: not app._busy, pilot)

            assert adapter.decisions[0]["decision"] == "allow"
            assert adapter.decisions[0]["turn_id"] == "turn_approval"
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED
            assert app._runtime_view.approvals.pending == ()
        adapter.close()

    _run(scenario())


def test_read_only_approval_card_never_returns_allow():
    async def scenario():
        expires = datetime.now(timezone.utc) + timedelta(minutes=2)
        request = ApprovalRequest(
            "read-only", "command_execution",
            "item/commandExecution/requestApproval", "command: echo safe",
            datetime.now(timezone.utc).isoformat(), expires.isoformat(),
            ("allow", "deny"), "ws", "ses", "run", "turn", "agent_main")
        app = PiBatchTUI(TuiOptions(), backend_factory=_Backend)
        decisions = []
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(
                TUIApprovalScreen(request, read_only=True), decisions.append)
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, TUIApprovalScreen)
            assert decisions == []
            await pilot.press("escape")
            await pilot.pause()
            assert decisions == [None]

    _run(scenario())


def test_explicit_appserver_failure_blocks_instead_of_faking_legacy(tmp_path):
    async def scenario():
        def unavailable(_cwd):
            raise RuntimeError("socket unavailable")

        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), runtime="app-server"),
            backend_factory=_Backend, appserver_factory=unavailable)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app._adapter is None
            assert "App Server unavailable" in app._runtime_blocked_reason
            assert app._start_turn("must not fall back") is False
            assert app._busy is False

    _run(scenario())


async def _wait_until(predicate, pilot, attempts=300):
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
