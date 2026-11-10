"""Authorized server-side task discovery uses the durable latest turn metadata."""

import json
from dataclasses import replace

import pytest

from pbatch.hub_auth import HubPrincipal
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store_helpers import HubStoreError
from test_hub_store import _store


@pytest.fixture
def task_store(tmp_path):
    store = _store(tmp_path)
    try:
        yield store
    finally:
        store.close()


def principal(**changes):
    user = HubPrincipal("team", "user", "client", "user", frozenset({"alpha"}),
                        frozenset({"agent.instances:read", "agent.sessions:read"}))
    return replace(user, **changes)


def session(store, local, *, instance="alpha", tenant="team", name=None, status="idle"):
    generation = store.register_instance(tenant, instance, {"boot_id": instance})["generation"]
    return store.upsert_session(tenant, instance, generation, {
        "local_session_id": local, "name": name or local, "project_id": "Project catalog",
        "controllable": True, "status": status})


def turn(store, row, state, key, created=None):
    result = store.submit_turn(row["tenant"], row["session_id"], "private-actor",
                               "password=private-prompt", key)
    with store._write() as conn:
        conn.execute("UPDATE hub_turns SET status=?,created_at=?,updated_at=? WHERE turn_id=?",
                     (state, created or store.clock(), created or store.clock(), result["turn_id"]))
    return result["turn_id"]


def page(store, query=None, user=None, method="GET", body=None):
    return HubService(store).dispatch(method, PREFIX + "/task-sessions", query or {},
                                      body or {}, user or principal())


def test_task_page_preserves_metadata_and_adds_only_latest_summary(task_store):
    row = session(task_store, "new")
    assert page(task_store)[1] == {"data": [{**task_store.session("team", row["session_id"]),
                                            "latest_turn": None, "category": "idle"}],
                                  "next_cursor": ""}
    failed = turn(task_store, row, "failed", "first", 1001)
    actual = page(task_store, {"state": "attention"})[1]["data"][0]
    assert actual["status"] == "idle" and actual["category"] == "attention"
    assert actual["latest_turn"] == {"turn_id": failed, "state": "failed",
                                      "created_at": 1001.0, "updated_at": 1001.0}
    assert not any(value in json.dumps(actual) for value in ("private-actor", "private-prompt"))
    successful = turn(task_store, row, "completed", "second", 1002)
    assert page(task_store, {"state": "attention"})[1]["data"] == []
    done = page(task_store, {"state": "completed"})[1]["data"]
    assert len(done) == 1 and done[0]["latest_turn"]["turn_id"] == successful
    assert done[0]["status"] == "idle" and done[0]["category"] == "completed"


@pytest.mark.parametrize("state", ["failed", "cancelled", "interrupted", "lost"])
def test_latest_terminal_failure_is_attention_but_active_turn_wins(task_store, state):
    row = session(task_store, "work")
    turn(task_store, row, state, "old", 2000)
    assert page(task_store, {"state": "attention"})[1]["data"][0]["category"] == "attention"
    running = turn(task_store, row, "running", "new", 1000)
    active = page(task_store, {"state": "active"})[1]["data"][0]
    assert active["active_turn_id"] == running and active["status"] == "running"
    assert active["latest_turn"]["state"] == state
    assert active["category"] == "active"
    assert page(task_store, {"state": "attention"})[1]["data"] == []


@pytest.mark.parametrize("status,category", [
    ("closing", "active"), ("closed", "closed"), ("failed", "attention"),
    ("close_failed", "attention"), ("close_lost", "attention"), ("uncertain", "attention"),
    ("active", "idle"), ("idle", "idle")])
def test_session_state_category_does_not_change_session_availability(task_store, status, category):
    row = session(task_store, "work", status=status)
    assert page(task_store, {"state": category})[1]["data"][0]["category"] == category
    assert page(task_store)[1]["data"][0]["status"] == status
    with task_store._write() as conn:
        conn.execute("UPDATE hub_instances SET last_seen=0")
    offline = page(task_store)[1]["data"][0]
    assert offline["online"] is False and offline["category"] == category


def test_latest_turn_ties_use_turn_id_and_database_page_is_stable(task_store):
    row = session(task_store, "tied")
    ids = [turn(task_store, row, "completed", str(i)) for i in range(4)]
    assert page(task_store)[1]["data"][0]["latest_turn"]["turn_id"] == max(ids)
    for i in range(113):
        session(task_store, f"task-{i:03}")
    expected = sorted(item["session_id"] for item in task_store.sessions("team", limit=200))
    seen, cursor = [], ""
    for _ in range(6):
        result = page(task_store, {"limit": "20", "after": cursor})[1]
        assert len(result["data"]) <= 20
        seen.extend(item["session_id"] for item in result["data"])
        cursor = result["next_cursor"]
    assert seen == expected and cursor == ""


def test_search_filters_all_authorized_rows_before_limit_and_is_literal(task_store):
    for i in range(105):
        session(task_store, f"plain-{i:03}")
    from pbatch.hub_store_helpers import global_session_id
    last = max(row["session_id"] for row in task_store.sessions("team", limit=200))
    local = next(f"searched-{i}" for i in range(10000)
                 if global_session_id("team", "alpha", f"searched-{i}") > last)
    target = session(task_store, local, name="任务 100%_done Straße")
    assert target["session_id"] not in {row["session_id"] for row in task_store.sessions("team")}
    session(task_store, "forbidden", instance="beta", name=target["name"])
    session(task_store, "other-tenant", tenant="other", name=target["name"])
    for query in ("100%_done", "STRASSE", "任务", target["session_id"]):
        rows = page(task_store, {"q": query, "limit": "1"})[1]["data"]
        assert len(rows) == 1 and rows[0]["session_id"] == target["session_id"]
    assert page(task_store, {"q": "%wildcard%"})[1]["data"] == []
    assert len(page(task_store, {"q": "PROJECT CATALOG", "limit": "50"})[1]["data"]) == 50


def test_cursor_bound_to_identity_authority_and_filters(task_store):
    for i in range(3):
        session(task_store, f"work-{i}")
    result = page(task_store, {"q": "work", "limit": "1"})[1]
    cursor = result["next_cursor"]
    assert cursor and cursor != result["data"][0]["session_id"]
    assert len(page(task_store, {"q": "work", "after": cursor, "limit": "50"})[1]["data"]) == 2
    for user in [principal(subject="other"), principal(client_id="other"),
                 principal(tenant_id="other"), principal(instances=frozenset({"alpha", "beta"}))]:
        with pytest.raises(HubAccessError) as error:
            page(task_store, {"q": "work", "after": cursor}, user)
        assert error.value.status == 403 and error.value.code == "task_cursor_authority_changed"
    for change in ({"state": "idle"}, {"q": "other"}, {"instance_id": "alpha"}):
        with pytest.raises(ValueError, match="cursor"):
            page(task_store, {"q": "work", "after": cursor, **change})
    for malformed in (cursor + "=", "x" * 2049, "not/a/cursor", "true"):
        with pytest.raises(ValueError, match="cursor"):
            page(task_store, {"q": "work", "after": malformed})


@pytest.mark.parametrize("query", [
    {"limit": "0"}, {"limit": "51"}, {"limit": True}, {"limit": "１"},
    {"limit": "1" * 1000}, {"state": "unknown"}, {"state": None}, {"before": "x"},
    {"q": "x" * 129}, {"q": "\n"}, {"q": None}, {"q": "\ud800"},
    {"after": False}, {"instance_id": " wrong "}, {"instance_id": 1},
])
def test_invalid_task_queries_fail_closed(task_store, query):
    with pytest.raises((ValueError, UnicodeError)):
        page(task_store, query)


def test_resource_scope_and_read_only_method_contract(task_store):
    session(task_store, "allowed")
    session(task_store, "other", instance="beta")
    assert len(page(task_store)[1]["data"]) == 1
    assert page(task_store, user=principal(instances=frozenset()))[1]["data"] == []
    for user in (principal(kind="gateway"), principal(scopes=frozenset())):
        with pytest.raises(HubAccessError):
            page(task_store, user=user)
    with pytest.raises(HubAccessError):
        page(task_store, {"instance_id": "beta"})
    with pytest.raises(HubAccessError) as error:
        page(task_store, method="POST")
    assert error.value.status == 405
    with pytest.raises(ValueError):
        page(task_store, body={"prompt": "do not execute"})
    assert task_store.commands("team", "alpha", 1) == []


def test_task_listing_preserves_legacy_session_list(task_store):
    row = session(task_store, "work")
    turn(task_store, row, "failed", "failed")
    legacy = HubService(task_store).dispatch("GET", PREFIX + "/sessions", {}, {}, principal())[1]
    assert "latest_turn" not in legacy["data"][0] and "category" not in legacy["data"][0]
    assert legacy["next_cursor"] == row["session_id"]
    assert page(task_store)[1]["data"][0]["category"] == "attention"


def test_query_deadline_rejects_late_results(task_store, monkeypatch):
    from pbatch import hub_task_sessions
    session(task_store, "work")
    ticks = iter([0.0, 0.0, 6.0, 6.0])
    monkeypatch.setattr(hub_task_sessions, "monotonic", lambda: next(ticks, 6.0))
    with pytest.raises(HubStoreError) as error:
        page(task_store)
    assert error.value.status == 503 and error.value.code == "task_query_budget"


def test_query_vm_budget_rejects_an_exhaustive_search(task_store, monkeypatch):
    from pbatch import hub_task_sessions
    session(task_store, "work")
    with task_store._write() as conn:
        conn.executemany("INSERT INTO hub_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            (f"sid-{i}", "team", "alpha", f"local-{i}", 1, f"name-{i}", "project", 1, "idle", 0, 1000)
            for i in range(300)])
    monkeypatch.setattr(hub_task_sessions, "QUERY_VM_STEPS", 1000)
    with pytest.raises(HubStoreError) as error:
        page(task_store, {"q": "not-present"})
    assert error.value.code == "task_query_budget"


def test_task_query_never_reads_turn_prompt_actor_or_idempotency(task_store, monkeypatch):
    import sqlite3
    row = session(task_store, "work")
    turn(task_store, row, "completed", "sensitive")
    original, reads = task_store._connect, []

    def connect():
        conn = original()
        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(task_store, "_connect", connect)
    assert page(task_store)[1]["data"][0]["category"] == "completed"
    assert not {("hub_turns", name) for name in ("prompt", "actor", "idempotency_key")} & set(reads)
