"""Runner evidence buffering during a temporary control-plane outage."""

from __future__ import annotations

from pbatch.fabric_outbox import EvidenceOutbox, EvidenceOutboxError
from pbatch.fabric_runner import _submit_evidence, flush_evidence


def _record():
    return {
        "device_id": "rn-outbox", "task_id": "task-1",
        "attempt_id": "attempt-1", "lease_id": "lease-1",
        "fencing_token": 3, "evidence": {"result": "passed"},
    }


class _Client:
    device_id = "rn-outbox"

    def __init__(self, responses):
        self.responses = list(responses)

    def evidence(self, *args, **kwargs):
        return self.responses.pop(0)


def test_outbox_is_idempotent_restartable_and_acknowledgeable(tmp_path):
    outbox = EvidenceOutbox(tmp_path)
    record = _record()
    key = outbox.enqueue(record)
    assert outbox.enqueue(dict(record)) == key
    restored = EvidenceOutbox(tmp_path)
    assert restored.pending() == [record]
    assert restored.acknowledge(key, "delivered") is True
    assert EvidenceOutbox(tmp_path).pending() == []


def test_outbox_rejects_conflicting_duplicate(tmp_path):
    outbox = EvidenceOutbox(tmp_path)
    outbox.enqueue(_record())
    conflicting = _record()
    conflicting["evidence"] = {"result": "failed"}
    try:
        outbox.enqueue(conflicting)
    except EvidenceOutboxError as exc:
        assert "conflicting" in str(exc)
    else:
        raise AssertionError("conflicting evidence was accepted")


def test_transport_failure_is_buffered_and_replayed(tmp_path):
    outbox = EvidenceOutbox(tmp_path)
    task = {"task_id": "task-1", "lease": {
        "lease_id": "lease-1", "fencing_token": 3}}
    offline = _Client([{"accepted": False, "transport_error": True}])
    result = _submit_evidence(offline, task, {"result": "passed"}, outbox)
    assert result["buffered"] is True
    assert len(outbox) == 1

    online = _Client([{"accepted": True}])
    assert flush_evidence(online, outbox) == {
        "delivered": 1, "rejected": 0, "pending": 0}


def test_stale_replay_is_authoritatively_removed(tmp_path):
    outbox = EvidenceOutbox(tmp_path)
    outbox.enqueue(_record())
    stale = _Client([{"accepted": False, "reason": "stale fencing token"}])
    result = flush_evidence(stale, outbox)
    assert result == {"delivered": 0, "rejected": 1, "pending": 0}
