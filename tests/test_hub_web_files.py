"""Browser file read opt-in is independent of old task-control permission."""

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch.hub_web import HubWeb, CONTROL_SCOPES, FILE_READ_SCOPE
from test_hub_auth import signed
from test_hub_web import web_env, logged_in
from test_hub_web_control import control, authenticated


@pytest.mark.parametrize("value", [None, 1, "true", [], {}])
def test_file_option_requires_boolean(web_env, value):
    _, auth, transport, settings, _ = web_env
    settings["web"].update(control=True, file_snapshots=value)
    with pytest.raises(ValueError, match="file_snapshots"):
        HubWeb(settings, auth, transport=transport)


def test_file_option_requires_task_control(web_env):
    _, auth, transport, settings, _ = web_env
    settings["web"]["file_snapshots"] = True
    with pytest.raises(ValueError, match="requires web.control"):
        HubWeb(settings, auth, transport=transport)


def test_explicit_file_option_requests_only_additional_scope_and_tracks_revocation(web_env, signed):
    old, auth, transport, settings, _ = web_env
    old.close()
    settings["web"].update(control=True, file_snapshots=True)
    scopes = set(CONTROL_SCOPES) | {FILE_READ_SCOPE}
    auth.grants = [replace(grant, scopes=frozenset(scopes)) for grant in auth.grants]
    transport.token = lambda: signed[1](scope=" ".join(sorted(scopes)))
    web = HubWeb(settings, auth, transport=transport)
    try:
        _, url = web.begin("")
        assert set(parse_qs(urlsplit(url).query)["scope"][0].split()) == scopes | {"openid"}
        key = logged_in(web)
        assert web.access(key)["file_snapshots"] is True
        auth.grants = [replace(grant, scopes=frozenset(CONTROL_SCOPES)) for grant in auth.grants]
        access = web.access(key)
        assert access["task_access"] is True and access["file_snapshots"] is False
        assert FILE_READ_SCOPE not in access["scopes"]
    finally:
        web.close()


def test_existing_control_configuration_cannot_read_files(control):
    call, *_ = control
    cookie, access = authenticated(call)
    assert "file_snapshots" not in access and FILE_READ_SCOPE not in access["scopes"]
    path = "/web/api/sessions/unknown/turns/unknown/file-snapshots"
    status, _, raw = call(path, cookie=cookie)
    assert status == 403 and json.loads(raw)["error"]["code"] == "web_files_disabled"


def enabled_files(control, signed):
    from pbatch.hub_file_snapshot_store import FileSnapshotStore
    from test_hub_file_snapshot_models import upload
    from test_hub_file_snapshot_store import terminal
    call, server, store, generations, transport = control
    scopes = set(CONTROL_SCOPES) | {FILE_READ_SCOPE}
    server.web.file_snapshots = True
    server.auth.grants = [replace(grant, scopes=frozenset(scopes)) for grant in server.auth.grants]
    transport.token = lambda: signed[1](scope=" ".join(sorted(scopes)))
    cookie, _ = authenticated(call)
    session = store.upsert_session("team", "alpha", generations["alpha"], {
        "local_session_id": "files", "controllable": True, "status": "idle"})
    turn = store.submit_turn("team", session["session_id"], "alice", "inspect", "files-turn")
    body = upload(session_id=session["session_id"], turn_id=turn["turn_id"])
    terminal(store, body)
    FileSnapshotStore(store).put("team", body)
    path = f'/web/api/sessions/{body["session_id"]}/turns/{body["turn_id"]}/file-snapshots'
    return cookie, path, body


def test_cookie_file_reads_and_current_scope_revocation(control, signed):
    call, server, *_ = control
    cookie, path, body = enabled_files(control, signed)
    status, headers, raw = call(path, cookie=cookie)
    assert status == 200 and "no-store" in headers["Cache-Control"]
    assert json.loads(raw)["data"]["files"][0]["sha256"] == body["files"][0]["sha256"]
    status, _, raw = call(path + "/" + "a" * 32, cookie=cookie)
    assert status == 200 and json.loads(raw)["data"]["content_b64"] == body["files"][0]["content_b64"]
    server.auth.grants = [replace(grant, scopes=grant.scopes - {FILE_READ_SCOPE}) for grant in server.auth.grants]
    assert call(path, cookie=cookie)[0] == 403
    assert call(path + "/" + "a" * 32, cookie=cookie)[0] == 403


@pytest.mark.parametrize("headers", [{"Range": "bytes=0-3"}, {"If-None-Match": "etag"},
                                      {"Idempotency-Key": "unused"}, {"Content-Length": "1"}])
def test_cookie_file_reads_reject_body_range_and_mutation_headers(control, signed, headers):
    call, *_ = control
    cookie, path, _ = enabled_files(control, signed)
    assert call(path, cookie=cookie, headers=headers)[0] == 400


@pytest.mark.parametrize("suffix", ["?", "?path=report.txt", "?url=https://evil.test", "/report.txt", "/..", "/%2fetc"])
def test_cookie_file_reads_accept_only_opaque_ids(control, signed, suffix):
    call, *_ = control
    cookie, path, _ = enabled_files(control, signed)
    assert call(path + suffix, cookie=cookie)[0] == 400
