"""Actual owned CLI processes: stop/restart without a real identity, provider or project."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from pbatch.hub_client import HubClient
from pbatch.hub_server_lifecycle import (RequestDrain, drain_deadline,
                                         serve_until_stopped, termination_signals)

ROOT = Path(__file__).resolve().parents[1]


def test_signal_requests_stop_without_throwing_and_restores_handlers():
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    class Server:
        timeout = 7
        calls = 0
        def handle_request(self):
            self.calls += 1
            assert self.timeout == 0.25
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
    server = Server()
    with termination_signals() as stop:
        assert signal.getsignal(signal.SIGTERM) == stop.request
        serve_until_stopped(server, stop)
        assert stop.number == signal.SIGTERM and server.calls == 1
    assert server.timeout == 7
    assert {number: signal.getsignal(number) for number in previous} == previous


def test_signal_context_does_not_overwrite_another_handler():
    original = signal.getsignal(signal.SIGTERM)
    replacement = lambda *_: None
    try:
        with termination_signals():
            signal.signal(signal.SIGTERM, replacement)
        assert signal.getsignal(signal.SIGTERM) is replacement
    finally:
        signal.signal(signal.SIGTERM, original)


def test_signal_context_rejects_background_thread():
    errors = []
    def run():
        try:
            with termination_signals():
                pytest.fail('worker installed process-global handlers')
        except ValueError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(2)
    assert len(errors) == 1 and 'main thread' in errors[0]


@pytest.mark.parametrize('timeout', [False, 0, -1, 31, float('nan'), float('inf'), '1'])
def test_invalid_drain_budget_rejected(timeout):
    with pytest.raises(ValueError):
        drain_deadline(timeout)


def test_admission_keeps_original_32_request_bound_and_never_reopens():
    class Transport:
        stopped = 0
        def shutdown(self, how):
            assert how == socket.SHUT_RDWR
            self.stopped += 1
    drain = RequestDrain()
    admitted = [Transport() for _ in range(32)]
    outsider = Transport()
    assert all(drain.admit(item) for item in admitted)
    assert not drain.admit(admitted[0]) and not drain.admit(outsider)
    drain.stop()
    assert all(item.stopped == 1 for item in admitted) and outsider.stopped == 0
    assert not drain.admit(outsider)
    with pytest.raises(RuntimeError, match='drain'):
        drain.wait(time.monotonic() + 0.01)
    for item in admitted:
        drain.complete(item)
    drain.wait(time.monotonic() + 1)
    assert not drain.admit(outsider)


@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_listener_failure_starts_no_workers_and_cleanup_always_closes_store(tmp_path, monkeypatch,
                                                                            cleanup_failure):
    from types import SimpleNamespace
    from pbatch import hub_compute, hub_http, hub_integrations
    from pbatch.hub_store import HubStore
    from test_hub_lifecycle import local_auth
    events = []
    class Component:
        def __init__(self, store, **_):
            self.store = store
        def start(self):
            pytest.fail('workers started before listener admission')
        def close(self):
            events.append(self.__class__.__name__)
    class Compute(Component):
        def close(self):
            super().close()
            if cleanup_failure:
                raise RuntimeError('synthetic close failure')
    def unavailable(*args, **kwargs):
        raise OSError('synthetic listener unavailable')
    monkeypatch.setattr(hub_compute, 'HubCompute', Compute)
    monkeypatch.setattr(hub_integrations, 'HubIntegrations', Component)
    monkeypatch.setattr(hub_http, 'HubHTTPServer', unavailable)
    prepared = SimpleNamespace(compute={}, sinks=[], identity=None, web=None)
    path = tmp_path / 'hub.sqlite3'
    with pytest.raises((OSError, RuntimeError), match='synthetic'):
        hub_http._run_server(SimpleNamespace(host='127.0.0.1', port=8788), path, {},
                             local_auth(), None, prepared)
    assert events == ['Compute', 'Component']
    reopened = HubStore(path)
    reopened.close()
    reopened.require_drained()


def test_failed_request_worker_start_does_not_leak_admission(tmp_path, monkeypatch):
    from pbatch.hub_http import HubHTTPServer
    from pbatch.hub_store import HubStore
    from test_hub_lifecycle import local_auth
    store = HubStore(tmp_path / 'hub.sqlite3')
    server = HubHTTPServer(('127.0.0.1', 0), store, local_auth())
    request, peer = socket.socketpair()
    def fail(_self):
        raise RuntimeError('synthetic worker start failure')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', fail)
            with pytest.raises(RuntimeError, match='synthetic'):
                server.process_request(request, ('127.0.0.1', 1))
        server.server_close(drain_timeout=0.2)
    finally:
        request.close()
        peer.close()
        server.server_close()
        store.close()


def settings():
    def grant(kind, token, scopes):
        return {'tenant_id': 'fixture', 'subject': token, 'client_id': 'fixture', 'kind': kind,
                'instances': ['alpha'], 'scopes': scopes,
                'token_sha256': hashlib.sha256(token.encode()).hexdigest()}
    return {'auth': {'mode': 'development', 'grants': [
        grant('gateway', 'fixture-machine', ['agent.gateway:connect']),
        grant('user', 'fixture-reader', ['agent.instances:read', 'agent.sessions:read',
                                        'agent.sessions:write'])]}}


def ready(process):
    deadline = time.monotonic() + 10
    output = b''
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline and len(output) < 4096:
            if not selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                continue
            chunk = os.read(process.stdout.fileno(), 1024)
            if not chunk:
                break
            output += chunk
            if b'Agent Hub listening on http://127.0.0.1:' in output and b'\n' in output:
                return
    pytest.fail('owned Hub subprocess did not report readiness within its fixed window')


@contextmanager
def hub_process(tmp_path):
    config = tmp_path / 'hub.json'
    config.write_text(json.dumps(settings()))
    config.chmod(0o600)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith('PBATCH_')}
    env['PYTHONPATH'] = str(ROOT)
    process = subprocess.Popen(
        [sys.executable, '-m', 'pbatch', 'fleet', 'serve', '--config', str(config),
         '--database', str(tmp_path / 'hub.sqlite3'), '--host', '127.0.0.1', '--port', str(port)],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready(process)
        yield process, f'http://127.0.0.1:{port}'
    finally:
        if process.poll() is None:
            process.kill()  # Only this fixture's exact owned Popen, never an existing service.
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX signal acceptance; not Windows process termination')
@pytest.mark.parametrize('stop_signal', [signal.SIGTERM, signal.SIGINT])
def test_cli_clean_stop_preserves_accepted_requests_without_executing_them(tmp_path, stop_signal):
    with hub_process(tmp_path) as (process, origin):
        machine = HubClient(origin, 'fixture-machine')
        machine.register_instance(instance_id='alpha', boot_id='fixture-boot', name='fixed project',
                                  project_id='fixture-project', session_capacity=1)
        reader = HubClient(origin, 'fixture-reader')
        accepted = reader.create_session('alpha', 'pending', idempotency_key='same-request')
        assert accepted['status'] == 'queued'
        process.send_signal(stop_signal)
        output, error = process.communicate(timeout=8)
        assert process.returncode == 0, (process.returncode, output[:2048], error[:2048])
        assert b'Traceback' not in error
    with hub_process(tmp_path) as (process, origin):
        reader = HubClient(origin, 'fixture-reader')
        assert reader.list_instances()[0]['name'] == 'fixed project'
        repeated = reader.create_session('alpha', 'pending', idempotency_key='same-request')
        assert repeated['request_id'] == accepted['request_id']
        assert repeated['status'] == 'queued' and reader.list_sessions() == []
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=8)
        assert process.returncode == 0
