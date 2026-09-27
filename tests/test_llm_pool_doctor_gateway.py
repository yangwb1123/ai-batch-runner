"""Explicit loopback-only doctor probes, including real gateway and hostile replies."""

from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import queue
import threading
import time

import pytest

from pbatch.llm_pool_cli import pool_main
from pbatch.llm_pool_config import load_config
from pbatch.llm_pool_doctor import doctor_report
from pbatch.llm_pool_gateway import PoolGateway
from pbatch.llm_pool_secrets import write_secret
from pbatch.llm_pool_snapshot import SNAPSHOT_HEADER, snapshot_tag
from test_llm_pool_doctor import (
    KEYS, TOKEN, check, credentials, pool_file, save, tree_snapshot, unsafe_pool_file,
)
from test_llm_pool_gateway import FakeHandler, Reply, serving, upstream


pytestmark = pytest.mark.scenario


def models_body(data):
    return json.dumps({"object": "list", "data": [
        {"id": name, "object": "model", "owned_by": "opencode-go"}
        for name in data["models"]]}).encode()


class ProbeHandler(FakeHandler):
    def do_GET(self):
        self.server.methods.append("GET")
        super().do_POST()


@pytest.fixture
def probe(pool_file):
    path, data = pool_file
    credentials(path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    server.replies, server.calls, server.errors = queue.Queue(), [], []
    server.methods, server.stopping = [], threading.Event()
    data["gateway"]["port"] = server.server_port
    save(path, data)
    with serving(server):
        try:
            yield server
        finally:
            server.stopping.set()
    assert not server.errors


def test_explicit_probe_only_gets_loopback_models_with_local_token(pool_file, probe, monkeypatch):
    path, data = pool_file
    settings = load_config(str(path))
    tag = snapshot_tag(settings, TOKEN, dict(zip((a.id for a in settings.accounts), KEYS)))
    probe.replies.put(Reply(body=models_body(data), headers={SNAPSHOT_HEADER: tag}))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    before = tree_snapshot(path.parent)
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is True and "gateway" not in report["not_checked"]
    assert check(report, "gateway")["code"] == "gateway_models_match"
    assert check(report, "gateway_snapshot")["code"] == "gateway_snapshot_match"
    assert "gateway_credential_snapshot" not in report["not_checked"]
    assert tag not in json.dumps(report) and tag not in str(probe.calls)
    assert probe.methods == ["GET"] and len(probe.calls) == 1
    call = probe.calls[0]
    assert call.path == "/v1/models" and call.body == b""
    assert call.headers["authorization"] == "Bearer " + TOKEN
    assert "cookie" not in call.headers and "x-api-key" not in call.headers
    assert not any(key in str(call.headers) for key in KEYS)
    assert tree_snapshot(path.parent) == before


def test_probe_failure_does_not_hide_local_credential_failure(pool_file, probe):
    path, _ = pool_file
    settings = load_config(str(path))
    Path(settings.accounts[0].credential_file).unlink()
    probe.replies.put(Reply(503))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "account_credential", "go-a")["status"] == "fail"
    assert check(report, "gateway")["code"] == "gateway_http_error"


def test_missing_token_skips_probe_without_sending_provider_keys(pool_file, probe):
    path, _ = pool_file
    load_config(str(path)).token_file.unlink()
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False and not probe.calls
    assert check(report, "gateway")["status"] == "skip"


@pytest.mark.parametrize("status,code", [(401, "gateway_auth_rejected"),
                                       (403, "gateway_auth_rejected"),
                                       (302, "gateway_redirect_rejected"),
                                       (307, "gateway_redirect_rejected"),
                                       (429, "gateway_http_error"), (500, "gateway_http_error")])
def test_errors_and_redirects_are_not_followed_or_echoed(pool_file, probe, capsys, status, code):
    path, _ = pool_file
    secret = "upstream-body-secret-123456789"
    probe.replies.put(Reply(status, secret.encode(), {"Location": "https://example.invalid/" + secret}))
    assert pool_main(["--config", str(path), "doctor", "--gateway", "--json"]) == 1
    output = capsys.readouterr()
    assert check(json.loads(output.out), "gateway")["code"] == code
    assert secret not in output.out + output.err and TOKEN not in output.out
    assert len(probe.calls) == 1


@pytest.mark.parametrize("kind", ["text", "empty", "non-json", "non-finite", "duplicate-key",
                                 "not-list", "not-object", "not-entries", "not-entry",
                                 "missing-id", "bad-id", "bad-owner", "bad-object",
                                 "empty-list", "duplicate-model", "too-many", "deep"])
def test_malformed_models_are_rejected_without_reflecting_body(pool_file, probe, kind):
    path, data = pool_file
    payload = json.loads(models_body(data))
    if kind == "not-list":
        payload["object"] = "not-list"
    elif kind == "not-entries":
        payload["data"] = {}
    elif kind == "not-entry":
        payload["data"] = ["credential-secret-123456789"]
    elif kind == "missing-id":
        del payload["data"][0]["id"]
    elif kind in ("bad-id", "bad-owner", "bad-object"):
        field = {"bad-id": "id", "bad-owner": "owned_by", "bad-object": "object"}[kind]
        payload["data"][0][field] = None
    elif kind == "empty-list":
        payload["data"] = []
    elif kind in ("duplicate-model", "too-many"):
        payload["data"] = payload["data"] * (2 if kind == "duplicate-model" else 100)
    raw = {"text": b"secret-body-123456789", "empty": b"", "non-json": b"{broken-json",
           "non-finite": b'{"object":NaN}', "duplicate-key": b'{"object":1,"object":2}',
           "not-object": b"[]", "deep": b"[" * 2000 + b"]" * 2000}.get(kind)
    probe.replies.put(Reply(body=raw if raw is not None else json.dumps(payload).encode()))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "gateway")["code"] == "gateway_invalid_response"
    assert "secret-body-123456789" not in json.dumps(report)


@pytest.mark.parametrize("headers", [
    {"Content-Length": None}, {"Content-Length": "-1"}, {"Content-Length": "65537"},
    {"Content-Length": "999999999999999999999"}, {"Content-Length": "10, 10"},
    {"Content-Length": "0"}, {"Transfer-Encoding": "chunked"},
    {"Content-Type": "text/html"}, {"Content-Encoding": "gzip"},
    {"Content-Length": "300"}, {"Content-Length": "100\r\nContent-Length: 100"},
    {"Content-Type": "application/json\r\nContent-Type: text/html"},
    {"Content-Encoding": "identity\r\nContent-Encoding: gzip"},
])
def test_unbounded_or_ambiguous_response_framing_is_rejected(pool_file, probe, headers):
    path, data = pool_file
    probe.replies.put(Reply(body=models_body(data), headers=headers))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "gateway")["code"] == "gateway_invalid_response"


@pytest.mark.parametrize("change", ["remove", "add", "rename"])
def test_stale_model_list_requires_config_check_or_restart(pool_file, probe, change):
    path, data = pool_file
    remote = deepcopy(data)
    if change == "remove":
        remote["models"].pop("glm-5.1")
    elif change == "add":
        remote["models"]["another-model"] = "responses"
    else:
        remote["models"]["renamed-model"] = remote["models"].pop("glm-5.1")
    probe.replies.put(Reply(body=models_body(remote)))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "gateway")["code"] == "gateway_models_mismatch"


@pytest.mark.parametrize("kind", ["headers", "body"])
def test_probe_has_total_deadline_even_while_response_trickles(pool_file, probe, monkeypatch, kind):
    path, data = pool_file
    monkeypatch.setattr("pbatch.llm_pool_doctor.GATEWAY_TIMEOUT", 0.15)
    body = models_body(data)
    reply = (Reply(body=body, hold=threading.Event()) if kind == "headers" else
             Reply(body=body[:1], headers={"Content-Length": str(len(body))},
                   chunks=tuple((0.03, bytes([value])) for value in body[1:])))
    probe.replies.put(reply)
    start = time.monotonic()
    report = doctor_report(str(path), gateway=True)
    assert time.monotonic() - start < 1.5
    assert report["ok"] is False and check(report, "gateway")["status"] == "fail"


def test_closed_socket_is_reported_without_exception_details(pool_file, probe):
    path, _ = pool_file
    probe.replies.put(Reply(drop=True))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "gateway")["code"] == "gateway_unavailable"


@pytest.mark.parametrize("change", ["none", "token", "models", "accounts"])
def test_real_gateway_probe_never_calls_provider_or_mutates_state(pool_file, upstream, change):
    path, data = pool_file
    settings = credentials(path)
    with serving(PoolGateway(settings, port=0, upstream_url=upstream.url)) as server:
        data["gateway"]["port"] = server.server_port
        if change == "token":
            write_secret(settings.token_file, "rotated-doctor-token-0123456789", replace=True)
        elif change == "models":
            data["models"]["new-model"] = "responses"
        elif change == "accounts":
            data["accounts"][0]["enabled"] = False
        save(path, data)
        before = tree_snapshot(path.parent)
        report = doctor_report(str(path), gateway=True)
        assert report["ok"] is (change == "none")
        assert ("gateway_account_snapshot" in report["not_checked"]) is (change == "token")
        if change == "accounts":
            assert check(report, "gateway_snapshot")["code"] == "gateway_snapshot_mismatch"
        assert "runtime_state" in report["not_checked"]
        assert tree_snapshot(path.parent) == before
        assert not upstream.calls
