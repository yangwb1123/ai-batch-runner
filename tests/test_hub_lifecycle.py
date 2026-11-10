"""Owned HTTP transports and SQLite connections must drain before ownership ends."""
from __future__ import annotations

from contextlib import closing
import hashlib
import socket
import sqlite3
import threading

import pytest

from pbatch.hub_auth import HubAuth
from pbatch.hub_http import HubHTTPServer, HubHandler
from pbatch.hub_store import HubStore, HubStoreError


def local_auth():
    return HubAuth({'mode': 'development', 'grants': [{
        'tenant_id': 'fixture', 'subject': 'reader', 'client_id': 'fixture', 'kind': 'user',
        'scopes': ['agent.instances:read'], 'instances': ['alpha'],
        'token_sha256': hashlib.sha256(b'synthetic-only').hexdigest()}]})


def assert_owned(path):
    with pytest.raises(HubStoreError, match='owns'):
        with closing(HubStore(path)):
            pass


def test_close_keeps_owner_until_last_sqlite_connection_closes(tmp_path):
    store = HubStore(tmp_path / 'hub.sqlite3')
    first, second = store._connect(), store._connect()
    try:
        store.close()
        with pytest.raises(HubStoreError, match='closed'):
            store._connect()
        assert_owned(store.path)
        first.close()
        assert_owned(store.path)
    finally:
        first.close()
        second.close()
        store.close()
    reopened = HubStore(store.path)
    reopened.close()


def test_sqlite_context_exit_closes_the_owned_connection(tmp_path):
    store = HubStore(tmp_path / 'hub.sqlite3')
    with store._connect() as connection:
        assert connection.execute('SELECT 1').fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    store.close()
    reopened = HubStore(store.path)
    reopened.close()


def test_completion_requires_closed_store_with_no_admitted_connections(tmp_path):
    store = HubStore(tmp_path / 'hub.sqlite3')
    connection = store._connect()
    try:
        with pytest.raises(HubStoreError, match='closure'):
            store.require_drained()
        store.close()
        with pytest.raises(HubStoreError, match='closure'):
            store.require_drained()
    finally:
        connection.close()
        store.close()
    store.require_drained()


def test_failed_connection_open_does_not_leak_an_owner(tmp_path, monkeypatch):
    store = HubStore(tmp_path / 'hub.sqlite3')
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic unavailable connection')
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', unavailable)
        with pytest.raises(sqlite3.OperationalError):
            store._connect()
    store.close()
    reopened = HubStore(store.path)
    reopened.close()


def test_failed_cross_thread_close_does_not_release_the_owner(tmp_path):
    store = HubStore(tmp_path / 'hub.sqlite3')
    connection = store._connect()
    errors = []
    def wrong_thread():
        try:
            connection.close()
        except sqlite3.ProgrammingError as exc:
            errors.append(type(exc))
    worker = threading.Thread(target=wrong_thread)
    try:
        store.close()
        worker.start()
        worker.join(2)
        assert errors == [sqlite3.ProgrammingError]
        assert_owned(store.path)
    finally:
        connection.close()
        store.close()
    reopened = HubStore(store.path)
    reopened.close()


@pytest.fixture
def pending_request(tmp_path):
    store = HubStore(tmp_path / 'hub.sqlite3')
    store.register_instance('fixture', 'alpha', {'boot_id': 'boot', 'name': 'before'})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Handler(HubHandler):
        def do_GET(self):
            try:
                with store._write() as connection:
                    connection.execute('UPDATE hub_instances SET metadata_json=?',
                                       ('{"name":"committed-after-stop"}',))
                    entered.set()
                    assert release.wait(5), 'test did not release its own transaction'
            finally:
                finished.set()
    server = HubHTTPServer(('127.0.0.1', 0), store, local_auth())
    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    peer = socket.create_connection(server.server_address, timeout=2)
    try:
        peer.sendall(b'GET /healthz HTTP/1.0\r\n\r\n')
        assert entered.wait(2)
        server.shutdown()
        yield store, server, release, finished
    finally:
        release.set()
        peer.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
        store.close()
        assert finished.wait(2)


def test_server_close_waits_for_accepted_work_before_store_close(pending_request):
    store, server, release, _ = pending_request
    started, closed, errors = threading.Event(), threading.Event(), []
    def stop():
        started.set()
        try:
            server.server_close()
            store.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            closed.set()
    worker = threading.Thread(target=stop)
    worker.start()
    try:
        assert started.wait(1)
        assert not closed.wait(0.05), 'close returned while the HTTP transaction was still active'
    finally:
        release.set()
        worker.join(3)
    assert closed.is_set() and not errors
    reopened = HubStore(store.path)
    try:
        assert reopened.instances('fixture')[0]['name'] == 'committed-after-stop'
    finally:
        reopened.close()


def test_drain_deadline_reports_failure_and_retains_database_owner(pending_request):
    store, server, release, finished = pending_request
    with pytest.raises(RuntimeError, match='drain'):
        server.server_close(drain_timeout=0.05)
    store.close()
    assert_owned(store.path)
    release.set()
    assert finished.wait(2)
    server.server_close()
    reopened = HubStore(store.path)
    try:
        # Lost response/stop is NOT rollback: an already accepted write can still commit.
        assert reopened.instances('fixture')[0]['name'] == 'committed-after-stop'
    finally:
        reopened.close()


def test_transport_cannot_close_and_reuse_its_fd_during_drain_shutdown():
    from pbatch.hub_server_lifecycle import RequestDrain
    entered, release, closed, closing = [threading.Event() for _ in range(4)]
    class Transport:
        def shutdown(self, _how):
            entered.set()
            assert release.wait(2)
        def close(self):
            closed.set()
    server = HubHTTPServer.__new__(HubHTTPServer)
    server._requests = RequestDrain()
    request = Transport()
    assert server._requests.admit(request)
    def close():
        closing.set()
        server.close_request(request)
    stopper = threading.Thread(target=server._requests.stop)
    closer = threading.Thread(target=close)
    stopper.start()
    try:
        assert entered.wait(1)
        closer.start()
        assert closing.wait(1)
        assert not closed.wait(0.05), 'owned fd could be reused during shutdown'
    finally:
        release.set()
        stopper.join(2)
        if closer.ident is not None:
            closer.join(2)
    assert closed.is_set() and not stopper.is_alive() and not closer.is_alive()


def test_shutdown_interrupts_only_owned_partial_http_transports(tmp_path):
    store = HubStore(tmp_path / 'hub.sqlite3')
    admitted = threading.Event()
    class Handler(HubHandler):
        def setup(self):
            super().setup()
            admitted.set()
    server = HubHTTPServer(('127.0.0.1', 0), store, local_auth())
    server.RequestHandlerClass = Handler
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    peer = socket.create_connection(server.server_address, timeout=2)
    unrelated, other_end = socket.socketpair()
    try:
        peer.sendall(b'GET /healthz HTTP/1.1\r\nHost:')
        assert admitted.wait(2)
        server.shutdown()
        server.server_close(drain_timeout=0.5)
        unrelated.sendall(b'still-open')
        assert other_end.recv(10) == b'still-open'
    finally:
        peer.close()
        unrelated.close()
        other_end.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
        store.close()
