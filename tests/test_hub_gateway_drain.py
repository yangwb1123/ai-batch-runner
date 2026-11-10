"""Gateway scheduling with a real temporary outbox and controlled HTTP/clock boundaries."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from pbatch.hub_gateway import HubGateway
from pbatch.hub_client import HubError
from pbatch.hub_gateway_store import GatewayStore
from tests.test_gateway_file_delivery import FileClient
from tests.test_hub_gateway import _FakeSession, _command, _options


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class RecordingStop:
    """No real sleeping; bounded recorder for the foreground delivery loop only."""
    def __init__(self, clock, client, done=lambda: True):
        self.clock, self.client, self.done = clock, client, done
        self.stopped = False
        self.waits = []
        self.checks = 0

    def is_set(self):
        self.checks += 1
        assert self.checks < 5000, "foreground loop spun without its bounded stop condition"
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        if self.stopped:
            return True
        self.waits.append((len(self.client.attempts), seconds))
        assert len(self.waits) <= 10, "foreground loop did not converge"
        self.clock.now += seconds
        self.stopped = self.stopped or self.done()
        return self.stopped


class DrainClient(FileClient):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.attempts, self.timeline = [], []
        self.event_reply = None
        self.command_reply = None
        self.invalid_file_ack = False

    def send_event(self, event, **values):
        self.attempts.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        self.timeline.append(("event", event["event_id"], self.clock.now))
        if self.event_reply:
            response = self.event_reply(event)
            if response is not None:
                return response
        return super().send_event(event, **values)

    def heartbeat(self, **values):
        self.timeline.append(("heartbeat", "", self.clock.now))
        return super().heartbeat(**values)

    def get_commands(self, **values):
        self.timeline.append(("poll", "", self.clock.now))
        if self.command_reply:
            return self.command_reply()
        return super().get_commands(**values)

    def publish_file_snapshots(self, body):
        self.timeline.append(("file", body["turn_id"], self.clock.now))
        response = super().publish_file_snapshots(body)
        return {} if self.invalid_file_ack else response


@pytest.fixture
def drain(tmp_path, monkeypatch, request):
    clock = Clock()
    local_time = SimpleNamespace(monotonic=clock.monotonic)
    monkeypatch.setattr("pbatch.hub_gateway.time", local_time)
    monkeypatch.setattr("pbatch.hub_gateway_delivery.time", local_time)
    (tmp_path / "out.txt").write_text("owned fixture bytes")
    client, session = DrainClient(clock), _FakeSession()
    values = {"share_files": ("out.txt",), **getattr(request, "param", {})}
    if values.get("observe_only"):
        (tmp_path / "project").mkdir()
        values.update(cwd=str(tmp_path / "project"), share_files=(), sessions=0, max_sessions=0,
                      instance_id="drain-observer", compute_tools=False)
        client.base_url = "http://127.0.0.1:8788"
    gateway = HubGateway(_options(tmp_path, **values), client=client,
                         session_factory=lambda options: session)
    # Creation/closing workers have their own real waits and are outside this loop's scope.
    monkeypatch.setattr(gateway._creation, "start", lambda: None)
    monkeypatch.setattr(gateway._closing, "start", lambda: None)
    gateway.start()
    client.timeline.clear()
    gateway._stop = RecordingStop(clock, client)
    try:
        yield gateway, client, session, clock
    finally:
        gateway.close()


def enqueue(gateway, count, *, session="hub-fixture", kind="message.delta", start=0):
    identifiers = []
    for index in range(start, start + count):
        identity = f"drain-{index:03d}"
        body = {"event_id": identity, "session_id": session, "turn_id": f"turn-{index:03d}",
                "kind": kind, "payload": {"text": f"durable fixture event {index}"}}
        gateway._store.enqueue_event(event_id=identity, event=body, generation=gateway.generation)
        identifiers.append(identity)
    return identifiers


def test_seventy_two_healthy_events_drain_three_batches_without_intermediate_wait(drain):
    gateway, client, _, _ = drain
    identities = enqueue(gateway, 72)
    gateway._stop.done = lambda: len(client.attempts) >= 72
    gateway.run_forever()
    assert gateway._store.pending_events() == []
    assert [json.loads(body)["event_id"] for body in client.attempts] == identities
    assert gateway._stop.waits == [(72, gateway.options.poll_interval)]


def test_single_iteration_stays_none_and_never_sends_more_than_thirty_two(drain):
    gateway, client, _, _ = drain
    enqueue(gateway, 72)
    for count in (32, 64, 72):
        assert gateway.run_once() is None
        assert len(client.attempts) == count
    assert gateway._store.pending_events() == []


def test_half_second_budget_is_checked_between_synchronous_requests(drain):
    gateway, client, _, clock = drain
    enqueue(gateway, 72)

    def advance(_event):
        clock.now += 0.2

    client.event_reply = advance
    assert gateway.run_once() is None
    assert len(client.attempts) == 3
    assert gateway._store.pending_events()[0]["event_id"] == "drain-003"
    assert clock.now == pytest.approx(100.6)  # The in-flight third request is not interrupted.


def test_delivery_result_is_immutable_and_describes_actual_bounded_progress(drain):
    from pbatch.hub_gateway_delivery import DeliveryResult, flush_outbox
    gateway, _, _, _ = drain
    enqueue(gateway, 40)
    result = flush_outbox(gateway)
    assert result == DeliveryResult(advanced=32, budget_exhausted=True, blocked=False)
    with pytest.raises(FrozenInstanceError):
        result.advanced = 99
    assert gateway._files.flush() == DeliveryResult()


def test_budget_exhaustion_without_progress_does_not_busy_spin(drain, monkeypatch):
    gateway, client, _, _ = drain
    enqueue(gateway, 72)
    ticks = iter(range(100, 10000))
    monkeypatch.setattr("pbatch.hub_gateway_delivery.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    gateway.run_forever()
    assert client.attempts == [] and gateway._stop.waits == [(0, 0.1)]
    assert len(gateway._store.pending_events(limit=100)) == 72


@pytest.mark.parametrize("drain", [{}, {"observe_only": True}], indirect=True)
def test_empty_or_observation_only_gateway_keeps_normal_wait(drain):
    gateway, client, session, _ = drain
    if gateway.options.observe_only:
        enqueue(gateway, 72)  # Unexpected local rows never grant execution authority.
    gateway.run_forever()
    assert client.attempts == [] and gateway._stop.waits == [(0, 0.1)]
    assert session.calls == 0
    if gateway.options.observe_only:
        assert not any(row[0] == "poll" for row in client.timeline)


@pytest.mark.parametrize("failure", ["temporary", "missing_cursor"])
def test_partial_success_then_failed_head_waits_and_retries_original_bytes(drain, failure):
    gateway, client, _, _ = drain
    identities = enqueue(gateway, 72)
    rejected = []

    def fail_once(event):
        if event["event_id"] == identities[5] and not rejected:
            rejected.append(event["event_id"])
            if failure == "temporary":
                raise HubError("controlled transient failure", code="hub_unavailable")
            return {}

    client.event_reply = fail_once
    gateway.run_forever()
    assert gateway._stop.waits == [(6, 0.1)]
    assert gateway._store.pending_events()[0]["event_id"] == identities[5]
    assert [json.loads(body)["event_id"] for body in client.attempts] == identities[:6]
    gateway._stop.stopped = False
    gateway._stop.done = lambda: len(client.attempts) >= 73
    gateway.run_forever()
    assert client.attempts[5] == client.attempts[6]
    assert [json.loads(body)["event_id"] for body in client.attempts] == identities[:6] + identities[5:]
    assert gateway._store.pending_events() == []


@pytest.mark.parametrize("failure", ["temporary", "missing_cursor", "unresolved_state"])
def test_healthy_event_lane_cannot_accelerate_unresolved_start_ack(drain, failure):
    gateway, client, session, _ = drain
    enqueue(gateway, 72)
    assert gateway._dispatch_command(_command(session, "pending-start"))

    def pending_reply(event):
        if event["kind"] != "turn.running":
            return None
        if failure == "temporary":
            raise HubError("start acknowledgement unknown", code="hub_unavailable")
        return {} if failure == "missing_cursor" else {"cursor": "accepted", "turn_state": "queued"}

    client.event_reply = pending_reply
    gateway.run_forever()
    assert gateway._stop.waits == [(33, 0.1)]
    assert sum(json.loads(body)["kind"] == "message.delta" for body in client.attempts) == 32
    assert "pending-start" in gateway._pending_starts and session.calls == 0


@pytest.mark.parametrize("lane", ["heartbeat", "poll"])
def test_control_lane_failure_waits_even_when_thirty_two_events_advance(drain, monkeypatch, lane):
    gateway, client, _, clock = drain
    enqueue(gateway, 72)
    clock.now = 131.0
    previous_heartbeat, failures = gateway._last_heartbeat, []

    def unavailable(**_values):
        failures.append(clock.now)
        raise HubError("control lane temporarily unavailable", code="hub_unavailable")

    monkeypatch.setattr(client, "heartbeat" if lane == "heartbeat" else "get_commands", unavailable)
    gateway.run_forever()
    assert len(client.attempts) == 32 and gateway._stop.waits == [(32, 0.1)]
    assert len(failures) == 1
    assert len(gateway._store.pending_events(limit=100)) == 40
    if lane == "heartbeat":
        assert gateway._last_heartbeat == previous_heartbeat


def test_only_skipped_pending_running_rows_make_no_outbox_progress(drain):
    from pbatch.hub_gateway_delivery import flush_outbox
    gateway, client, session, _ = drain
    assert gateway._dispatch_command(_command(session, "pending-only"))
    result = flush_outbox(gateway)
    assert client.attempts == [] and len(gateway._store.pending_events()) == 1
    assert result.advanced == 0
    assert session.calls == 0


def queued_file(gateway, suffix="one"):
    turn, identity = "file-turn-" + suffix, "file-terminal-" + suffix
    body = {"event_id": identity, "session_id": "hub-file-fixture", "turn_id": turn,
            "kind": "turn.completed", "payload": {}}
    gateway._store.enqueue_event(event_id=identity, event=body, generation=gateway.generation)
    gateway._files.synthetic(turn, body["session_id"], identity)
    return turn, identity


@pytest.mark.parametrize("failure", ["temporary", "invalid_ack"])
def test_event_progress_does_not_accelerate_unknown_file_delivery(drain, failure):
    gateway, client, _, _ = drain
    turn, _ = queued_file(gateway)
    enqueue(gateway, 72)
    if failure == "temporary":
        client.failure = HubError("file acknowledgement unknown", code="hub_unavailable")
    else:
        client.invalid_file_ack = True
    gateway.run_forever()
    assert gateway._stop.waits == [(32, 0.1)] and len(client.uploads) == 1
    assert gateway._files.queue.receipt(turn)["state"] == "pending"
    client.failure, client.invalid_file_ack = None, False
    gateway._stop.stopped = False
    gateway.run_once()
    assert client.uploads[0] == client.uploads[1]
    assert gateway._files.queue.receipt(turn)["state"] == "acknowledged"


def test_files_remain_ineligible_until_their_terminal_event_is_acknowledged(drain):
    gateway, client, _, _ = drain
    turn, identity = queued_file(gateway)

    def reject_terminal(event):
        assert event["event_id"] == identity
        raise HubError("terminal acknowledgement unknown", code="hub_unavailable")

    client.event_reply = reject_terminal
    gateway.run_once()
    assert client.uploads == [] and gateway._files.queue.receipt(turn)["state"] == "pending"
    client.event_reply = None
    gateway.run_once()
    assert len(client.uploads) == 1 and gateway._files.queue.receipt(turn)["state"] == "acknowledged"


def test_each_full_iteration_gives_file_delivery_a_turn_during_event_backlog(drain):
    gateway, client, _, _ = drain
    for suffix in ("one", "two", "three"):
        queued_file(gateway, suffix)
    enqueue(gateway, 72)
    gateway._stop.done = lambda: len(client.attempts) >= 75
    gateway.run_forever()
    events, file_positions = 0, []
    for kind, _, _ in client.timeline:
        events += kind == "event"
        if kind == "file":
            file_positions.append(events)
    assert file_positions == [32, 64, 75]
    assert gateway._stop.waits == [(75, 0.1)]
    assert len(client.uploads) == 3


@pytest.mark.parametrize("drain", [{"heartbeat_interval": 1}], indirect=True)
def test_cancel_is_polled_before_next_batch_while_heartbeat_and_poll_stay_throttled(drain):
    gateway, client, session, clock = drain
    enqueue(gateway, 72)
    managed = gateway._managed(session.local_id)
    managed.active_turn = "active-cancel"
    cancellations = []
    original_abort = session.abort

    def cancel():
        cancellations.append(len(client.attempts))
        original_abort()

    session.abort = cancel
    client.command_reply = lambda: ([] if clock.now < 100.5 or cancellations else
        [{**_command(session, "active-cancel"), "cancel_requested": True}])

    def advance(_event):
        clock.now += 0.04

    client.event_reply = advance
    gateway._stop.done = lambda: len(client.attempts) >= 72
    gateway.run_forever()
    assert cancellations == [13] and session.aborted
    heartbeats = [stamp for kind, _, stamp in client.timeline if kind == "heartbeat"]
    polls = [stamp for kind, _, stamp in client.timeline if kind == "poll"]
    assert len(heartbeats) == 2 and all(b - a >= 1 for a, b in zip([100] + heartbeats, heartbeats))
    assert len(polls) == 6 and all(b - a >= 0.1 for a, b in zip(polls, polls[1:]))
    assert gateway._stop.waits == [(72, 0.1)]


def test_obsolete_events_advance_without_retry_and_other_sessions_continue(drain):
    gateway, client, _, _ = drain
    identities = enqueue(gateway, 40, session="frozen-session") + enqueue(gateway, 32, start=40)

    def reject_old(event):
        if event["session_id"] == "frozen-session":
            raise HubError("closed session", code="session_read_only", status=409)

    client.event_reply = reject_old
    gateway._stop.done = lambda: len(client.attempts) >= 72
    gateway.run_forever()
    assert [json.loads(body)["event_id"] for body in client.attempts] == identities
    assert gateway._store.pending_events() == [] and gateway._fatal is None
    assert gateway._stop.waits == [(72, 0.1)]


def test_fencing_during_foreground_delivery_stops_without_next_lane_or_wait(drain):
    gateway, client, session, _ = drain
    enqueue(gateway, 72)

    def fenced(_event):
        raise HubError("wrong generation", code="stale_generation", status=409)

    client.event_reply = fenced
    with pytest.raises(RuntimeError, match="fencing generation"):
        gateway.run_forever()
    assert session.aborted and gateway._fatal is not None and gateway._stop.is_set()
    assert len(client.attempts) == 1 and gateway._stop.waits == [] and client.uploads == []


@pytest.mark.parametrize("lane", ["heartbeat", "event"])
@pytest.mark.parametrize("response", ["success", "fenced"])
def test_close_during_inflight_foreground_lane_ignores_late_reply_without_closed_store_access(
        drain, monkeypatch, lane, response):
    gateway, client, session, clock = drain
    enqueue(gateway, 72)
    entered, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        if response == "fenced":
            raise HubError("late fencing reply", code="stale_generation", status=409)
        return {"cursor": "late", "ok": True}

    clock.now = 131.0
    if lane == "heartbeat":
        monkeypatch.setattr(client, "heartbeat", held)
    else:
        client.event_reply = held
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(gateway.run_forever)
            assert entered.wait(2)
            before_close = list(client.timeline)
            gateway.close()
            release.set()
            future.result(timeout=2)
        assert session.calls == 0 and gateway._stop.waits == []
        assert client.timeline == before_close
        assert gateway._fatal is None and client.uploads == []
        reopened = GatewayStore(gateway.options.state_dir)
        try:
            assert len(reopened.pending_events(limit=100)) == 72
        finally:
            reopened.close()
    finally:
        release.set()
