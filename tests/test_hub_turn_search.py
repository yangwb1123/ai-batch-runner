"""Historical search only matches the same bounded, redacted text it returns."""

from dataclasses import replace

import pytest

from pbatch.hub_auth import HubPrincipal
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store_helpers import HubStoreError
from test_hub_store import _store, _session


@pytest.fixture
def history_store(tmp_path):
    store = _store(tmp_path)
    try:
        _, session = _session(store)
        yield store, session["session_id"]
    finally:
        store.close()


def add_turn(store, session, prompt, key, *, state="completed", stamp=1000):
    turn = store.submit_turn("tenant-a", session, "user", prompt, key)
    with store._write() as conn:
        conn.execute("UPDATE hub_turns SET status=?,created_at=?,updated_at=? WHERE turn_id=?",
                     (state, stamp, stamp, turn["turn_id"]))
    return turn["turn_id"]


def principal(**changes):
    user = HubPrincipal("tenant-a", "user", "client", "user", frozenset({"worker-a"}),
                        frozenset({"agent.sessions:read"}))
    return replace(user, **changes)


def page(store, session, query=None, user=None):
    return HubService(store).dispatch("GET", PREFIX + f"/sessions/{session}/turns",
                                      query or {}, {}, user or principal())[1]


def test_search_reads_beyond_initial_page_and_is_literal_casefolded(history_store):
    store, sid = history_store
    target = add_turn(store, sid, "任务 Straße 100%_done", "target", stamp=900)
    for index in range(60):
        add_turn(store, sid, "ordinary", f"ordinary-{index}")
    assert target not in {row["turn_id"] for row in store.turn_history("tenant-a", sid)["data"]}
    for query in ("STRASSE", "任务", "100%_done", target.upper()):
        result = page(store, sid, {"q": query, "limit": "1"})
        assert [row["turn_id"] for row in result["data"]] == [target]
        assert result["next_cursor"] == ""
    assert page(store, sid, {"q": "%wildcard%"})["data"] == []


def test_search_cannot_oracle_raw_secrets_or_text_past_visible_truncation(history_store):
    store, sid = history_store
    hidden = add_turn(store, sid, "password=private-oracle-value visible", "secret")
    expanded = add_turn(store, sid, "token=x " * 2000 + "invisible_tail", "expanded")
    for query in ("private-oracle", "oracle-value", "password=private", "token=x", "invisible_tail"):
        assert page(store, sid, {"q": query}) == {"data": [], "next_cursor": ""}
    result = page(store, sid, {"q": "[redacted]"})
    assert {row["turn_id"] for row in result["data"]} == {hidden, expanded}
    truncated = next(row for row in result["data"] if row["turn_id"] == expanded)
    assert truncated["prompt_truncated"] is True
    assert "invisible_tail" not in truncated["prompt"]
    assert len(truncated["prompt"].encode()) <= 16384


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested", "completed", "failed",
                                  "cancelled", "interrupted", "lost"])
def test_exact_turn_state_filter_composes_with_visible_search(history_store, state):
    store, sid = history_store
    add_turn(store, sid, "ordinary", "ordinary")
    target = add_turn(store, sid, "selected prompt", "selected", state=state)
    result = page(store, sid, {"state": state, "q": "selected"})
    assert [row["turn_id"] for row in result["data"]] == [target]
    assert result["data"][0]["state"] == state
    assert page(store, sid, {"state": state, "q": "missing"})["data"] == []


def test_filtered_cursor_is_stable_newest_first_and_binds_filters(history_store):
    store, sid = history_store
    ids = [add_turn(store, sid, "Straße", f"turn-{index}", stamp=900 + index // 3) for index in range(7)]
    expected = sorted(ids[:3], reverse=True)
    expected = [ids[-1], *sorted(ids[3:6], reverse=True), *expected]
    first = page(store, sid, {"q": "STRASSE", "state": "completed", "limit": "2"})
    seen, cursor = [row["turn_id"] for row in first["data"]], first["next_cursor"]
    add_turn(store, sid, "Straße newly arrived", "new", stamp=1001)
    while cursor:
        result = page(store, sid, {"q": "straße", "state": "completed", "limit": "3", "before": cursor})
        seen.extend(row["turn_id"] for row in result["data"])
        cursor = result["next_cursor"]
    assert seen == expected
    for change in ({"q": "other"}, {"state": "all"}):
        with pytest.raises(ValueError, match="cursor"):
            page(store, sid, {"q": "STRASSE", "state": "completed", "before": first["next_cursor"], **change})


def test_cursor_binds_principal_identity_instances_and_session(history_store):
    store, sid = history_store
    for index in range(3):
        add_turn(store, sid, "visible", f"turn-{index}")
    cursor = page(store, sid, {"limit": "1"})["next_cursor"]
    for user in (principal(subject="other"), principal(client_id="other"),
                 principal(instances=frozenset({"worker-a", "worker-b"}))):
        with pytest.raises((ValueError, HubAccessError)):
            page(store, sid, {"before": cursor}, user)
    _, other = _session(store, local="other")
    with pytest.raises(ValueError, match="cursor"):
        page(store, other["session_id"], {"before": cursor})
    for user in (principal(tenant_id="other"), principal(instances=frozenset({"worker-b"})),
                 principal(scopes=frozenset()), principal(kind="gateway")):
        with pytest.raises((HubAccessError, HubStoreError)):
            page(store, sid, {"q": "visible"}, user)


def test_exact_turn_lookup_is_session_bound_redacted_and_not_paginated(history_store):
    store, sid = history_store
    target = add_turn(store, sid, "token=private selected", "target", stamp=900)
    for index in range(22):
        add_turn(store, sid, "ordinary", f"ordinary-{index}")
    result = page(store, sid, {"turn_id": target})
    assert len(result["data"]) == 1 and result["data"][0]["turn_id"] == target
    assert result["data"][0]["prompt"] == "token=[REDACTED] selected"
    assert result["next_cursor"] == ""
    assert page(store, sid, {"turn_id": "not-found"}) == {"data": [], "next_cursor": ""}
    _, other = _session(store, local="other")
    assert page(store, other["session_id"], {"turn_id": target}) == {"data": [], "next_cursor": ""}
    assert page(store, sid, {"turn_id": target, "q": "", "before": "", "state": "all", "limit": "50"}) == result


@pytest.mark.parametrize("query", [
    {"q": None}, {"q": True}, {"q": "x" * 129}, {"q": "🙂" * 129}, {"q": "\n"}, {"q": "\x7f"},
    {"q": "\ud800"}, {"q": "\x85"}, {"q": "\u202e"}, {"q": "\u2066"},
    {"state": None}, {"state": "active"}, {"state": "COMPLETED"},
    {"turn_id": ""}, {"turn_id": None}, {"turn_id": " wrong "}, {"turn_id": "a" * 257},
    {"turn_id": "selected", "q": "find"}, {"turn_id": "selected", "state": "failed"},
    {"turn_id": "selected", "before": "cursor"}, {"limit": "１"}, {"before": None},
])
def test_query_inputs_and_exact_lookup_conflicts_fail_closed(history_store, query):
    store, sid = history_store
    with pytest.raises((ValueError, UnicodeError)):
        page(store, sid, query)


def test_legacy_store_unfiltered_cursor_and_keyword_interface_remain_compatible(history_store):
    from pbatch.hub_turn_history import _binding, _encode
    store, sid = history_store
    ids = [add_turn(store, sid, "visible", f"turn-{index}") for index in range(3)]
    ordered = sorted(ids, reverse=True)
    cursor = _encode(_binding("tenant-a", sid), 1000.0, ordered[0])
    assert store.turn_history("tenant-a", sid, limit=1)["next_cursor"] == cursor
    result = store.turn_history("tenant-a", sid, before=cursor)
    assert [row["turn_id"] for row in result["data"]] == ordered[1:]
    assert page(store, sid, {"before": cursor})["data"] == result["data"]
    for query in ({"q": "visible"}, {"state": "completed"}):
        with pytest.raises(ValueError, match="cursor"):
            page(store, sid, {"before": cursor, **query})


def test_cursor_authority_changes_are_403_and_filter_changes_are_400(history_store):
    store, sid = history_store
    for index in range(3):
        add_turn(store, sid, "visible", f"turn-{index}")
    cursor = page(store, sid, {"q": "visible", "limit": "1"})["next_cursor"]
    with pytest.raises(HubAccessError) as error:
        page(store, sid, {"q": "visible", "before": cursor}, principal(subject="other"))
    assert error.value.status == 403 and error.value.code == "turn_cursor_authority_changed"
    with pytest.raises(ValueError, match="cursor"):
        page(store, sid, {"q": "changed", "before": cursor})


@pytest.mark.parametrize("mode", ["rows", "bytes", "vm", "deadline", "projection_deadline"])
def test_all_query_budgets_fail_closed_without_partial_success(history_store, monkeypatch, mode):
    from pbatch import hub_turn_query, hub_turn_history
    store, sid = history_store
    for index in range(8):
        add_turn(store, sid, "bounded prompt", f"turn-{index}")
    if mode == "rows":
        monkeypatch.setattr(hub_turn_query, "QUERY_SCAN_ROWS", 2)
    elif mode == "bytes":
        monkeypatch.setattr(hub_turn_query, "QUERY_SCAN_BYTES", 20)
    elif mode == "vm":
        monkeypatch.setattr(hub_turn_query, "QUERY_VM_STEPS", 1)
        monkeypatch.setattr(hub_turn_query, "_PROGRESS_STEPS", 1)
    elif mode == "deadline":
        ticks = iter([0.0, 0.0, 6.0])
        monkeypatch.setattr(hub_turn_query, "monotonic", lambda: next(ticks, 6.0))
    else:
        clock, original = [0.0], hub_turn_history._record
        monkeypatch.setattr(hub_turn_query, "monotonic", lambda: clock[0])
        def slow_record(row):
            result = original(row)
            clock[0] = 6.0
            return result
        monkeypatch.setattr(hub_turn_history, "_record", slow_record)
    with pytest.raises(HubStoreError) as error:
        page(store, sid, {"q": "absent"})
    assert error.value.status == 503 and error.value.code == "turn_query_budget"


def test_corrupt_prompt_in_search_or_exact_read_cannot_hide_as_no_match(history_store):
    store, sid = history_store
    target = add_turn(store, sid, "ordinary", "target")
    with store._write() as conn:
        conn.execute("UPDATE hub_turns SET prompt=? WHERE turn_id=?", ("x" * 20000, target))
    for query in ({"q": "not-present"}, {"turn_id": target}, {}):
        with pytest.raises(ValueError, match="prompt"):
            page(store, sid, query)


def test_successful_and_exhausted_searches_never_write_or_leak_raw_query_to_sql(history_store, monkeypatch):
    store, sid = history_store
    add_turn(store, sid, "password=private-oracle-value visible", "target")
    original, statements = store._connect, []
    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(store, "_connect", connect)
    for query in ("visible", "private-oracle-value"):
        page(store, sid, {"q": query})
    assert not any("private-oracle-value" in sql for sql in statements)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql in statements)
    assert any(sql == "PRAGMA query_only=ON" for sql in statements)


def test_filtered_scan_streams_in_history_order_without_materializing_prompt_sort(history_store):
    from pbatch.hub_turn_query import _query, filters
    store, sid = history_store
    for index in range(3):
        add_turn(store, sid, "visible", f"row-{index}", state="completed")
    sql, values = _query("tenant-a", sid, filters("visible", "completed", "", ""), None, 20)
    with store._connect() as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, values)]
    assert any("hub_turns_history" in step for step in plan)
    assert not any("TEMP B-TREE" in step for step in plan)
    assert len(page(store, sid, {"q": "visible", "state": "completed"})["data"]) == 3
