"""Additional Pi RPC wire and active-turn acceptance boundaries.

These tests deliberately exercise the adapter's command core without adding
the next model/thinking/compaction API.  They remain valid when that control
surface is added: every command must keep the same id, response-shape, EOF,
and active-turn invariants.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from pbatch.tui_adapter_pi_rpc import PiRpcAdapter
from pbatch.tui_protocol import TurnState


_MISSING = object()


class DeferredTransport:
    """Transport whose test owns response ordering and response shape."""

    def __init__(self) -> None:
        self.incoming: queue.Queue[str | None] = queue.Queue()
        self.sent: list[dict] = []
        self.condition = threading.Condition()
        self.closed = False

    def send_line(self, line: str) -> None:
        with self.condition:
            self.sent.append(json.loads(line))
            self.condition.notify_all()

    def recv_line(self):
        return self.incoming.get(timeout=2)

    def wait_request(self, index: int) -> dict:
        deadline = time.monotonic() + 2
        with self.condition:
            while len(self.sent) <= index:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"request {index} was not sent")
                self.condition.wait(remaining)
            return dict(self.sent[index])

    def respond(
            self, request: dict, *, request_id=_MISSING,
            command=_MISSING, success=True, data=_MISSING, error="") -> None:
        value = {
            "id": request["id"] if request_id is _MISSING else request_id,
            "type": "response",
            "command": request["type"] if command is _MISSING else command,
            "success": success,
        }
        if data is not _MISSING:
            value["data"] = data
        if error:
            value["error"] = error
        self.push(value)

    def push(self, value: dict) -> None:
        self.incoming.put(json.dumps(value, ensure_ascii=False) + "\n")

    def disconnect(self) -> None:
        self.incoming.put(None)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.disconnect()


def _state(path: str = "/sessions/acceptance.jsonl") -> dict:
    return {
        "sessionFile": path, "sessionId": "acceptance",
        "isStreaming": False, "messageCount": 0,
    }


def _invoke(function, *args, **kwargs):
    outcome = []

    def run() -> None:
        try:
            outcome.append(function(*args, **kwargs))
        except BaseException as exc:  # assertions inspect provider failures
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, outcome


def _finish(worker: threading.Thread, outcome: list):
    worker.join(2)
    assert worker.is_alive() is False
    assert len(outcome) == 1
    return outcome[0]


def _initialized(
        transport: DeferredTransport, *, timeout: float = 5.0) -> PiRpcAdapter:
    adapter = PiRpcAdapter(
        "/tmp/project", transport=transport, timeout=timeout)
    worker, outcome = _invoke(adapter.initialize, {})
    request = transport.wait_request(0)
    transport.respond(request, data=_state())
    info = _finish(worker, outcome)
    assert info.capabilities.structured_events is True
    return adapter


def _call(adapter: PiRpcAdapter, transport: DeferredTransport,
          index: int, rpc_command: str, *, data=_MISSING, **response):
    worker, outcome = _invoke(adapter._call, rpc_command)
    request = transport.wait_request(index)
    assert request["type"] == rpc_command
    transport.respond(request, data=data, **response)
    return _finish(worker, outcome), request


def test_concurrent_out_of_order_responses_match_only_their_request_ids():
    transport = DeferredTransport()
    adapter = _initialized(transport)
    first_worker, first = _invoke(adapter._call, "get_state")
    second_worker, second = _invoke(adapter._call, "get_messages")
    requests = [transport.wait_request(1), transport.wait_request(2)]
    by_command = {request["type"]: request for request in requests}

    transport.respond(by_command["get_messages"], data={"messages": ["m"]})
    transport.respond(by_command["get_state"], data=_state("/sessions/other.jsonl"))

    outcomes = {
        "get_state": _finish(first_worker, first),
        "get_messages": _finish(second_worker, second),
    }
    # Thread scheduling may swap which worker owns pbatch-2, so inspect results.
    observed = [outcomes["get_state"], outcomes["get_messages"]]
    assert {tuple(value) for value in observed} == {
        tuple(_state("/sessions/other.jsonl")), ("messages",)}
    ids = [request["id"] for request in requests]
    assert len(set(ids)) == 2
    assert all(request_id.startswith("pbatch-") for request_id in ids)
    adapter.close()


def test_response_command_and_data_shapes_fail_only_the_matching_request():
    transport = DeferredTransport()
    adapter = _initialized(transport)

    mismatch, _ = _call(
        adapter, transport, 1, "get_messages", command="get_state")
    scalar, _ = _call(
        adapter, transport, 2, "get_messages", data=["not", "an", "object"])
    no_data, _ = _call(
        adapter, transport, 3, "set_thinking_level")

    assert isinstance(mismatch, RuntimeError)
    assert "command mismatch" in str(mismatch)
    assert isinstance(scalar, RuntimeError)
    assert "invalid data" in str(scalar)
    assert no_data == {}
    assert adapter.runtime_info().capabilities.structured_events is True
    adapter.close()


def test_missing_response_id_disconnects_and_revokes_capabilities():
    transport = DeferredTransport()
    adapter = _initialized(transport)
    worker, outcome = _invoke(adapter._call, "get_messages")
    transport.wait_request(1)
    transport.push({
        "type": "response", "command": "get_messages",
        "success": True, "data": {"messages": []},
    })

    failed = _finish(worker, outcome)
    assert isinstance(failed, RuntimeError)
    assert "connection lost" in str(failed)
    assert not any(adapter.runtime_info().capabilities.capabilities.values())
    error = adapter.events().get(timeout=1)
    assert error.kind == "runtime.error"
    assert error.payload["reason"] == "Pi RPC connection lost"
    adapter.close()


def test_timed_out_request_is_forgotten_and_late_response_is_diagnostic():
    transport = DeferredTransport()
    adapter = _initialized(transport, timeout=0.05)

    with pytest.raises(RuntimeError, match="get_messages timed out"):
        adapter._call("get_messages")
    expired = transport.wait_request(1)
    transport.respond(expired, data={"messages": []})

    warning = adapter.events().get(timeout=1)
    assert warning.kind == "runtime.warning"
    assert warning.payload["request_id"].endswith(expired["id"])
    assert adapter.runtime_info().capabilities.structured_events is True
    adapter.close()


def test_active_turn_guards_reject_ambiguous_control_without_wire_writes():
    transport = DeferredTransport()
    adapter = _initialized(transport)
    session_worker, session_outcome = _invoke(
        adapter.start_session, {"session_id": "ses-local"})
    transport.respond(transport.wait_request(1), data=_state())
    _finish(session_worker, session_outcome)

    turn_worker, turn_outcome = _invoke(
        adapter.start_turn,
        {"prompt": "work", "run_id": "run-one", "turn_id": "turn-one"})
    transport.respond(transport.wait_request(2))
    handle = _finish(turn_worker, turn_outcome)
    sent_before = len(transport.sent)

    with pytest.raises(RuntimeError, match="already running"):
        adapter.start_turn("second turn")
    with pytest.raises(RuntimeError, match="active Pi turn"):
        adapter.start_session({"session_id": "ses-other"})
    with pytest.raises(ValueError, match="does not match"):
        adapter.steer_turn({"turn_id": "turn-other", "text": "wrong"})
    with pytest.raises(ValueError, match="does not match"):
        adapter.interrupt_turn({"turn_id": "turn-other"})
    assert len(transport.sent) == sent_before

    follow_worker, follow_outcome = _invoke(
        adapter.follow_up, {"turn_id": "turn-one", "text": "then summarize"})
    follow_request = transport.wait_request(3)
    transport.respond(follow_request)
    assert _finish(follow_worker, follow_outcome) is None
    assert follow_request["type"] == "follow_up"
    assert follow_request["message"] == "then summarize"

    transport.push({"type": "agent_settled"})
    assert handle.wait(1)
    assert handle.state == TurnState.SUCCEEDED
    adapter.close()
