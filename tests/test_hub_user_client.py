"""A running Hub client observes shared user login, logout and token rejection."""

import json
import time
import urllib.error

import pytest

from pbatch.hub_client import HubClient, HubError
from pbatch.hub_user_auth_config import load_user_config
from pbatch.hub_user_credentials import CredentialStore
from test_hub_client import _HubHandler, hub_server


@pytest.fixture
def user_profile(tmp_path, hub_server):
    path = tmp_path / "user.json"
    path.write_text(json.dumps({
        "hub_url": hub_server, "device_authorization_endpoint": "https://identity.test/device/code",
        "token_endpoint": "https://identity.test/token", "client_id": "user-cli",
        "resource": "agent-hub", "scopes": ["agent.sessions:read"],
        "credential_directory": "credentials",
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def _login(store, token="user-token"):
    now = time.time()
    store.finish_login(store.begin_login(), {
        "access_token": token, "issued_at": now, "expires_at": now + 60})


def test_client_is_offline_until_request_and_observes_another_process_login(user_profile):
    client = HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": str(user_profile)})
    assert _HubHandler.seen == []
    assert not (user_profile.parent / "credentials").exists()
    with pytest.raises(HubError) as missing:
        client.list_sessions()
    assert missing.value.code == "user_login_required" and _HubHandler.seen == []
    store = CredentialStore(load_user_config(user_profile))
    _login(store)
    assert client.list_sessions() == [{"session_id": "ses-1"}]
    store.logout()
    before = len(_HubHandler.seen)
    with pytest.raises(HubError) as logged_out:
        client.list_sessions()
    assert logged_out.value.status == 401 and len(_HubHandler.seen) == before
    _login(store, "second-token")
    assert client.list_sessions() == [{"session_id": "ses-1"}]
    assert _HubHandler.seen[-1][2]["Authorization"] == "Bearer second-token"


def test_profile_cannot_send_token_to_another_hub(user_profile):
    with pytest.raises(ValueError):
        HubClient.from_env("https://other-hub.test", environ={
            "PBATCH_HUB_USER_CONFIG": str(user_profile)})
    assert _HubHandler.seen == []


def test_user_401_invalidates_cache_without_replaying_business_write(user_profile):
    store = CredentialStore(load_user_config(user_profile))
    _login(store, "old-token")
    client = HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": str(user_profile)})
    with pytest.raises(HubError) as rejected:
        client._request("POST", "/rejected-write", {"operation": "once"})
    assert rejected.value.status == 401 and len(_HubHandler.seen) == 1
    assert not store.status()["authenticated"]
    _login(store, "new-token")
    assert client._request("POST", "/rejected-write", {}) == {"state": "accepted"}
    assert len(_HubHandler.seen) == 2


def test_late_401_does_not_clear_newly_completed_login(user_profile):
    store = CredentialStore(load_user_config(user_profile))
    _login(store, "old-token")
    client = HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": str(user_profile)})
    original_open = client._opener.open

    def delayed_response(*args, **kwargs):
        try:
            return original_open(*args, **kwargs)
        except urllib.error.HTTPError:
            _login(store, "new-token")
            raise

    client._opener.open = delayed_response
    with pytest.raises(HubError) as rejected:
        client._request("POST", "/rejected-write", {})
    assert rejected.value.status == 401
    assert store.token() == "new-token"
    assert len(_HubHandler.seen) == 1


def test_user_403_preserves_login_for_other_authorized_operations(user_profile):
    store = CredentialStore(load_user_config(user_profile))
    _login(store)
    client = HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": str(user_profile)})
    with pytest.raises(HubError) as rejected:
        client._request("POST", "/forbidden-write", {})
    assert rejected.value.status == 403 and store.status()["authenticated"]
    assert client.list_sessions() == [{"session_id": "ses-1"}]


def test_http_response_closes_when_invalidating_user_credentials_fails(user_profile):
    store = CredentialStore(load_user_config(user_profile))
    _login(store, "old-token")
    client = HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": str(user_profile)})
    original_open = client._opener.open
    responses = []

    def capture_error(*args, **kwargs):
        try:
            return original_open(*args, **kwargs)
        except urllib.error.HTTPError as error:
            responses.append(error)
            raise

    def cannot_invalidate(_token):
        raise HubError("private user credentials are invalid or unavailable",
                       code="user_credentials_unavailable", status=401)

    client._opener.open = capture_error
    client._token_provider.invalidate = cannot_invalidate
    with pytest.raises(HubError) as rejected:
        client._request("POST", "/rejected-write", {})
    assert rejected.value.code == "user_credentials_unavailable"
    assert len(responses) == 1 and len(_HubHandler.seen) == 1
    try:
        assert responses[0].fp.closed
    finally:
        responses[0].close()
