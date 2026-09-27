"""HTTP loaded-state checks: startup snapshot, private failures, no provider calls."""

from copy import deepcopy
import json
from pathlib import Path

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
from test_llm_pool_doctor_gateway import models_body, probe
from test_llm_pool_gateway import Reply, gateway_factory, serving, upstream


pytestmark = pytest.mark.scenario


def change_disk(path, data, kind, monkeypatch):
    settings = load_config(str(path))
    if kind in ("priority", "max_concurrent", "quota_group", "enabled"):
        data["accounts"][0][kind] = {"priority": 5, "max_concurrent": 2,
                                     "quota_group": "other-subscription", "enabled": False}[kind]
    elif kind == "reorder":
        data["accounts"].reverse()
    elif kind == "remove":
        data["accounts"].pop()
    elif kind == "add":
        data["accounts"].append({"id": "go-c", "quota_group": "group-c", "credential_file": "keys/c"})
        write_secret(path.parent / "keys" / "c", "test-added-provider-key-0123456789")
    elif kind == "protocol":
        data["models"]["glm-5.1"] = "responses"
    elif kind == "usage":
        data["usage"] = {"enabled": True, "max_age": 10, "timeout": 2}
    elif kind in ("request_timeout", "max_connections", "probe_after"):
        data["gateway"][kind] = 10
    elif kind == "pool_id":
        data["pool_id"] = "another-pool"
    elif kind == "runtime-root":
        monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(path.parent / "other-runtime"))
    elif kind in ("credential-path", "token-path"):
        if kind == "credential-path":
            target, key = path.parent / "keys" / "new-a", KEYS[0]
            data["accounts"][0]["credential_file"] = str(target)
        else:
            target, key = path.parent / "keys" / "new-token", TOKEN
            data["gateway"]["token_file"] = str(target)
        write_secret(target, key)
    elif kind == "key-rotation":
        write_secret(Path(settings.accounts[0].credential_file), "test-rotated-provider-0123456789", replace=True)
    elif kind == "key-swap":
        for account, key in zip(settings.accounts, reversed(KEYS)):
            write_secret(Path(account.credential_file), key, replace=True)
    else:
        pytest.fail("unknown test change")
    save(path, data)


@pytest.mark.parametrize("kind", [
    "priority", "max_concurrent", "quota_group", "enabled", "reorder", "remove", "add",
    "protocol", "usage", "request_timeout", "max_connections", "probe_after", "pool_id",
    "runtime-root", "credential-path", "token-path", "key-rotation", "key-swap",
])
def test_unchanged_model_ids_no_longer_hide_stale_configuration_or_keys(pool_file, upstream, monkeypatch, kind):
    path, data = pool_file
    settings = credentials(path)
    with serving(PoolGateway(settings, port=0, upstream_url=upstream.url)) as server:
        data["gateway"]["port"] = server.server_port
        save(path, data)
        assert doctor_report(str(path), gateway=True)["ok"] is True
        original_tag = server.snapshot
        change_disk(path, data, kind, monkeypatch)
        before = tree_snapshot(path.parent)
        report = doctor_report(str(path), gateway=True)
        assert report["ok"] is False
        assert check(report, "gateway")["code"] == "gateway_models_match"
        assert check(report, "gateway_snapshot")["code"] == "gateway_snapshot_mismatch"
        assert "gateway_account_snapshot" not in report["not_checked"]
        assert "provider_authentication" in report["not_checked"]
        assert tree_snapshot(path.parent) == before and server.snapshot == original_tag
        assert not upstream.calls
        output = json.dumps(report)
        assert not any(value in output for value in (TOKEN, *KEYS, original_tag, str(path.parent)))


def test_restart_loads_rotated_keys_and_updated_protocol_without_model_call(pool_file, upstream):
    path, data = pool_file
    settings = credentials(path)
    with serving(PoolGateway(settings, port=0, upstream_url=upstream.url)) as server:
        data["gateway"]["port"] = server.server_port
        data["models"]["glm-5.1"] = "responses"
        write_secret(Path(settings.accounts[0].credential_file), "test-new-provider-key-0123456789", replace=True)
        save(path, data)
        assert doctor_report(str(path), gateway=True)["ok"] is False
    with serving(PoolGateway(load_config(str(path)), port=0, upstream_url=upstream.url)) as server:
        data["gateway"]["port"] = server.server_port
        save(path, data)
        before = tree_snapshot(path.parent)
        report = doctor_report(str(path), gateway=True)
        assert report["ok"] is True
        assert check(report, "gateway_snapshot")["code"] == "gateway_snapshot_match"
        assert tree_snapshot(path.parent) == before and not upstream.calls


def test_disabled_key_rotation_does_not_require_loading_unused_credentials(pool_file, upstream):
    path, data = pool_file
    data["accounts"][1]["enabled"] = False
    save(path, data)
    settings = credentials(path)
    with serving(PoolGateway(settings, port=0, upstream_url=upstream.url)) as server:
        data["gateway"]["port"] = server.server_port
        save(path, data)
        Path(settings.accounts[1].credential_file).unlink()
        assert doctor_report(str(path), gateway=True)["ok"] is True
        assert "go-b" not in server.keys and not upstream.calls


def test_startup_snapshot_is_frozen_and_models_endpoint_never_rereads_credentials(pool_file, upstream, monkeypatch):
    path, data = pool_file
    settings = credentials(path)
    with serving(PoolGateway(settings, port=0, upstream_url=upstream.url)) as server:
        data["gateway"]["port"] = server.server_port
        save(path, data)
        before = tree_snapshot(path.parent)
        def forbidden(*args, **kwargs):
            pytest.fail("models endpoint must use loaded snapshot without I/O")
        monkeypatch.setattr("pbatch.llm_pool_gateway.read_secret", forbidden)
        settings.models.clear()  # Caller-owned mutable input cannot mutate the gateway.
        with pytest.raises(TypeError):
            server.settings.models["glm-5.1"] = "responses"
        with pytest.raises(TypeError):
            server.keys["go-a"] = "changed"
        assert doctor_report(str(path), gateway=True)["ok"] is True
        assert tree_snapshot(path.parent) == before and not upstream.calls


def test_gateway_loads_each_credential_once_against_the_same_local_token(pool_file, upstream, monkeypatch):
    path, _ = pool_file
    settings = credentials(path)
    from pbatch import llm_pool_gateway as gateway
    original, calls = gateway.read_secret, []
    def tracked(source):
        calls.append(source)
        return original(source)
    monkeypatch.setattr(gateway, "read_secret", tracked)
    with PoolGateway(settings, port=0, upstream_url=upstream.url):
        assert calls == [settings.token_file, *(Path(account.credential_file) for account in settings.accounts)]


@pytest.mark.parametrize("value,code", [
    (None, "gateway_snapshot_unavailable"), ("", "gateway_snapshot_invalid"),
    ("v2:" + "0" * 64, "gateway_snapshot_invalid"),
    ("v1:" + "0" * 63, "gateway_snapshot_invalid"),
    ("v1:" + "0" * 65, "gateway_snapshot_invalid"),
    ("v1:" + "A" * 64, "gateway_snapshot_invalid"),
    ("secret-header-content-0123456789", "gateway_snapshot_invalid"),
    ("v1:" + "0" * 64, "gateway_snapshot_mismatch"),
    ("v1:" + "0" * 64 + "\r\n" + SNAPSHOT_HEADER + ": v1:" + "0" * 64, "gateway_snapshot_invalid"),
])
def test_legacy_unknown_or_duplicate_snapshot_headers_fail_closed_and_are_redacted(pool_file, probe, capsys, value, code):
    path, data = pool_file
    probe.replies.put(Reply(body=models_body(data), headers={SNAPSHOT_HEADER: value}))
    assert pool_main(["--config", str(path), "doctor", "--gateway", "--json"]) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert check(report, "gateway")["status"] == "pass"
    assert check(report, "gateway_snapshot")["code"] == code
    assert "secret-header-content" not in output.out + output.err
    assert ("gateway_account_snapshot" not in report["not_checked"]) is (code == "gateway_snapshot_mismatch")
    assert len(probe.calls) == 1


def test_missing_local_key_never_claims_loaded_credentials_were_checked(pool_file, probe):
    path, data = pool_file
    Path(load_config(str(path)).accounts[0].credential_file).unlink()
    probe.replies.put(Reply(body=models_body(data), headers={SNAPSHOT_HEADER: "v1:" + "0" * 64}))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "gateway")["status"] == "pass"
    assert check(report, "gateway_snapshot")["code"] == "gateway_snapshot_local_incomplete"
    assert "gateway_credential_snapshot" in report["not_checked"]


@pytest.mark.parametrize("headers,path,code", [
    ({}, "/v1/models", 200), ({"Authorization": None}, "/v1/models", 401),
    ({"Authorization": "Bearer invalid"}, "/v1/models", 401),
    ({"Origin": "https://example.invalid"}, "/v1/models", 403), ({}, "/unknown", 404),
])
def test_snapshot_header_is_only_on_authenticated_models_responses(gateway_factory, headers, path, code):
    gateway = gateway_factory()
    reply = gateway.request(b"", method="GET", path=path, headers=headers)
    assert reply.status == code
    if code == 200:
        assert reply.headers[SNAPSHOT_HEADER.lower()] == gateway.server.snapshot
        assert reply.headers["cache-control"] == "no-store"
        assert set(json.loads(reply.body)) == {"object", "data"}
    else:
        assert SNAPSHOT_HEADER.lower() not in reply.headers


def test_model_responses_do_not_expose_the_loaded_state_comparison_tag(gateway_factory, upstream):
    gateway = gateway_factory()
    upstream.replies.put(Reply())
    response = gateway.request()
    assert response.status == 200
    assert SNAPSHOT_HEADER.lower() not in response.headers
    assert SNAPSHOT_HEADER.lower() not in upstream.calls[0].headers
    assert gateway.server.snapshot not in str(upstream.calls[0].headers)


def test_valid_snapshot_does_not_override_mismatched_models_payload(pool_file, probe):
    path, data = pool_file
    settings = load_config(str(path))
    tag = snapshot_tag(settings, TOKEN, dict(zip((a.id for a in settings.accounts), KEYS)))
    remote = deepcopy(data)
    remote["models"]["new-model"] = "responses"
    probe.replies.put(Reply(body=models_body(remote), headers={SNAPSHOT_HEADER: tag}))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "gateway")["code"] == "gateway_models_mismatch"
    assert check(report, "gateway_snapshot")["code"] == "gateway_snapshot_match"
