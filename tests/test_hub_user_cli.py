"""User authentication commands never require or print existing credentials."""

import json
import sys
import types

import pytest

from pbatch import hub_auth_cli, hub_cli
from pbatch.hub_client import HubClient, HubError


@pytest.fixture
def auth_boundary(monkeypatch):
    calls = []
    config = {"hub_url": "https://hub.example.test"}

    class Store:
        def __init__(self, value):
            assert value == config

        def begin_login(self):
            calls.append("begin")
            return "ticket"

        def finish_login(self, ticket, credential):
            calls.append(("finish", ticket, credential))

        def cancel_login(self, ticket):
            calls.append(("cancel", ticket))

        def status(self):
            return {"authenticated": False, "pending": False}

        def logout(self):
            calls.append("logout")

    class Authorization:
        def __init__(self, value, timeout=5):
            assert value == config and timeout == 5
            calls.append("authorization")

        def start(self):
            calls.append("start")
            return {"device_code": "private-device-code", "user_code": "ABCD-EFGH",
                    "verification_uri": "https://identity.example.test/device/verify"}

        def poll(self, challenge):
            assert challenge["device_code"] == "private-device-code"
            calls.append("poll")
            return {"access_token": "private-access-token", "issued_at": 10, "expires_at": 70}

    modules = {
        "hub_user_auth_config": {"load_user_config": lambda path: config},
        "hub_user_credentials": {"CredentialStore": Store},
        "hub_device_oauth": {"DeviceAuthorization": Authorization},
    }
    for name, members in modules.items():
        module = types.ModuleType("pbatch." + name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(HubClient, "from_env", lambda *_a, **_k: pytest.fail("Hub contacted"))
    monkeypatch.setenv("PBATCH_HUB_USER_CONFIG", "/private/user.json")
    return calls, Store, Authorization


def test_device_login_dispatches_before_hub_authentication_and_hides_secrets(auth_boundary, capsys):
    calls, _, _ = auth_boundary
    assert hub_cli.hub_main(["auth", "login"]) == 0
    output = capsys.readouterr()
    assert "https://identity.example.test/device/verify" in output.out
    assert "ABCD-EFGH" in output.out
    assert "private-device-code" not in output.out + output.err
    assert "private-access-token" not in output.out + output.err
    assert calls[:4] == ["authorization", "begin", "start", "poll"]
    assert calls[-1][0:2] == ("finish", "ticket")


def test_interrupt_cancels_only_pending_login(auth_boundary, capsys, monkeypatch):
    calls, _, authorization = auth_boundary

    def interrupt(_self, _challenge):
        raise KeyboardInterrupt

    monkeypatch.setattr(authorization, "poll", interrupt)
    assert hub_cli.hub_main(["auth", "login"]) == 130
    assert calls[-1] == ("cancel", "ticket")
    assert not any(isinstance(call, tuple) and call[0] == "finish" for call in calls)
    assert "cancelled" in capsys.readouterr().err


def test_failed_commit_does_not_report_success(auth_boundary, capsys, monkeypatch):
    calls, store, _ = auth_boundary

    def superseded(_self, _ticket, _credential):
        raise HubError("login was superseded", code="login_superseded")

    monkeypatch.setattr(store, "finish_login", superseded)
    assert hub_auth_cli.main(["login"]) == 2
    output = capsys.readouterr()
    assert "session saved" not in output.out and "superseded" in output.err
    assert calls[-1] == ("cancel", "ticket")


@pytest.mark.parametrize("action", ["status", "logout"])
def test_local_actions_never_start_authorization(auth_boundary, capsys, action):
    calls, _, _ = auth_boundary
    assert hub_cli.hub_main(["auth", action]) == 0
    output = capsys.readouterr()
    assert "authorization" not in calls
    if action == "status":
        assert json.loads(output.out) == {"authenticated": False, "pending": False}
    else:
        assert calls == ["logout"]
        assert "does not revoke" in output.out


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "121"])
def test_login_timeout_is_rejected_before_profile_io(timeout, monkeypatch, capsys):
    monkeypatch.delenv("PBATCH_HUB_USER_CONFIG", raising=False)
    assert hub_auth_cli.main(["login", "--timeout", timeout]) == 2
    assert "timeout" in capsys.readouterr().err


@pytest.mark.parametrize("other", ["PBATCH_HUB_TOKEN", "PBATCH_HUB_TOKEN_FILE",
                                  "PBATCH_HUB_OAUTH_CONFIG"])
def test_user_profile_conflicts_before_opening_config(other):
    with pytest.raises(ValueError, match="conflicts"):
        HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": "/missing/user.json",
                                    other: "other-credential"})


def test_explicit_machine_profile_conflicts_with_user_login():
    with pytest.raises(ValueError, match="conflicts"):
        HubClient.from_env(oauth_config="/missing/machine.json", environ={
            "PBATCH_HUB_USER_CONFIG": "/missing/user.json"})


def test_gateway_cannot_use_ambient_human_login(monkeypatch, capsys):
    monkeypatch.setenv("PBATCH_HUB_USER_CONFIG", "/private/user.json")
    assert hub_cli.hub_main(["agent", "--cwd", "/missing/project"]) == 2
    assert "requires machine credentials" in capsys.readouterr().err
