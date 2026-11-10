"""Owned, single-link SQLite families and stable cooperative writer guards."""
from __future__ import annotations

from contextlib import closing
import os
import sqlite3

import pytest

from pbatch.hub_store import HubStore, HubStoreError
from test_hub_store_admission import legacy_database

SUFFIXES = ['', '.lock', '-wal', '-shm', '-journal']


def deny_native(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('SQLite must not see an unadmitted storage path')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)


def create_unsafe(path, kind, target):
    if kind == 'symlink':
        path.symlink_to(target)
    elif kind == 'hardlink':
        os.link(target, path)
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path, 0o600)


@pytest.mark.parametrize('suffix', SUFFIXES, ids=['database', 'lock', 'wal', 'shm', 'journal'])
@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'fifo'])
def test_unsafe_family_is_refused_before_sqlite_or_metadata_mutation(tmp_path, monkeypatch, suffix, kind):
    path, target = tmp_path / 'hub.sqlite3', tmp_path / 'owned-target'
    target.write_bytes(b'owned protected content')
    target.chmod(0o640)
    if suffix:
        legacy_database(path)
    create_unsafe(tmp_path / ('hub.sqlite3' + suffix), kind, target)
    deny_native(monkeypatch)
    with pytest.raises((HubStoreError, ValueError)):
        HubStore(path)
    assert target.read_bytes() == b'owned protected content'
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_orphan_sidecar_does_not_create_new_database(tmp_path, monkeypatch, suffix):
    path, sidecar = tmp_path / 'hub.sqlite3', tmp_path / ('hub.sqlite3' + suffix)
    sidecar.write_bytes(b'owned retained sidecar')
    sidecar.chmod(0o600)
    deny_native(monkeypatch)
    with pytest.raises((HubStoreError, ValueError)):
        HubStore(path)
    assert not path.exists() and sidecar.read_bytes() == b'owned retained sidecar'


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_public_sidecar_is_not_implicitly_adopted_or_chmodded(tmp_path, monkeypatch, suffix):
    path, sidecar = tmp_path / 'hub.sqlite3', tmp_path / ('hub.sqlite3' + suffix)
    legacy_database(path)
    sidecar.write_bytes(b'owned sidecar')
    sidecar.chmod(0o640)
    deny_native(monkeypatch)
    with pytest.raises((HubStoreError, ValueError)):
        HubStore(path)
    assert sidecar.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('suffix', ['', '.lock'], ids=['database', 'lock'])
def test_replacement_between_stat_and_open_is_not_admitted(tmp_path, monkeypatch, suffix):
    path = tmp_path / 'hub.sqlite3'
    legacy_database(path)
    lock = tmp_path / 'hub.sqlite3.lock'
    lock.touch(mode=0o600)
    chosen, replacement = tmp_path / ('hub.sqlite3' + suffix), tmp_path / 'replacement'
    replacement.write_bytes(b'' if suffix else b'owned not a database')
    replacement.chmod(0o640)
    original_open, swapped = os.open, []
    def swap(name, *args, **kwargs):
        if str(name) == str(chosen) and not swapped:
            os.replace(replacement, chosen)
            swapped.append(True)
        return original_open(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', swap)
        deny_native(patch)
        with pytest.raises((HubStoreError, ValueError)):
            HubStore(path)
    assert swapped and chosen.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('mode', [0o777, 0o770])
def test_writable_shared_parent_is_refused_without_restricting_it(tmp_path, monkeypatch, mode):
    parent = tmp_path / 'shared'
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    deny_native(monkeypatch)
    with pytest.raises((HubStoreError, ValueError)):
        HubStore(parent / 'hub.sqlite3')
    assert parent.stat().st_mode & 0o777 == mode
    assert list(parent.iterdir()) == []


def test_unsafe_ancestor_cannot_hide_behind_a_private_leaf_directory(tmp_path, monkeypatch):
    ancestor, leaf = tmp_path / 'shared', tmp_path / 'shared' / 'private'
    leaf.mkdir(parents=True, mode=0o700)
    ancestor.chmod(0o777)
    deny_native(monkeypatch)
    with pytest.raises(HubStoreError):
        HubStore(leaf / 'hub.sqlite3')
    assert list(leaf.iterdir()) == [] and ancestor.stat().st_mode & 0o777 == 0o777


@pytest.mark.parametrize('suffix', ['x' * 4097, '汉' * 1400, '/'.join(['nested'] * 65)],
                         ids=['characters', 'utf8-bytes', 'depth'])
def test_path_budget_is_checked_before_resolution_or_file_creation(tmp_path, monkeypatch, suffix):
    from pathlib import Path
    def forbidden(*_args, **_kwargs):
        raise AssertionError('over-budget path must not be resolved')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'resolve', forbidden)
        with pytest.raises(ValueError, match='storage admission limits'):
            HubStore(tmp_path / suffix)
    assert list(tmp_path.iterdir()) == []


def test_untrusted_ancestor_is_refused_before_creating_missing_children(tmp_path):
    ancestor = tmp_path / 'shared'
    ancestor.mkdir()
    ancestor.chmod(0o777)
    with pytest.raises(HubStoreError):
        HubStore(ancestor / 'must-not-create' / 'hub.sqlite3')
    assert list(ancestor.iterdir()) == []


def test_parent_alias_uses_same_permanent_writer_guard(tmp_path):
    directory, alias = tmp_path / 'private', tmp_path / 'alias'
    directory.mkdir(mode=0o700)
    alias.symlink_to(directory, target_is_directory=True)
    with closing(HubStore(directory / 'hub.sqlite3')):
        with pytest.raises(HubStoreError, match='another HubStore'):
            HubStore(alias / 'hub.sqlite3')
    with closing(HubStore(alias / 'hub.sqlite3')) as reopened:
        assert reopened.path == directory / 'hub.sqlite3'


def test_replaced_database_cannot_redirect_a_live_store(tmp_path):
    path, replacement = tmp_path / 'hub.sqlite3', tmp_path / 'replacement.sqlite3'
    store = HubStore(path)
    with closing(HubStore(replacement)) as other:
        other.register_instance('owned', 'replacement', {'boot_id': 'different'})
    expected = replacement.read_bytes()
    os.replace(replacement, path)
    try:
        with pytest.raises(HubStoreError):
            store.instances('owned')
    finally:
        store.close()
    assert path.read_bytes() == expected
    with closing(HubStore(path)) as reopened:
        assert reopened.instances('owned')[0]['instance_id'] == 'replacement'


def test_replaced_guard_blocks_new_connections_without_touching_replacement(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    store = HubStore(path)
    lock, replacement = tmp_path / 'hub.sqlite3.lock', tmp_path / 'new-lock'
    replacement.write_bytes(b'owned replacement guard')
    replacement.chmod(0o640)
    os.replace(replacement, lock)
    try:
        with pytest.raises(HubStoreError):
            store.instances('owned')
    finally:
        store.close()
    assert lock.read_bytes() == b'owned replacement guard'
    assert lock.stat().st_mode & 0o777 == 0o640


def test_path_replacement_during_sqlite_open_is_closed_before_any_pragmas(tmp_path, monkeypatch):
    path, replacement = tmp_path / 'hub.sqlite3', tmp_path / 'replacement.sqlite3'
    store = HubStore(path)
    legacy_database(replacement, 42)
    replacement.chmod(0o600)
    expected, original_connect, opened = replacement.read_bytes(), sqlite3.connect, []
    def swapped(*args, **kwargs):
        os.replace(replacement, path)
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', swapped)
            with pytest.raises(HubStoreError):
                store._connect()
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
    finally:
        for connection in opened:
            connection.close()
        store.close()
    assert path.read_bytes() == expected


@pytest.mark.parametrize('capability', ['O_NOFOLLOW', 'O_NONBLOCK', 'getuid', 'fchmod'])
def test_missing_safe_storage_primitives_fail_before_directory_creation(tmp_path, monkeypatch, capability):
    parent = tmp_path / 'must-not-create'
    with monkeypatch.context() as patch:
        patch.delattr(os, capability)
        with pytest.raises(ValueError, match='requires POSIX'):
            HubStore(parent / 'hub.sqlite3')
    assert not parent.exists()


@pytest.mark.parametrize('suffix', SUFFIXES, ids=['database', 'lock', 'wal', 'shm', 'journal'])
def test_foreign_file_owner_is_rejected_before_native_open(tmp_path, monkeypatch, suffix):
    from pathlib import Path
    path, target = tmp_path / 'hub.sqlite3', tmp_path / ('hub.sqlite3' + suffix)
    legacy_database(path)
    if suffix:
        target.touch(mode=0o600)
    original_stat = Path.lstat
    def foreign(file, *args, **kwargs):
        info = original_stat(file, *args, **kwargs)
        if file == target:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', foreign)
        deny_native(patch)
        with pytest.raises(HubStoreError):
            HubStore(path)


def test_disappearing_database_is_not_recreated_by_a_runtime_connection(tmp_path, monkeypatch):
    path, detached = tmp_path / 'hub.sqlite3', tmp_path / 'detached-owned.sqlite3'
    store = HubStore(path)
    before, original_connect = path.read_bytes(), sqlite3.connect
    def moved(*args, **kwargs):
        os.replace(path, detached)
        return original_connect(*args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', moved)
            with pytest.raises((sqlite3.Error, HubStoreError)):
                store._connect()
        assert not path.exists() and detached.read_bytes() == before
    finally:
        store.close()


def test_lost_privacy_blocks_new_connections_without_automatic_permission_repair(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    store = HubStore(path)
    path.chmod(0o644)
    try:
        with pytest.raises(HubStoreError):
            store.instances('owned')
        assert path.stat().st_mode & 0o777 == 0o644
    finally:
        store.close()
