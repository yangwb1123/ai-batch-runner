"""Storage admission must precede mutations; core initialization is one transaction."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from pbatch import file_lock
from pbatch import hub_store as module
from pbatch.hub_store import HubStore, HubStoreError


def schema(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()


def legacy_database(path, version=0):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(module._SCHEMA)
        connection.execute(f'PRAGMA user_version={version}')
        connection.execute("INSERT INTO hub_instances VALUES('owned','alpha','boot',7,100,'{}')")
        connection.commit()


@pytest.mark.parametrize('version', [2, 42, 2147483647])
def test_unsupported_version_is_refused_without_schema_journal_or_permission_changes(tmp_path, version):
    path = tmp_path / 'hub.sqlite3'
    legacy_database(path, version)
    path.chmod(0o640)
    before, tables = path.read_bytes(), schema(path)
    with pytest.raises(ValueError, match='unsupported HubStore schema version'):
        HubStore(path)
    assert path.read_bytes() == before and schema(path) == tables
    assert path.stat().st_mode & 0o777 == 0o640
    assert not any(tmp_path.glob('hub.sqlite3-*'))
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert connection.execute('PRAGMA user_version').fetchone()[0] == version
    with closing(os.fdopen(os.open(str(path) + '.lock', os.O_RDWR), 'rb')) as lock:
        file_lock.flock(lock.fileno(), file_lock.LOCK_EX | file_lock.LOCK_NB)


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('fragment', ['_SCHEMA', '_SESSION_REQUEST_SCHEMA', '_SESSION_CLOSE_SCHEMA', '_OPERATIONS_SCHEMA'])
def test_initialization_failure_rolls_back_every_core_schema_fragment(tmp_path, monkeypatch, existing, fragment):
    path = tmp_path / 'hub.sqlite3'
    if existing:
        legacy_database(path)
    else:
        with closing(sqlite3.connect(path)):
            pass
    before = schema(path)
    invalid = getattr(module, fragment) + '\nCREATE TABLE owned_partial(value TEXT);\nINVALID OWNED SQL;'
    with monkeypatch.context() as patch:
        patch.setattr(module, fragment, invalid)
        with pytest.raises(sqlite3.Error):
            HubStore(path)
    assert schema(path) == before
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 0
        if existing:
            assert connection.execute('SELECT generation FROM hub_instances').fetchone()[0] == 7
    with closing(HubStore(path)) as store:
        with store._connect() as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


def test_auth_schema_is_in_the_same_initialization_transaction(tmp_path, monkeypatch):
    from pbatch import hub_compute_models
    path = tmp_path / 'hub.sqlite3'
    with monkeypatch.context() as patch:
        patch.setattr(hub_compute_models, 'AUTH_SCHEMA', 'CREATE TABLE owned_partial(x); INVALID OWNED SQL;')
        with pytest.raises(sqlite3.Error):
            HubStore(path)
    assert schema(path) == []
    with closing(HubStore(path)) as store:
        with store._connect() as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.mark.parametrize('version', [0, 1])
def test_supported_legacy_schema_keeps_rows_and_gains_atomic_upgrade(tmp_path, version):
    path = tmp_path / 'hub.sqlite3'
    legacy_database(path, version)
    path.chmod(0o640)
    with closing(HubStore(path)) as store:
        with store._connect() as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
            assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert connection.execute('PRAGMA synchronous').fetchone()[0] == 2
        assert store.instances('owned')[0]['generation'] == 7
        assert store.session_operations('owned') == {'data': [], 'next_cursor': ''}
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('existing', [False, True])
def test_competing_owner_cannot_create_database_or_restrict_another_owners_permissions(tmp_path, existing):
    path, lock_path = tmp_path / 'hub.sqlite3', tmp_path / 'hub.sqlite3.lock'
    if existing:
        legacy_database(path)
        path.chmod(0o640)
    lock_path.touch(mode=0o640)
    lock_path.chmod(0o640)
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        file_lock.flock(descriptor, file_lock.LOCK_EX | file_lock.LOCK_NB)
        with pytest.raises(HubStoreError, match='another HubStore'):
            HubStore(path)
        assert path.exists() is existing
        if existing:
            assert path.stat().st_mode & 0o777 == 0o640
        assert lock_path.stat().st_mode & 0o777 == 0o640
    finally:
        os.close(descriptor)


def test_native_lock_failure_closes_its_owned_descriptor(tmp_path, monkeypatch):
    path, opened = tmp_path / 'hub.sqlite3', []
    original_open, original_flock = os.open, file_lock.flock
    def record(name, *args, **kwargs):
        descriptor = original_open(name, *args, **kwargs)
        if str(name) == str(path) + '.lock':
            info = os.fstat(descriptor)
            opened.append((descriptor, (info.st_dev, info.st_ino)))
        return descriptor
    def unavailable(descriptor, operation):
        if operation & file_lock.LOCK_EX:
            raise OSError('owned lock backend unavailable')
        return original_flock(descriptor, operation)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'open', record)
            patch.setattr(file_lock, 'flock', unavailable)
            with pytest.raises(OSError, match='owned lock backend unavailable'):
                HubStore(path)
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0][0])
        assert not path.exists()
    finally:
        for descriptor, identity in opened:
            try:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) == identity:
                    os.close(descriptor)
            except OSError:
                pass


def test_invalid_database_is_not_chmodded_or_replaced(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    path.write_bytes(b'owned non-database content\n')
    path.chmod(0o640)
    with pytest.raises(sqlite3.Error):
        HubStore(path)
    assert path.read_bytes() == b'owned non-database content\n'
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize('version', [0, 42])
def test_crashed_wal_version_is_checked_from_sqlite_view_not_stale_main_file(tmp_path, version):
    path = tmp_path / 'hub.sqlite3'
    script = '''
import os, sqlite3, sys
from pbatch.hub_store_helpers import _SCHEMA
fd = os.open(sys.argv[1], os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
os.close(fd)
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.executescript(_SCHEMA)
conn.execute('PRAGMA user_version=' + sys.argv[2])
conn.execute("INSERT INTO hub_instances VALUES('owned','alpha','boot',7,100,'{}')")
conn.commit()
os._exit(0)
'''
    result = subprocess.run([sys.executable, '-c', script, str(path), str(version)],
                            capture_output=True, timeout=5)
    assert result.returncode == 0
    wal = tmp_path / 'hub.sqlite3-wal'
    before, wal_before = path.read_bytes(), wal.read_bytes()
    assert wal_before and int.from_bytes(before[60:64], 'big') == 0
    if version == 42:
        with pytest.raises(ValueError, match='unsupported HubStore schema version'):
            HubStore(path)
        assert path.read_bytes() == before and wal.read_bytes() == wal_before
    else:
        with closing(HubStore(path)) as store:
            assert store.instances('owned')[0]['generation'] == 7
            with store._connect() as connection:
                assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.mark.parametrize('name', ['hub ?#%.sqlite3', 'owned-雪.sqlite3'])
def test_readonly_probe_escapes_uri_names_and_reads_the_exact_owned_database(tmp_path, name):
    path = tmp_path / name
    legacy_database(path, 42)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='unsupported HubStore schema version'):
        HubStore(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('check_only', [True, False])
def test_cli_separates_offline_config_check_from_storage_and_stops_before_listener(tmp_path, monkeypatch, capsys, check_only):
    from pbatch import hub_http, hub_compute, hub_integrations
    from test_hub_server_signals import settings
    path, config = tmp_path / 'hub.sqlite3', tmp_path / 'hub.json'
    legacy_database(path, 42)
    config.write_text(json.dumps(settings()))
    config.chmod(0o600)
    before = path.read_bytes()
    def forbidden(*_args, **_kwargs):
        raise AssertionError('no listener or worker before storage admission')
    monkeypatch.setattr(hub_http, 'HubHTTPServer', forbidden)
    monkeypatch.setattr(hub_compute, 'HubCompute', forbidden)
    monkeypatch.setattr(hub_integrations, 'HubIntegrations', forbidden)
    args = ['--config', str(config), '--database', str(path), '--host', '127.0.0.1']
    if check_only:
        assert hub_http.hub_serve_main(args + ['--check-config', '--json']) == 0
        report = json.loads(capsys.readouterr().out)
        assert report['result'] == 'passed'
        assert report['external_acceptance']['result'] == 'not_executed'
        assert not (tmp_path / 'hub.sqlite3.lock').exists()
    else:
        with pytest.raises(SystemExit) as failure:
            hub_http.hub_serve_main(args)
        assert failure.value.code == 2
        assert 'unsupported HubStore schema version' in capsys.readouterr().err
    assert path.read_bytes() == before
