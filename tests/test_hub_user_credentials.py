"""Private shared short-lived user sessions and cross-process logout fencing."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import stat
import time

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_user_auth_config import profile_digest
from pbatch import hub_user_credential_files as files
from pbatch.hub_user_credentials import CredentialStore, UserTokenProvider


@pytest.fixture
def config(tmp_path):
    return {"hub_url": "https://hub.test", "device_authorization_endpoint": "https://sso.test/device",
            "token_endpoint": "https://sso.test/token", "client_id": "cli-user", "resource": "agent-hub",
            "scopes": ["agent.sessions:read"], "credential_directory": str(tmp_path / "credentials")}


@pytest.fixture
def store(config):
    result = CredentialStore(config)
    result._clock = lambda: 1000.0
    return result


def _credential(token="user-bearer", **overrides):
    return dict(access_token=token, issued_at=1000.0, expires_at=1060.0, **overrides)


def _sign_in(store, token="user-bearer"):
    ticket = store.begin_login()
    store.finish_login(ticket, _credential(token))
    return ticket


def _path(config, name=files.STATE_FILE):
    return Path(config["credential_directory"]) / name


def _disk(config):
    return json.loads(_path(config).read_text())


def _unavailable(operation):
    with pytest.raises(HubError) as rejected:
        operation()
    assert rejected.value.code == "user_credentials_unavailable"
    assert str(rejected.value) == files.ERROR_MESSAGE


def test_absent_status_logout_cancel_and_token_do_not_create_any_directory(store, config):
    store._directory = str(Path(config["credential_directory"]) / "nested")
    assert store.status() == {"authenticated": False, "pending": False}
    store.logout()
    store.cancel_login("0" * 64)
    with pytest.raises(HubError) as missing:
        store.token()
    assert (missing.value.code, missing.value.status) == ("user_login_required", 401)
    assert not Path(config["credential_directory"]).exists()


def test_save_is_private_bound_and_status_never_exposes_token_or_profile(store, config):
    ticket = store.begin_login()
    assert store.status() == {"authenticated": False, "pending": True}
    store.finish_login(ticket, _credential())
    assert store.token() == "user-bearer"
    assert store.status() == {"authenticated": True, "pending": False, "expires_at": 1060.0}
    assert _disk(config)["profile"] == profile_digest(config)
    directory = Path(config["credential_directory"])
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert {item.name for item in directory.iterdir()} == {files.LOCK_FILE, files.STATE_FILE}
    for item in directory.iterdir():
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
        assert item.stat().st_uid == os.getuid()
    assert "user-bearer" not in repr(store)


def test_pending_login_preserves_prior_session_and_only_latest_completion_wins(store):
    _sign_in(store, "initial")
    old = store.begin_login()
    current = store.begin_login()
    assert old != current
    assert store.token() == "initial"
    assert store.status()["pending"] is True
    store.cancel_login(old)
    with pytest.raises(HubError) as late:
        store.finish_login(old, _credential("old-completion"))
    assert late.value.code == "user_login_superseded"
    store.finish_login(current, _credential("new-completion"))
    store.cancel_login(old)
    assert store.token() == "new-completion"


def test_logout_retains_tombstone_and_late_login_cannot_revive_session(store, config):
    _sign_in(store)
    ticket = store.begin_login()
    store.logout()
    assert _disk(config) == {"version": 1, "profile": profile_digest(config),
                             "pending": "", "credential": None}
    with pytest.raises(HubError) as late:
        store.finish_login(ticket, _credential("late-token"))
    assert late.value.code == "user_login_superseded"
    assert store.status() == {"authenticated": False, "pending": False}
    _sign_in(store, "later-explicit-login")
    assert store.token() == "later-explicit-login"


def test_cancel_only_own_pending_and_old_401_cannot_clear_new_session(store, config):
    _sign_in(store, "old-token")
    ticket = store.begin_login()
    store.cancel_login(ticket)
    assert store.status()["pending"] is False
    assert store.token() == "old-token"
    _sign_in(store, "new-token")
    store.invalidate("old-token")
    assert store.token() == "new-token"
    pending = store.begin_login()
    store.invalidate("new-token")
    assert store.status() == {"authenticated": False, "pending": True}
    assert _disk(config)["credential"] is None
    store.finish_login(pending, _credential("next-token"))
    assert store.token() == "next-token"


@pytest.mark.parametrize("operation", ["token", "status"])
def test_expiry_removes_token_from_disk_and_requires_login(store, config, operation):
    _sign_in(store)
    store._clock = lambda: 1060.0
    if operation == "token":
        with pytest.raises(HubError) as expired:
            store.token()
        assert (expired.value.code, expired.value.status) == ("user_login_required", 401)
    else:
        assert store.status() == {"authenticated": False, "pending": False}
    assert _disk(config)["credential"] is None
    assert b"user-bearer" not in _path(config).read_bytes()


@pytest.mark.parametrize("changes", [
    {"hub_url": "https://other-hub.test"}, {"client_id": "other-client"},
    {"resource": "another-resource"}, {"scopes": ["agent.devices:read"]},
    {"verification_origin": "https://another-verify.test"},
    {"device_authorization_endpoint": "https://sso.test/another-device"},
    {"token_endpoint": "https://sso.test/another-token"},
])
def test_saved_profile_cannot_be_reused_for_other_authority(store, config, changes):
    _sign_in(store)
    prior = _path(config).read_bytes()
    changed = CredentialStore(dict(config, **changes))
    for operation in (changed.token, changed.status, changed.begin_login, changed.logout):
        _unavailable(operation)
    assert _path(config).read_bytes() == prior


@pytest.mark.parametrize("field,value", [
    ("issued_at", True), ("issued_at", -1), ("issued_at", 1001),
    ("issued_at", float("nan")), ("issued_at", 10 ** 400),
    ("expires_at", float("inf")), ("expires_at", 1000), ("expires_at", 87401),
    ("access_token", ""), ("access_token", "token\nprivate"),
    ("access_token", "α"), ("access_token", "a" * 16385),
])
def test_invalid_credentials_are_rejected_without_replacing_prior_session(store, field, value):
    _sign_in(store, "prior-token")
    ticket = store.begin_login()
    credential = _credential()
    credential[field] = value
    _unavailable(lambda: store.finish_login(ticket, credential))
    assert store.token() == "prior-token"
    assert store.status()["pending"] is True


def test_refresh_and_id_token_fields_are_not_accepted(store):
    ticket = store.begin_login()
    for field in ("refresh_token", "id_token"):
        _unavailable(lambda: store.finish_login(ticket, _credential(**{field: "not-stored"})))
    assert store.status()["authenticated"] is False


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b'{"version":1,"version":1}',
                                  b'{"expires_at":NaN}', b"x" * 32769])
def test_corrupt_or_unbounded_state_fails_closed(store, config, raw):
    _sign_in(store)
    _path(config).write_bytes(raw)
    _unavailable(store.token)
    _unavailable(store.status)


@pytest.mark.parametrize("name,mode", [(files.STATE_FILE, 0o644), (files.STATE_FILE, 0o400),
                                       (files.LOCK_FILE, 0o666), ("", 0o755)])
def test_existing_permissions_are_rejected_without_chmod(store, config, name, mode):
    _sign_in(store)
    path = _path(config, name)
    path.chmod(mode)
    _unavailable(store.status)
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_directory_must_belong_to_current_uid(store, config, monkeypatch):
    _sign_in(store)
    uid = os.getuid()
    monkeypatch.setattr(files.os, "getuid", lambda: uid + 1)
    _unavailable(store.status)


@pytest.mark.parametrize("name", [files.STATE_FILE, files.LOCK_FILE])
def test_fifo_and_hardlinked_files_are_rejected_without_blocking(store, config, name):
    _sign_in(store)
    target = _path(config, name)
    target.unlink()
    os.mkfifo(target, 0o600)
    started = time.monotonic()
    _unavailable(store.status)
    assert time.monotonic() - started < 0.5
    target.unlink()
    other = target.with_name("hardlink-source")
    other.write_text("{}" if name == files.STATE_FILE else "")
    other.chmod(0o600)
    os.link(other, target)
    _unavailable(store.status)


@pytest.mark.parametrize("name", [files.STATE_FILE, files.LOCK_FILE])
def test_symlinked_state_and_lock_never_touch_target(store, config, name):
    _sign_in(store)
    target = _path(config, name)
    target.unlink()
    other = target.with_name("unrelated-private-file")
    other.write_text("unrelated contents")
    other.chmod(0o600)
    target.symlink_to(other)
    _unavailable(store.status)
    assert other.read_text() == "unrelated contents"


@pytest.mark.parametrize("ancestor", [False, True])
def test_directory_symlink_components_are_refused(config, tmp_path, ancestor):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    path = link / "child" if ancestor else link
    selected = CredentialStore(dict(config, credential_directory=str(path)))
    _unavailable(selected.begin_login)
    assert list(outside.iterdir()) == []


def test_lock_wait_is_bounded_and_does_not_change_session(store, config):
    _sign_in(store)
    descriptor = os.open(_path(config, files.LOCK_FILE), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store._lock_timeout = 0.02
        started = time.monotonic()
        _unavailable(store.status)
        assert time.monotonic() - started < 0.5
    finally:
        os.close(descriptor)
    assert store.token() == "user-bearer"


@pytest.mark.parametrize("failure", ["replace", "file_fsync", "write"])
def test_failed_atomic_save_preserves_previous_state_and_removes_temporary(store, config,
                                                                        monkeypatch, failure):
    _sign_in(store, "old-token")
    ticket = store.begin_login()
    before = _path(config).read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("private secret and path must not escape")

    name = {"replace": "replace", "file_fsync": "fsync", "write": "write"}[failure]
    with monkeypatch.context() as patch:
        patch.setattr(files.os, name, fail)
        _unavailable(lambda: store.finish_login(ticket, _credential("new-token")))
    assert _path(config).read_bytes() == before
    assert store.token() == "old-token"
    assert not list(Path(config["credential_directory"]).glob(".session-*.tmp"))


def test_atomic_write_handles_short_writes(store, monkeypatch):
    original = files.os.write
    monkeypatch.setattr(files.os, "write", lambda fd, raw: original(fd, raw[:7]))
    _sign_in(store)
    assert store.token() == "user-bearer"


def test_restrictive_umask_cannot_commit_state_with_unexpected_permissions(store, config):
    _sign_in(store, "old-token")
    ticket = store.begin_login()
    before = _path(config).read_bytes()
    prior = os.umask(0o200)
    try:
        _unavailable(lambda: store.finish_login(ticket, _credential("new-token")))
    finally:
        os.umask(prior)
    assert _path(config).read_bytes() == before


def test_directory_fsync_error_does_not_report_success_or_restore_old_token(store, config,
                                                                          monkeypatch):
    _sign_in(store, "old-token")
    ticket = store.begin_login()
    original = files.os.fsync

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed with private diagnostic")
        return original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "fsync", fail_directory_sync)
        _unavailable(lambda: store.finish_login(ticket, _credential("new-token")))
    assert store.token() == "new-token"
    assert _disk(config)["pending"] == ""
    assert b"old-token" not in _path(config).read_bytes()


def test_directory_replacement_after_lock_cannot_redirect_save(store, config, monkeypatch):
    _sign_in(store)
    directory = Path(config["credential_directory"])
    original = files._acquire

    def replace_directory(descriptor, timeout):
        original(descriptor, timeout)
        directory.rename(directory.with_name("moved-private-directory"))
        directory.mkdir(mode=0o700)

    monkeypatch.setattr(files, "_acquire", replace_directory)
    _unavailable(store.begin_login)
    assert list(directory.iterdir()) == []


def test_lock_replacement_while_waiting_cannot_create_split_brain(store, config, monkeypatch):
    _sign_in(store)
    prior = _path(config).read_bytes()
    original = files._acquire

    def replace_lock(descriptor, timeout):
        original(descriptor, timeout)
        path = _path(config, files.LOCK_FILE)
        path.unlink()
        path.touch(mode=0o600)

    monkeypatch.setattr(files, "_acquire", replace_lock)
    _unavailable(store.begin_login)
    assert _path(config).read_bytes() == prior


def test_state_replacement_before_commit_is_detected(store, config, monkeypatch):
    _sign_in(store, "old-token")
    ticket = store.begin_login()
    original = files.os.fsync
    replacement_bytes = json.dumps({**_disk(config), "credential": None}).encode()

    def replace_state(descriptor):
        original(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            replacement = _path(config, "external-state")
            replacement.write_bytes(replacement_bytes)
            replacement.chmod(0o600)
            os.replace(replacement, _path(config))

    monkeypatch.setattr(files.os, "fsync", replace_state)
    _unavailable(lambda: store.finish_login(ticket, _credential("new-token")))
    assert _path(config).read_bytes() == replacement_bytes
    assert not list(Path(config["credential_directory"]).glob(".session-*.tmp"))


def _late_finish(config, ticket, ready, release, result):
    store = CredentialStore(config)
    store._clock = lambda: 1000.0
    ready.set()
    if not release.wait(5):
        result.put("timed_out")
        return
    try:
        store.finish_login(ticket, _credential("late-process"))
        result.put("unexpected_success")
    except HubError as error:
        result.put(error.code)


def test_logout_in_other_process_fences_a_login_already_waiting_on_network(store, config):
    ticket = store.begin_login()
    context = multiprocessing.get_context("fork")
    ready, release, result = context.Event(), context.Event(), context.Queue()
    child = context.Process(target=_late_finish, args=(config, ticket, ready, release, result))
    child.start()
    try:
        assert ready.wait(5)
        store.logout()
        release.set()
        assert result.get(timeout=5) == "user_login_superseded"
        child.join(timeout=5)
        assert child.exitcode == 0
        assert store.status() == {"authenticated": False, "pending": False}
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)
        result.close()


def _concurrent_begin(config, start, result):
    if not start.wait(5):
        result.put("timeout")
        return
    result.put(CredentialStore(config).begin_login())


def test_concurrent_cli_logins_share_one_lock_and_only_one_ticket_can_complete(store, config):
    context = multiprocessing.get_context("fork")
    start, result = context.Event(), context.Queue()
    children = [context.Process(target=_concurrent_begin, args=(config, start, result))
                for _ in range(4)]
    for child in children:
        child.start()
    try:
        start.set()
        tickets = [result.get(timeout=5) for _ in children]
        assert len(set(tickets)) == 4
        current = _disk(config)["pending"]
        assert current in tickets
        for ticket in tickets:
            if ticket == current:
                continue
            with pytest.raises(HubError) as stale:
                store.finish_login(ticket, _credential("stale"))
            assert stale.value.code == "user_login_superseded"
        store.finish_login(current, _credential("winning-login"))
        assert store.token() == "winning-login"
    finally:
        for child in children:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
            assert child.exitcode == 0
        result.close()


def test_provider_is_offline_and_observes_other_clients_login_logout_and_expiry(config, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    provider = UserTokenProvider.from_file(path)
    assert provider.hub_url == "https://hub.test:443"
    assert not Path(config["credential_directory"]).exists()
    provider._store._clock = lambda: 1000.0
    writer = CredentialStore(config)
    writer._clock = lambda: 1000.0
    _sign_in(writer, "first")
    assert provider() == "first"
    _sign_in(writer, "second")
    provider.invalidate("first")
    assert provider() == "second"
    writer.logout()
    with pytest.raises(HubError, match="sign in"):
        provider()
    assert "first" not in repr(provider)
    with pytest.raises(ValueError):
        UserTokenProvider.from_file(path, hub_url="https://wrong-hub.test")


@pytest.mark.parametrize("timeout", [0, -1, 121, True, float("inf"), float("nan"), 10 ** 400])
def test_provider_rejects_invalid_timeout_before_opening_config(timeout):
    with pytest.raises(ValueError, match="user credential timeout is invalid"):
        UserTokenProvider.from_file("/must-not-open", timeout=timeout)


def test_missing_posix_capability_fails_with_fixed_error(store, monkeypatch):
    monkeypatch.delattr(files.os, "O_NOFOLLOW")
    _unavailable(store.status)
