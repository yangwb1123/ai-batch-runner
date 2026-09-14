"""Lifecycle outbox persistence, recovery and authorization boundaries."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pbatch.hub_auth import HubPrincipal, USER_SCOPES
from pbatch.hub_ecosystem_http import IntegrationError
from pbatch.hub_errors import HubAccessError
from pbatch.hub_integration_store import IM_RETRY_WINDOW
from pbatch.hub_integrations import HubIntegrations
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store import HubStore, HubStoreError


class Sink:
    name, tenant_id, kind, fingerprint = "audit", "team", "audit", "fixed-target"

    def __init__(self):
        self.ledger, self.calls = {}, []
        self.error, self.prepare_error = None, None
        self.block, self.entered = None, threading.Event()

    def prepare(self, fact):
        if fact["status"] not in {"cancelled", "completed", "lost"}:
            return None
        if self.prepare_error:
            raise self.prepare_error
        return dict(fact)

    def deliver(self, payload, key):
        self.entered.set()
        if self.block is not None:
            assert self.block.wait(5)
        self.calls.append((key, payload))
        self.ledger.setdefault(key, payload)
        if self.error:
            raise self.error
        return {"receipt_id": key}


@pytest.fixture
def system(tmp_path):
    now = [1000.0]
    hub = HubStore(tmp_path / "hub.sqlite", clock=lambda: now[0])
    hub.register_instance("team", "alpha", {"boot_id": "one"})
    session = hub.upsert_session("team", "alpha", 1, {
        "local_session_id": "local", "controllable": True, "project_id": "project"})
    user = HubPrincipal("team", "alice", "console", "user", frozenset({"alpha"}), USER_SCOPES)
    result = SimpleNamespace(hub=hub, now=now, session=session, user=user)
    yield result
    result.hub.close()


def cancelled(system, key="request", actor="alice"):
    turn = system.hub.submit_turn("team", system.session["session_id"], "alice",
                                  "never export this prompt or password", key)
    system.hub.cancel_turn("team", turn["turn_id"], actor)
    return turn


def runtime(system, sink=None, start=0):
    sink = sink or Sink()
    return HubIntegrations(system.hub, configured=[(sink, start)]), sink


def test_fact_projection_excludes_content_and_records_real_canceller(system):
    turn = cancelled(system, actor="operator")
    integration, sink = runtime(system)
    integration.tick()
    payload = next(iter(sink.ledger.values()))
    assert payload["turn_id"] == turn["turn_id"]
    assert payload["actor_subject"] == "alice"
    assert payload["trigger_subject"] == "operator"
    assert "never export" not in json.dumps(payload)
    assert set(payload) == {"tenant_id", "event_id", "cursor", "kind", "created_at", "instance_id",
                            "session_id", "turn_id", "task_id", "actor_subject", "trigger_subject", "status"}
    rows = integration.store.list("team", ("alpha",))
    assert len(rows) == 1 and rows[0]["state"] == "delivered"
    assert not {"payload", "fact", "fingerprint"} & set(rows[0])


def test_new_sink_starts_now_explicit_replay_and_no_duplicate_projection(system):
    cancelled(system)
    live, sink = runtime(system, start=None)
    live.tick()
    assert not sink.calls
    cancelled(system, key="next")
    live.tick()
    live.tick()
    assert len(sink.calls) == 1
    other = Sink()
    other.name = "replay"
    replay, _ = runtime(system, other, start=0)
    for _ in range(3):
        replay.tick()
    assert len(other.calls) == 2


def test_ack_lost_then_process_restart_reuses_exact_payload_and_id(system):
    cancelled(system)
    original, sink = runtime(system)
    original.store.project(sink)
    pending = original.store.due(sink)[0]
    claimed = original.store.claim("team", pending["delivery_id"])
    sink.deliver(claimed["payload"], claimed["delivery_id"])
    path = system.hub.path
    system.hub.close()
    system.hub = HubStore(path, clock=lambda: system.now[0])
    resumed, _ = runtime(system, sink)
    resumed.tick()
    assert len(sink.calls) == 2 and sink.calls[0] == sink.calls[1]
    assert len(sink.ledger) == 1
    assert resumed.store.get("team", pending["delivery_id"])["state"] == "delivered"


def test_only_one_claim_and_backoff_then_manual_retry(system):
    cancelled(system)
    integration, sink = runtime(system)
    integration.store.project(sink)
    key = integration.store.due(sink)[0]["delivery_id"]
    with ThreadPoolExecutor(max_workers=8) as workers:
        claims = list(workers.map(lambda _: integration.store.claim("team", key), range(8)))
    claimed = next(row for row in claims if row is not None)
    assert sum(row is not None for row in claims) == 1
    integration.store.finish(claimed, error=IntegrationError("offline"))
    assert integration.store.get("team", key)["state"] == "retrying"
    assert not integration.store.due(sink)
    integration.retry("team", key)
    integration.tick()
    assert integration.store.get("team", key)["state"] == "delivered"


def test_poison_preparation_is_visible_and_retry_prepares_only_unsent_payload(system):
    cancelled(system)
    sink = Sink()
    sink.prepare_error = IntegrationError("recipient_unmapped", retryable=False)
    integration, _ = runtime(system, sink)
    integration.tick()
    row = integration.store.list("team", ("alpha",))[0]
    assert row["state"] == "failed" and row["attempts"] == 0
    assert not sink.calls
    sink.prepare_error = None
    integration.retry("team", row["delivery_id"])
    integration.tick()
    assert len(sink.calls) == 1


def test_im_window_blocks_replay_after_remote_dedup_may_have_expired(system):
    cancelled(system)
    sink = Sink()
    sink.kind, sink.error = "im", IntegrationError("offline")
    integration, _ = runtime(system, sink)
    integration.tick()
    row = integration.store.list("team", ("alpha",))[0]
    system.now[0] += IM_RETRY_WINDOW
    with pytest.raises(HubStoreError, match="window expired"):
        integration.retry("team", row["delivery_id"])
    integration.tick()
    assert len(sink.calls) == 1
    assert integration.store.get("team", row["delivery_id"])["error_code"] == "idempotency_window_elapsed"


def test_changed_sink_identity_does_not_redirect_pending_delivery(system):
    cancelled(system)
    integration, sink = runtime(system)
    integration.store.project(sink)
    changed = Sink()
    changed.fingerprint = "new-endpoint"
    with pytest.raises(ValueError, match="target changed"):
        runtime(system, changed)
    assert not changed.calls


def test_tenant_queue_cap_keeps_event_cursor_for_later_replay(system, monkeypatch):
    import pbatch.hub_integration_store as module
    cancelled(system)
    integration, sink = runtime(system)
    monkeypatch.setattr(module, "MAX_PENDING", 0)
    integration.tick()
    assert not sink.calls
    monkeypatch.setattr(module, "MAX_PENDING", 1000)
    integration.tick()
    assert len(sink.calls) == 1


def test_delivery_routes_require_scope_and_instance_access(system):
    cancelled(system)
    sink = Sink()
    sink.error = IntegrationError("unavailable")
    integration, _ = runtime(system, sink)
    integration.tick()
    service = HubService(system.hub, integrations=integration)
    path = PREFIX + "/integrations/deliveries"
    key = service.dispatch("GET", path, {}, {}, system.user)[1]["data"][0]["delivery_id"]
    outsider = replace(system.user, tenant_id="other")
    assert service.dispatch("GET", path, {}, {}, outsider)[1]["data"] == []
    wrong = replace(system.user, instances=frozenset({"beta"}))
    assert service.dispatch("GET", path, {}, {}, wrong)[1]["data"] == []
    with pytest.raises(HubAccessError):
        service.dispatch("POST", path + "/" + key + "/retry", {}, {}, wrong)
    reader = replace(system.user, scopes=frozenset({"agent.sessions:read"}))
    with pytest.raises(HubAccessError):
        service.dispatch("GET", path, {}, {}, reader)
    retry_only = replace(system.user, scopes=frozenset({"agent.integrations:retry"}))
    _, body = service.dispatch("POST", path + "/" + key + "/retry", {}, {}, retry_only)
    assert set(body["data"]) == {"delivery_id", "state", "attempts"}


def test_slow_audit_does_not_block_im_or_session_api(system):
    cancelled(system)
    audit, im = Sink(), Sink()
    audit.block = threading.Event()
    im.name, im.kind = "im", "im"
    integration = HubIntegrations(system.hub, configured=[(audit, 0), (im, 0)])
    integration.start()
    try:
        assert audit.entered.wait(2)
        assert im.entered.wait(2)
        service = HubService(system.hub, integrations=integration)
        assert service.dispatch("GET", PREFIX + "/sessions", {}, {}, system.user)[0] == 200
    finally:
        audit.block.set()
        integration.close()


def test_monotonic_pagination_includes_later_deliveries_with_smaller_uuid(system):
    cancelled(system)
    integration, sink = runtime(system)
    integration.tick()
    first = integration.store.list("team", ("alpha",), limit=1)[0]
    cancelled(system, key="later")
    integration.tick()
    following = integration.store.list("team", ("alpha",), after=first["sequence"])
    assert len(following) == 1
    assert following[0]["sequence"] > first["sequence"]
    assert following[0]["delivery_id"] != first["delivery_id"]
    service = HubService(system.hub, integrations=integration)
    _, result = service.dispatch("GET", PREFIX + "/integrations/deliveries",
                                 {"after": str(first["sequence"])}, {}, system.user)
    assert result["next_cursor"] == following[0]["sequence"]


def test_permanent_failures_do_not_exhaust_active_delivery_capacity(system, monkeypatch):
    import pbatch.hub_integration_store as module
    monkeypatch.setattr(module, "MAX_PENDING", 1)
    cancelled(system)
    sink = Sink()
    sink.error = IntegrationError("rejected", retryable=False)
    integration, _ = runtime(system, sink)
    integration.tick()
    assert integration.store.list("team", None)[0]["state"] == "failed"
    cancelled(system, key="new-work")
    sink.error = None
    integration.tick()
    assert [row["state"] for row in integration.store.list("team", None)] == ["failed", "delivered"]


def test_stalled_database_ack_recovers_and_stale_attempt_cannot_overwrite(system):
    cancelled(system)
    integration, sink = runtime(system)
    integration.store.project(sink)
    key = integration.store.due(sink)[0]["delivery_id"]
    original = integration.store.claim("team", key)
    sink.deliver(original["payload"], key)
    system.now[0] += 91
    integration.tick()
    assert len(sink.calls) == 2 and len(sink.ledger) == 1
    integration.store.finish(original, error=IntegrationError("late-failure", retryable=False))
    assert integration.store.get("team", key)["state"] == "delivered"


def test_repeated_crashes_cannot_bypass_attempt_budget(system):
    cancelled(system)
    integration, sink = runtime(system)
    integration.store.project(sink)
    key = integration.store.due(sink)[0]["delivery_id"]
    for _ in range(12):
        assert integration.store.claim("team", key) is not None
        system.now[0] += 91
        integration.store.recover_stalled()
    assert integration.store.claim("team", key) is None
    assert integration.store.get("team", key)["error_code"] == "retry_budget_exhausted"
    integration.retry("team", key)
    integration.tick()
    assert integration.store.get("team", key)["attempts"] == 13
    assert integration.store.get("team", key)["state"] == "delivered"
