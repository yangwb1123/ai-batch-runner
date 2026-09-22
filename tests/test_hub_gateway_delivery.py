"""A closing session cannot fence or block event delivery for other sessions."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_gateway import HubGateway
from pbatch.hub_gateway_store import GatewayStore
from test_hub_gateway import _FakeSession, _command, _options, _wait_for
from test_hub_gateway_closing import CloseClient, close_acknowledged


class DeliveryClient(CloseClient):
    def __init__(self):
        super().__init__()
        self.read_only = set()
        self.attempts = []
        self.stored_events = {}
        self.fail_terminal_once = False
        self.rejection = "session_read_only"

    def send_event(self, event, **values):
        self.attempts.append(event)
        prior = self.stored_events.get(event["event_id"])
        if prior is not None:
            assert prior[0] == event
            return prior[1]
        if event["session_id"] in self.read_only:
            raise HubError("private old session details", code=self.rejection, status=409)
        result = super().send_event(event, **values)
        self.stored_events[event["event_id"]] = (dict(event), result)
        if self.fail_terminal_once and event["kind"] == "turn.completed":
            self.fail_terminal_once = False
            raise HubError("lost terminal acknowledgement", code="hub_unavailable")
        return result


@pytest.fixture
def delivery(tmp_path):
    client = DeliveryClient()
    sessions = [_FakeSession(), _FakeSession()]
    pending = iter(sessions)
    gateway = HubGateway(_options(tmp_path, sessions=2), client=client,
                         session_factory=lambda _options: next(pending))
    gateway.start()
    yield gateway, client, sessions
    gateway.close()


@pytest.mark.parametrize("cancelled", [False, True])
def test_read_only_running_ack_and_late_cancel_events_do_not_block_other_sessions(delivery, cancelled):
    gateway, client, (old, current) = delivery
    first = _command(old, "old-turn")
    assert gateway._dispatch_command(first)
    if cancelled:
        gateway._dispatch_command({**first, "cancel_requested": True})
    client.read_only.add(first["session_id"])
    gateway._retry_pending_start()
    gateway._flush_outbox()
    assert old.calls == 0 and gateway._pending_starts == {}
    assert gateway._store.pending_events() == []
    assert gateway._fatal is None and not gateway._stop.is_set()
    assert gateway._dispatch_command(first) is False
    second = _command(current, "other-turn")
    assert gateway._dispatch_command(second)
    gateway._retry_pending_start()
    _wait_for(lambda: current.calls == 1 and not gateway._turn_threads)
    gateway._flush_outbox()
    assert any(row["session_id"] == second["session_id"] and row["kind"] == "turn.completed"
               for row in client.events)
    assert gateway._store.pending_events() == []
    if not cancelled:
        assert gateway._store.turn("old-turn")["state"] == "cancelled"


def test_terminal_ack_loss_then_close_replays_only_stored_event_and_delivers_other_sessions(delivery):
    gateway, client, (old, current) = delivery
    first = _command(old, "old-turn")
    assert gateway._dispatch_command(first)
    gateway._retry_pending_start()
    _wait_for(lambda: not gateway._turn_threads)
    client.fail_terminal_once = True
    gateway._flush_outbox()
    pending = gateway._store.pending_events()
    assert len(pending) == 1 and pending[0]["body"]["kind"] == "turn.completed"
    client.read_only.add(first["session_id"])
    client.request_close(gateway, old.local_id)
    _wait_for(lambda: close_acknowledged(gateway))
    # A new synthetic result has no canonical Hub receipt after freezing.
    gateway._publish_synthetic("late-result", first["session_id"], "turn.failed", {"reason": "late"})
    second = _command(current, "other-turn")
    assert gateway._dispatch_command(second)
    gateway._retry_pending_start()
    _wait_for(lambda: current.calls == 1 and not gateway._turn_threads)
    gateway._flush_outbox()
    assert gateway._store.pending_events() == []
    assert gateway._store.turn("old-turn")["state"] == "completed"
    assert sum(event["event_id"] == pending[0]["event_id"] for event in client.attempts) == 2
    assert any(row["turn_id"] == "other-turn" and row["kind"] == "turn.completed"
               for row in client.events)
    assert all(row["turn_id"] != "late-result" for row in client.events)
    assert gateway._fatal is None and not gateway._stop.is_set()


@pytest.mark.parametrize("path", ["running", "outbox"])
@pytest.mark.parametrize("result", ["success", "read_only", "fenced"])
def test_shutdown_during_event_send_ignores_late_ack_or_failure_without_sqlite_access(
        delivery, monkeypatch, path, result):
    gateway, client, (old, _) = delivery
    entered, release = threading.Event(), threading.Event()
    original = client.send_event

    def blocked(event, **values):
        entered.set()
        assert release.wait(5)
        if result != "success":
            code = "session_read_only" if result == "read_only" else "stale_generation"
            raise HubError("late response", code=code, status=409)
        return original(event, **values)

    monkeypatch.setattr(client, "send_event", blocked)
    command = _command(old)
    assert gateway._dispatch_command(command)
    if path == "outbox":
        gateway._cancel_command({**command, "cancel_requested": True}, gateway._managed(old.local_id))
    operation = gateway._retry_pending_start if path == "running" else gateway._flush_outbox
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(operation)
            assert entered.wait(2)
            gateway.close()
            release.set()
            waiting.result(timeout=2)
        assert old.calls == 0 and gateway._fatal is None
        reopened = GatewayStore(gateway.options.state_dir)
        try:
            assert reopened.pending_events()
        finally:
            reopened.close()
    finally:
        release.set()


@pytest.mark.parametrize("path", ["running", "outbox"])
def test_wrong_generation_still_fences_the_instance(delivery, path):
    gateway, client, (old, current) = delivery
    command = _command(old)
    assert gateway._dispatch_command(command)
    client.read_only.add(command["session_id"])
    client.rejection = "stale_generation"
    if path == "outbox":
        gateway._pending_starts.clear()
    operation = gateway._retry_pending_start if path == "running" else gateway._flush_outbox
    with pytest.raises(HubError) as captured:
        operation()
    assert captured.value.code == "stale_generation"
    assert gateway._fatal is not None and gateway._stop.is_set()
    assert old.calls == 0 and current.aborted


def test_read_only_outbox_rejections_are_bounded_and_never_retry_an_obsolete_row(delivery):
    gateway, client, (old, _) = delivery
    session_id = "hub-" + old.local_id
    client.read_only.add(session_id)
    for index in range(40):
        gateway._publish_synthetic(f"late-{index}", session_id, "turn.failed", {"reason": "late"})
    gateway._flush_outbox()
    assert 0 < len(client.attempts) <= 32
    for _ in range(8):
        gateway._flush_outbox()
    assert len(client.attempts) == len({row["event_id"] for row in client.attempts}) == 40
    assert gateway._store.pending_events() == []
    assert gateway._fatal is None and not gateway._stop.is_set()
