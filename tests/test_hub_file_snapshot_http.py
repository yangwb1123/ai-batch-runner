"""Real local HTTP framing, size limits and post-body authentication checks."""

from types import SimpleNamespace
import socket
import threading
import time

import pytest

from pbatch.hub_errors import HubAccessError
from pbatch.hub_file_snapshot_http import read_upload_bytes
from tests.test_hub_file_snapshot_models import file_row, upload
from tests.test_hub_http import hub, publish, send, event


def completed(call):
    sid, generation = publish(call)
    turn = send(call, sid)[1]["data"]["turn_id"]
    for state in ("running", "completed"):
        assert event(call, sid, turn, generation, kind="turn." + state, event_id=state)[0] == 200
    return upload(session_id=sid, turn_id=turn, generation=generation)


def test_http_large_upload_then_bounded_opaque_content_download(hub):
    call, _store, _clock = hub
    body = completed(call)
    body["files"] = [file_row(b"x" * 65536)]
    assert call("/gateway/file-snapshots", method="POST", token="alpha", body=body)[0] == 200
    route = f'/sessions/{body["session_id"]}/turns/{body["turn_id"]}/file-snapshots'
    status, manifest, headers = call(route)
    assert status == 200 and manifest["data"]["files"][0]["size_bytes"] == 65536
    assert headers["Cache-Control"] == "no-store"
    assert call(route, token="alpha-reader")[0] == 403
    assert call(route + "/" + "a" * 32)[1]["data"]["content_b64"] == body["files"][0]["content_b64"]


@pytest.mark.parametrize("headers", [{"Range": "bytes=0-1"}, {"If-Range": "abc"},
                                      {"If-None-Match": "abc"}, {"Content-Length": "1"},
                                      {"Transfer-Encoding": "chunked"}, {"Idempotency-Key": "x"}])
def test_get_rejects_range_conditional_and_body_headers(hub, headers):
    call, _store, _clock = hub
    body = completed(call)
    route = f'/sessions/{body["session_id"]}/turns/{body["turn_id"]}/file-snapshots'
    assert call(route, headers=headers)[0] == 400


def test_only_new_upload_gets_512k_wire_limit():
    from pbatch.hub_workspace import request_limit
    assert request_limit("/api/v1/agent/gateway/file-snapshots") == 524288
    assert request_limit("/gateway/file-snapshots") == 524288
    for path in ("/gateway/events", "/gateway/heartbeat", "/gateway/file-snapshots/other",
                 "/gateway/file-snapshots/", "//gateway/file-snapshots"):
        assert request_limit(path) == 65536


def test_upload_read_budget_covers_whole_window(monkeypatch):
    ticks = [0.0]

    class Reader:
        def read1(self, _size):
            ticks[0] += 2.0
            return b"x"

    connection = SimpleNamespace(gettimeout=lambda: 15, settimeout=lambda _value: None)
    handler = SimpleNamespace(rfile=Reader(), connection=connection, close_connection=False)
    monkeypatch.setattr("pbatch.hub_file_snapshot_http.monotonic", lambda: ticks[0])
    with pytest.raises(HubAccessError) as caught:
        read_upload_bytes(handler, 10)
    assert caught.value.status == 408 and handler.close_connection
    assert ticks[0] == 6.0


def test_post_body_reauth_rejects_revoked_uploader(hub, monkeypatch):
    call, store, _clock = hub
    body = completed(call)
    from pbatch.hub_auth import HubAuth
    original = HubAuth.authenticate
    calls = []

    def authenticate(self, value, **kwargs):
        if value == "Bearer alpha":
            calls.append(value)
            if len(calls) == 2:
                raise HubAccessError("unauthorized", "credential expired", 401)
        return original(self, value, **kwargs)

    monkeypatch.setattr(HubAuth, "authenticate", authenticate)
    status, _value, _headers = call("/gateway/file-snapshots", method="POST", token="alpha", body=body)
    assert status == 401 and len(calls) == 2
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM hub_file_snapshot_sets").fetchone()[0] == 0


def test_real_socket_trickle_cannot_extend_upload_window(tmp_path, monkeypatch):
    from pbatch.hub_auth import HubAuth, GATEWAY_SCOPE
    from pbatch.hub_http import HubHTTPServer
    from pbatch.hub_store import HubStore
    from tests.test_hub_http import _grant
    monkeypatch.setattr("pbatch.hub_file_snapshot_http.UPLOAD_SECONDS", 0.15)
    store = HubStore(tmp_path / "slow.sqlite")
    auth = HubAuth({"mode": "development", "grants": [
        _grant("alpha", kind="gateway", instances=("alpha",), scopes=[GATEWAY_SCOPE])]})
    server = HubHTTPServer(("127.0.0.1", 0), store, auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address, timeout=2) as peer:
            peer.sendall(b"POST /api/v1/agent/gateway/file-snapshots HTTP/1.1\r\n"
                         b"Host: localhost\r\nAuthorization: Bearer alpha\r\n"
                         b"Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n")
            started = time.monotonic()
            for _index in range(6):
                peer.sendall(b" ")
                time.sleep(0.03)
            response = peer.recv(4096)
            assert b" 408 " in response.split(b"\r\n", 1)[0]
            assert time.monotonic() - started < 1.0
        with store._connect() as conn:
            assert conn.execute("SELECT count(*) FROM hub_file_snapshot_sets").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        store.close()


@pytest.mark.parametrize("suffix", ["?path=report.txt", "?", "?bad=%FF"])
def test_http_rejects_queries_even_empty_or_invalid_utf8(hub, suffix):
    call, _store, _clock = hub
    body = completed(call)
    route = f'/sessions/{body["session_id"]}/turns/{body["turn_id"]}/file-snapshots'
    assert call(route + suffix)[0] == 400
