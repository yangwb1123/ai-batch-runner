import base64
import json
import threading
import time
from dataclasses import replace

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_file_snapshot_models import VERIFICATION, file_metadata
from pbatch.hub_gateway import HubGateway
from pbatch.hub_gateway_files import bounded_capture
from pbatch.process_exec import ValidationResult
from tests.test_hub_gateway import _FakeClient, _FakeSession, _command, _options, _wait_for


class FileClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.uploads = []
        self.failure = None

    def publish_file_snapshots(self, body):
        self.uploads.append(json.loads(json.dumps(body)))
        if self.failure:
            raise self.failure
        fields = ('schema', 'instance_id', 'session_id', 'turn_id', 'snapshot_set_id')
        return {**{key: body[key] for key in fields}, 'verification': VERIFICATION,
                'files': [file_metadata(row) for row in body['files']],
                'unavailable': body['unavailable']}


@pytest.fixture
def owner(tmp_path):
    (tmp_path / 'out.txt').write_text('original bytes')
    client, session = FileClient(), _FakeSession()
    gateway = HubGateway(_options(tmp_path, share_files=('out.txt',)), client=client,
                         session_factory=lambda options: session)
    gateway.start()
    try:
        yield gateway, client, session, tmp_path
    finally:
        gateway.close()


def execute(owner):
    gateway, client, session, _ = owner
    gateway._dispatch_command(_command(session))
    gateway._retry_pending_start()
    _wait_for(lambda: session.calls == 1 and not gateway._turn_threads)


def test_capture_durably_precedes_upload_and_requires_terminal_ack(owner):
    gateway, client, _, _ = owner
    execute(owner)
    receipt = gateway._files.queue.receipt('hub-turn')
    assert receipt['state'] == 'pending' and client.uploads == []
    gateway._files.flush()
    assert client.uploads == []
    gateway._flush_outbox()
    gateway._files.flush()
    assert len(client.uploads) == 1
    row = client.uploads[0]['files'][0]
    assert base64.b64decode(row['content_b64']) == b'original bytes'
    assert gateway._files.queue.receipt('hub-turn')['state'] == 'acknowledged'


def test_unknown_upload_retries_same_ids_and_original_bytes(owner):
    gateway, client, session, workspace = owner
    execute(owner)
    gateway._flush_outbox()
    client.failure = HubError('unknown', code='hub_unavailable')
    gateway._files.flush()
    (workspace / 'out.txt').write_text('changed after capture')
    client.failure = None
    gateway._files.flush()
    assert client.uploads[0] == client.uploads[1]
    assert session.calls == 1
    assert gateway._store.turn('hub-turn')['state'] == 'completed'


def test_definite_upload_rejection_preserves_task_and_bounded_receipt(owner):
    gateway, client, session, _ = owner
    execute(owner)
    gateway._flush_outbox()
    client.failure = HubError('policy rejected', code='snapshot_invalid', status=400)
    gateway._files.flush()
    gateway._files.flush()
    receipt = gateway._files.queue.receipt('hub-turn')
    assert receipt['state'] == 'rejected' and receipt['body'] == ''
    assert receipt['error_code'] == 'upload_rejected' and len(client.uploads) == 1
    assert gateway._store.turn('hub-turn')['state'] == 'completed' and session.calls == 1


@pytest.mark.parametrize('terminal', ['turn.failed', 'turn.interrupted', None])
def test_failed_cancelled_and_missing_terminal_never_read_files(owner, monkeypatch, terminal):
    gateway, _, session, _ = owner
    def events(prompt):
        session.calls += 1
        yield session._event('turn.started')
        if terminal:
            yield session._event(terminal)
    monkeypatch.setattr(session, 'prompt_events', events)
    monkeypatch.setattr('pbatch.hub_gateway_files.bounded_capture',
                        lambda *args: pytest.fail('unsuccessful turn read files'))
    execute(owner)
    body = json.loads(gateway._files.queue.receipt('hub-turn')['body'])
    assert body['files'] == []
    assert body['unavailable'][0]['code'] == 'turn_not_completed'


def test_capture_remains_owned_until_done_and_cancel_discards_bytes(owner, monkeypatch):
    gateway, _, session, _ = owner
    entered, release = threading.Event(), threading.Event()
    def capture(current, identity, cancel_event=None):
        entered.set()
        assert release.wait(2)
        return bounded_capture(current, identity, cancel_event)
    monkeypatch.setattr('pbatch.hub_gateway_files.bounded_capture', capture)
    gateway._dispatch_command(_command(session))
    gateway._retry_pending_start()
    assert entered.wait(2)
    managed = gateway._managed(session.local_id)
    assert managed.active_turn == 'hub-turn'
    gateway._cancel_command(_command(session), managed)
    release.set()
    _wait_for(lambda: not gateway._turn_threads)
    body = json.loads(gateway._files.queue.receipt('hub-turn')['body'])
    assert body['files'] == [] and body['unavailable'][0]['code'] == 'cancel_requested'


def test_subprocess_deadline_and_stop_are_explicit_and_errors_hide_paths(owner, monkeypatch):
    gateway, _, _, _ = owner
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return ValidationResult(False, -1, 'private output', '/private/path', True)
    monkeypatch.setattr('pbatch.hub_gateway_files.run_bounded', run)
    value = bounded_capture(gateway, gateway._files.identity)
    assert value == {'files': [], 'unavailable': [{'path': 'out.txt', 'code': 'read_timeout'}]}
    args, kwargs = calls[0]
    assert args[2:5] == (5.0, 524288, False)
    assert kwargs['cancel_event'] is gateway._stop
    assert 'out.txt' not in repr(args[0]) and 'private' not in repr(value)


def test_actual_capture_process_stops_on_gateway_stop(owner):
    gateway, _, _, _ = owner
    gateway._stop.set()
    start = time.monotonic()
    value = bounded_capture(gateway, gateway._files.identity)
    assert time.monotonic() - start < 1
    assert value['files'] == [] and value['unavailable'][0]['code'] == 'capture_unavailable'


def test_disabled_gateway_never_reads_workspace_files(tmp_path, monkeypatch):
    monkeypatch.setattr('pbatch.hub_gateway_files.root_identity',
                        lambda path: pytest.fail('disabled feature read root'))
    gateway = HubGateway(_options(tmp_path), client=_FakeClient())
    gateway.close()


def test_observe_only_rejects_share_files_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr('pbatch.hub_gateway.HubClient.from_env',
                        lambda *args: pytest.fail('credentials were accessed'))
    options = _options(tmp_path, share_files=('out.txt',), observe_only=True,
                       state_dir=str(tmp_path.parent / 'private-state'), instance_id='inst',
                       sessions=0, max_sessions=0)
    with pytest.raises(ValueError, match='observation cannot share'):
        HubGateway(options)


@pytest.mark.parametrize('result', [
    {'files': [], 'unavailable': []},
    {'files': [{'path': 'out.txt'}], 'unavailable': []},
    {'files': [], 'unavailable': [{'path': 'other.txt', 'code': 'missing'}]},
])
def test_invalid_child_results_persist_explicit_unavailable(owner, monkeypatch, result):
    gateway, _, _, _ = owner
    monkeypatch.setattr('pbatch.hub_gateway_files.bounded_capture', lambda *args: result)
    execute(owner)
    receipt = gateway._files.queue.receipt('hub-turn')
    assert receipt['state'] == 'pending'
    body = json.loads(receipt['body'])
    assert body['files'] == []
    assert body['unavailable'] == [{'path': 'out.txt', 'code': 'capture_unavailable'}]


@pytest.mark.parametrize('status,code,state,uploads', [
    (507, 'snapshot_capacity', 'rejected', 1),
    (507, 'upstream_error', 'pending', 2),
    (503, 'hub_unavailable', 'pending', 2),
])
def test_capacity_rejection_is_distinct_from_unknown_server_outcome(owner, status, code, state, uploads):
    gateway, client, session, _ = owner
    execute(owner)
    gateway._flush_outbox()
    client.failure = HubError('service response', code=code, status=status)
    gateway._files.flush()
    gateway._files.flush()
    receipt = gateway._files.queue.receipt('hub-turn')
    assert receipt['state'] == state and len(client.uploads) == uploads
    assert session.calls == 1 and gateway._store.turn('hub-turn')['state'] == 'completed'
    if state == 'pending':
        assert client.uploads[0] == client.uploads[1]
    else:
        assert receipt['body'] == '' and receipt['error_code'] == 'upload_rejected'
