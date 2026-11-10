"""Durable, bounded turn history and authorized per-turn event windows."""

import json

import pytest

from pbatch.hub_store import HubStore, HubStoreError
from test_hub_auth import signed
from test_hub_http import hub, publish, send, event
from test_hub_store import _store, _session
from test_hub_web import web_env
from test_hub_web_control import control, authenticated, create_session
from test_hub_web_read_grants import readers


def completed(store, session, key, prompt="Inspect the project", tenant="tenant-a"):
    turn = store.submit_turn(tenant, session, "user", prompt, key)
    for kind in ("turn.running", "turn.completed"):
        store.append_event(tenant, "worker-a", 1, {
            "session_id": session, "turn_id": turn["turn_id"], "kind": kind,
            "event_id": key + kind, "payload": {}})
    return turn


def test_turn_history_is_latest_first_paged_redacted_and_durable(tmp_path):
    store = _store(tmp_path)
    try:
        _, session = _session(store)
        sid = session["session_id"]
        turns = [completed(store, sid, f"key-{index}", "password=never-publish plan") for index in range(5)]
        expected = sorted((row["turn_id"] for row in turns), reverse=True)
        seen, cursor = [], ""
        for _ in range(3):
            page = store.turn_history("tenant-a", sid, limit=2, before=cursor)
            seen.extend(row["turn_id"] for row in page["data"])
            for row in page["data"]:
                assert row["state"] == "completed" and row["prompt_truncated"] is False
                assert row["prompt"] == "password=[REDACTED] plan"
                assert not {"actor", "idempotency_key", "tenant"} & set(row)
            cursor = page["next_cursor"]
        assert seen == expected and cursor == ""
        path = store.path
    finally:
        store.close()
    reopened = HubStore(path)
    try:
        assert [row["turn_id"] for row in reopened.turn_history("tenant-a", sid)["data"]] == expected
    finally:
        reopened.close()


def test_turn_history_cursors_are_session_bound_and_input_is_bounded(tmp_path):
    store = _store(tmp_path)
    try:
        _, first = _session(store)
        _, other = _session(store, local="other")
        for index in range(3):
            completed(store, first["session_id"], f"cursor-{index}")
        cursor = store.turn_history("tenant-a", first["session_id"], limit=1)["next_cursor"]
        with pytest.raises(ValueError, match="cursor"):
            store.turn_history("tenant-a", other["session_id"], before=cursor)
        with pytest.raises(HubStoreError) as captured:
            store.turn_history("other-tenant", first["session_id"])
        assert captured.value.status == 404
        for value in (True, 0, 51, "1"):
            with pytest.raises(ValueError):
                store.turn_history("tenant-a", first["session_id"], limit=value)
        for value in ("x", "x" * 2049, "../bad", cursor + "="):
            with pytest.raises(ValueError):
                store.turn_history("tenant-a", first["session_id"], before=value)
    finally:
        store.close()


def test_turn_history_redaction_expansion_is_utf8_bounded_and_marked(tmp_path):
    store = _store(tmp_path)
    try:
        _, session = _session(store)
        completed(store, session["session_id"], "expanded", "token=x " * 2048)
        row = store.turn_history("tenant-a", session["session_id"])["data"][0]
        assert len(row["prompt"].encode()) <= 16384 and row["prompt_truncated"] is True
        assert "token=x" not in row["prompt"]
    finally:
        store.close()


def test_turn_history_rejects_corrupt_stored_prompt_instead_of_reading_unbounded_text(tmp_path):
    store = _store(tmp_path)
    try:
        _, session = _session(store)
        turn = completed(store, session["session_id"], "corrupt")
        with store._write() as conn:
            conn.execute("UPDATE hub_turns SET prompt=? WHERE turn_id=?", ("x" * 20000, turn["turn_id"]))
        with pytest.raises(ValueError, match="prompt"):
            store.turn_history("tenant-a", session["session_id"])
    finally:
        store.close()


def test_event_turn_filter_is_bound_to_session_and_preserves_cursors(tmp_path):
    store = _store(tmp_path)
    try:
        _, session = _session(store)
        first = completed(store, session["session_id"], "first")
        second = completed(store, session["session_id"], "second")
        selected = store.events("tenant-a", session["session_id"], limit=2, turn_id=second["turn_id"])
        assert len(selected) == 2 and all(row["turn_id"] == second["turn_id"] for row in selected)
        rest = store.events("tenant-a", session["session_id"], after=selected[-1]["cursor"], turn_id=second["turn_id"])
        assert [row["kind"] for row in rest] == ["turn.completed"]
        assert len(store.events("tenant-a", session["session_id"])) == 6
        for invalid in (None, False, 0, []):
            with pytest.raises(ValueError):
                store.events("tenant-a", session["session_id"], turn_id=invalid)
        _, other = _session(store, local="other")
        with pytest.raises(HubStoreError) as captured:
            store.events("tenant-a", other["session_id"], turn_id=first["turn_id"])
        assert captured.value.status == 404
    finally:
        store.close()


def test_bearer_history_and_filtered_events_reuse_resource_authorization(hub):
    call, store, _ = hub
    session, generation = publish(call)
    turn = send(call, session, prompt="token=private workflow")[1]["data"]["turn_id"]
    assert event(call, session, turn, generation, kind="turn.running", event_id="started")[0] == 200
    path = f"/sessions/{session}/turns"
    status, page, _ = call(path, token="alpha-reader")
    assert status == 200 and page["data"][0]["prompt"] == "token=[REDACTED] workflow"
    assert page["data"][0]["state"] == "running"
    for token in ("outsider", "alpha"):
        assert call(path, token=token)[0] in {403, 404}
    other, _ = publish(call, instance="beta")
    assert call(f"/sessions/{other}/turns", token="alpha-reader")[0] == 403
    assert call(f"/sessions/{other}/events?turn_id={turn}")[0] == 404
    status, filtered, _ = call(f"/sessions/{session}/events?turn_id={turn}&limit=1")
    assert status == 200 and len(filtered["data"]) == 1
    assert filtered["next_cursor"] == filtered["data"][0]["cursor"]
    assert len(store.commands("team", "alpha", generation)) == 0
    assert call(path, body={})[0] == 400
    assert call(f"/sessions/{session}/events?turn_id=")[0] == 400


@pytest.mark.parametrize("suffix", ["?limit=0", "?limit=51", "?limit=true", "?before=bad", "?after=0", "?unknown=1"])
def test_history_http_rejects_invalid_queries(hub, suffix):
    call, *_ = hub
    session, _ = publish(call)
    assert call(f"/sessions/{session}/turns{suffix}")[0] == 400


def test_browser_history_requires_exact_task_grant_and_never_widens_cookie_authority(control, signed):
    call, server, store, *_ = control
    cookie, access = authenticated(call)
    session = create_session(control, cookie, access)
    turn = store.submit_turn("team", session, "synthetic-user", "password=hidden inspect", "history-turn")
    path = f"/web/api/sessions/{session}/turns"
    status, _, raw = call(path + "?limit=20", cookie=cookie)
    assert status == 200 and json.loads(raw)["data"][0]["prompt"] == "password=[REDACTED] inspect"
    status, _, raw = call(f"/web/api/sessions/{session}/events?turn_id={turn['turn_id']}&after=0", cookie=cookie)
    assert status == 200 and len(json.loads(raw)["data"]) == 1
    assert call(path + "?before=bad", cookie=cookie)[0] == 400
    assert call(path + "?limit=51", cookie=cookie)[0] == 400
    assert call(path + "?limit=21", cookie=cookie)[0] == 400
    assert call(path, cookie=cookie, body=b"{}")[0] == 400
    server.auth = server.web.auth = readers(signed[2])
    assert call(path, cookie=cookie)[0] == 403
    assert call(f"/web/api/sessions/{session}/events?turn_id={turn['turn_id']}", cookie=cookie)[0] == 403


def test_browser_history_page_stays_below_response_budget_with_maximum_json_escaping(control):
    call, _, store, *_ = control
    cookie, access = authenticated(call)
    session = create_session(control, cookie, access)
    for index in range(21):
        turn = store.submit_turn("team", session, "synthetic-user", "\x00" * 16384, f"escaped-{index}")
        store.cancel_turn("team", turn["turn_id"], "synthetic-user")
    status, _, raw = call(f"/web/api/sessions/{session}/turns?limit=20", cookie=cookie)
    assert status == 200 and len(raw) < 2 * 1024 * 1024
    payload = json.loads(raw)
    assert len(payload["data"]) == 20 and payload["next_cursor"]
    assert all(row["prompt"] == "\x00" * 16384 for row in payload["data"])


@pytest.mark.parametrize("path", ["/web/api/sessions/turns", "/web/api/turns/turns"])
def test_history_query_fields_apply_only_to_the_history_route(control, path):
    call, *_ = control
    cookie, _ = authenticated(call)
    assert call(path + "?limit=1", cookie=cookie)[0] == 400
