"""Live, bounded operation history over the original durable request ledgers."""

import base64
import json
import math

import pytest

from pbatch import hub_session_operations_store as history
from pbatch.hub_store import HubStore


@pytest.fixture
def store(tmp_path):
    value = HubStore(tmp_path / "hub.sqlite", clock=lambda: 1000.0)
    yield value
    value.close()


def seed(store, operation, request_id, at=1000.0, *, tenant="team", instance="alpha", status="queued"):
    common = {"request_id": request_id, "tenant": tenant, "actor": "private-actor",
              "client_id": "private-client", "idempotency_key": operation + request_id,
              "instance_id": instance, "generation": 1, "local_session_id": operation + request_id,
              "status": status, "error_code": "", "created_at": at, "updated_at": at}
    if operation == "create":
        common.update(name="Requested work", project_id="/private/project", session_id="")
    else:
        common["session_id"] = "session-" + request_id
    with store._write() as conn:
        conn.execute("INSERT INTO " + history.TABLES[operation] + " (" + ",".join(common) + ") "
                     "VALUES (" + ",".join("?" for _ in common) + ")", tuple(common.values()))


def identities(page):
    return [(row["operation"], row["request_id"]) for row in page["data"]]


def all_pages(store, *, limit=2, **filters):
    result, seen, before = [], set(), ""
    for _ in range(20):
        page = store.session_operations("team", before=before, limit=limit, **filters)
        result.extend(page["data"])
        cursor = page["next_cursor"]
        assert cursor not in seen
        if not cursor:
            return result
        seen.add(cursor)
        before = cursor
    pytest.fail("bounded fixture did not terminate pagination")


def test_mixed_equal_timestamp_rows_page_without_duplicates_or_missing_cross_table_identity(store):
    for operation in ("create", "close"):
        for key in ("a", "b", "c"):
            seed(store, operation, key)
    rows = all_pages(store, limit=1)
    assert [(row["operation"], row["request_id"]) for row in rows] == [
        (op, key) for op in ("create", "close") for key in ("c", "b", "a")]
    assert len({(row["operation"], row["request_id"]) for row in rows}) == 6
    for row in rows:
        assert set(row) == {"operation", *history.FIELDS}
        assert row["name"] == ("Requested work" if row["operation"] == "create" else "")
    assert "private" not in json.dumps(rows)


def test_tenant_and_grant_filtering_precedes_page_limit(store):
    for index in range(60):
        seed(store, "create", f"denied-{index}", at=2000 + index, instance="denied")
    seed(store, "create", "other-tenant", at=4000, tenant="other")
    seed(store, "create", "allowed-old", at=500)
    seed(store, "close", "allowed-new", at=1000)
    rows = all_pages(store, limit=1, allowed_instances=("alpha",))
    assert [row["request_id"] for row in rows] == ["allowed-new", "allowed-old"]
    assert store.session_operations("team", allowed_instances=()) == {"data": [], "next_cursor": ""}
    assert store.session_operations("team", "alpha", allowed_instances=("denied",))["data"] == []
    assert identities(store.session_operations("team", "alpha")) == [
        ("close", "allowed-new"), ("create", "allowed-old")]


def test_status_updates_never_move_rows_and_restart_retains_accepted_history(store):
    seed(store, "create", "newer", at=1000)
    seed(store, "close", "older", at=900)
    page = store.session_operations("team", limit=1)
    with store._write() as conn:
        conn.execute("UPDATE hub_session_close_requests SET status='closed',updated_at=9999")
    second = store.session_operations("team", before=page["next_cursor"], limit=1)
    assert identities(second) == [("close", "older")]
    assert second["data"][0]["status"] == "closed" and second["next_cursor"] == ""
    store.close()
    reopened = HubStore(store.path)
    try:
        assert reopened.session_operations("team")["data"][1]["status"] == "closed"
        assert identities(reopened.session_operations("team", before=page["next_cursor"])) == identities(second)
    finally:
        reopened.close()


def test_new_requests_and_clock_rollback_use_recorded_time_in_live_pages(store):
    seed(store, "create", "first", at=1000)
    seed(store, "close", "last", at=100)
    first = store.session_operations("team", limit=1)
    seed(store, "create", "normal-new", at=1100)
    seed(store, "close", "clock-rolled-back", at=500)
    older = store.session_operations("team", before=first["next_cursor"])
    assert [row["request_id"] for row in older["data"]] == ["clock-rolled-back", "last"]
    assert store.session_operations("team")["data"][0]["request_id"] == "normal-new"


def test_both_queries_share_one_read_transaction_even_when_a_writer_commits_between_them(store, monkeypatch):
    seed(store, "create", "first")
    seed(store, "close", "old")
    original, writes = history._branch, []

    def interleaved(conn, operation, *args):
        result = original(conn, operation, *args)
        if operation == "create" and not writes:
            writes.append(True)
            with store._write() as writer:
                writer.execute("UPDATE hub_session_close_requests SET status='closed'")
            seed(store, "close", "late", at=2000)
        return result

    monkeypatch.setattr(history, "_branch", interleaved)
    page = store.session_operations("team")
    assert identities(page) == [("create", "first"), ("close", "old")]
    assert page["data"][1]["status"] == "queued"
    latest = store.session_operations("team")
    assert latest["data"][0]["request_id"] == "late"
    assert latest["data"][-1]["status"] == "closed"


def test_branch_fetches_are_bounded_and_planned_against_history_indexes(store, monkeypatch):
    for index in range(60):
        for operation in history.TABLES:
            seed(store, operation, f"request-{index:03}", at=index)
    original, sizes, queries = history._branch, [], []

    def bounded(conn, operation, *args):
        conn.set_trace_callback(lambda query: queries.append(query) if query.startswith("SELECT") else None)
        result = original(conn, operation, *args)
        sizes.append(len(result))
        return result

    monkeypatch.setattr(history, "_branch", bounded)
    first = store.session_operations("team", limit=50)
    second = store.session_operations("team", "alpha", before=first["next_cursor"], limit=1)
    assert len(first["data"]) == 50 and len(second["data"]) == 1
    assert sizes[:2] == [51, 51] and max(sizes[2:]) <= 2
    with store._connect() as conn:
        for query in queries:
            plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            assert all("SCAN hub_session" not in row[3] for row in plan)
            assert any("_history" in row[3] for row in plan)


def test_additive_index_migration_keeps_old_rows_without_creating_another_state_table(store):
    seed(store, "create", "existing")
    with store._write() as conn:
        tables_before = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for operation in history.TABLES.values():
            conn.execute("DROP INDEX " + operation + "_history")
            conn.execute("DROP INDEX " + operation + "_instance_history")
    store.close()
    reopened = HubStore(store.path)
    try:
        assert identities(reopened.session_operations("team")) == [("create", "existing")]
        with reopened._connect() as conn:
            tables_after = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert tables_after == tables_before
        assert {table + suffix for table in history.TABLES.values()
                for suffix in ("_history", "_instance_history")} <= indexes
    finally:
        reopened.close()


def encoded(payload, *, compact=True):
    options = {"separators": (",", ":")} if compact else {}
    raw = json.dumps(payload, ensure_ascii=False, **options).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize("payload", [None, {}, [], [1], [1, "0x1p+0", "create", "id"],
                                    [True, "0x1.0000000000000p+0", "create", "id"],
                                    [2, "0x1.0000000000000p+0", "create", "id"],
                                    [1, "nan", "create", "id"], [1, "inf", "create", "id"],
                                    [1, "-0x1.0000000000000p+0", "create", "id"],
                                    [1, "0x1p+9999999", "create", "id"],
                                    [1, 10 ** 1000, "create", "id"],
                                    [1, "0x1.0000000000000p+0", "unknown", "id"],
                                    [1, "0x1.0000000000000p+0", [], "id"],
                                    [1, "0x1.0000000000000p+0", "close", "bad\n"],
                                    [1, "0x1.0000000000000p+0", "close", "bad\x7f"],
                                    [1, "0x1.0000000000000p+0", "close", "x" * 257]])
def test_cursor_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        history.decode_cursor(encoded(payload))


@pytest.mark.parametrize("cursor", ["a", "not+base64", "abc=", "x" * 2049, "汉", None, 1])
def test_cursor_rejects_noncanonical_or_unbounded_encoding(cursor):
    with pytest.raises(ValueError):
        history.decode_cursor(cursor)


def test_cursor_preserves_exact_float_order_unicode_and_existing_c1_identifiers(store):
    moment = math.nextafter(1000.0, math.inf)
    key = "会话\x80" + "界" * 100
    seed(store, "create", key, at=moment)
    seed(store, "close", key, at=1000.0)
    first = store.session_operations("team", limit=1)
    assert history.decode_cursor(first["next_cursor"]) == (moment, "create", key)
    second = store.session_operations("team", before=first["next_cursor"], limit=1)
    assert identities(second) == [("close", key)] and not second["next_cursor"]
    payload = [1, moment.hex(), "create", key]
    with pytest.raises(ValueError):
        history.decode_cursor(encoded(payload, compact=False))


@pytest.mark.parametrize("limit", [0, 51, True, 1.5, "20"])
def test_store_rejects_invalid_page_limits(store, limit):
    with pytest.raises(ValueError):
        store.session_operations("team", limit=limit)
