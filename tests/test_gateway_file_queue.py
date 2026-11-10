import hashlib
import uuid

import pytest

from pbatch.hub_gateway_files_store import GatewayFileQueue
from pbatch.hub_gateway_store import GatewayStore


def payload(turn='turn-1', generation=1):
    return {'schema': 'abr.file-snapshots.v1', 'instance_id': 'inst-1',
            'generation': generation, 'session_id': 'session-1', 'turn_id': turn,
            'snapshot_set_id': uuid.uuid4().hex, 'files': [],
            'unavailable': [{'path': 'out.txt', 'code': 'turn_not_completed'}]}


@pytest.fixture
def queue(tmp_path):
    store = GatewayStore(tmp_path)
    try:
        yield GatewayFileQueue(store), store
    finally:
        store.close()


def ready(queue, store, body):
    event_id = 'evt-' + body['turn_id']
    store.enqueue_event(event_id=event_id, event={'kind': 'turn.completed'},
                        generation=body['generation'])
    assert queue.reserve(body, event_id)
    queue.save(body)
    return event_id


def test_payload_waits_for_terminal_ack_and_retries_same_bytes(queue):
    outbox, store = queue
    body = payload()
    event_id = ready(outbox, store, body)
    assert outbox.pending(1) == []
    store.mark_event_sent(event_id, 'cursor-1')
    row = outbox.pending(1)[0]
    outbox.failure(row['turn_id'], 'upload_unknown')
    assert outbox.pending(1)[0]['body'] == body
    outbox.finish(row['turn_id'], 'acknowledged')
    assert outbox.pending(1) == []
    receipt = outbox.receipt(row['turn_id'])
    assert receipt['body'] == '' and receipt['state'] == 'acknowledged'
    assert len(receipt['payload_digest']) == len(hashlib.sha256().hexdigest())


def test_payload_is_immutable_and_turn_never_recaptured(queue):
    outbox, store = queue
    body = payload()
    ready(outbox, store, body)
    assert not outbox.reserve(body, 'other-event')
    outbox.save(body)
    with pytest.raises(ValueError, match='immutable'):
        outbox.save(dict(body, snapshot_set_id=uuid.uuid4().hex))


def test_generation_change_does_not_replay_old_bytes(queue):
    outbox, store = queue
    body = payload()
    event_id = ready(outbox, store, body)
    store.mark_event_sent(event_id, 'cursor-1')
    outbox.activate_generation(2)
    assert outbox.pending(1) == outbox.pending(2) == []
    assert outbox.receipt(body['turn_id'])['state'] == 'obsolete'


def test_pending_capacity_is_reserved_before_files_are_read(queue):
    outbox, store = queue
    for index in range(16):
        assert outbox.reserve(payload(f'turn-{index}'), f'evt-{index}')
    assert not outbox.reserve(payload('overflow'), 'overflow-event')
    receipt = outbox.receipt('overflow')
    assert receipt['state'] == 'rejected' and receipt['error_code'] == 'capacity_exceeded'
