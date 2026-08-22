"""Contract tests for the bidirectional Codex App Server adapter."""
from __future__ import annotations

import json
import os
import queue
import threading
import time

import pytest

from pbatch.tui_adapter_appserver import (
    CodexAppServerAdapter,
    Transport,
)
from pbatch.tui_runtime import (
    ApprovalDecision,
    InitializeRequest,
    InterruptRequest,
    StartSessionRequest,
    StartTurnRequest,
    SteerRequest,
    UnsupportedOperation,
)


def _read_line(fd: int, buffer: bytearray):
    while b"\n" not in buffer:
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            return None
        if not chunk:
            return None
        buffer.extend(chunk)
    raw, _, rest = buffer.partition(b"\n")
    buffer[:] = rest
    return raw.decode("utf-8") + "\n"


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class PipeTransport:
    """Client half of two cross-connected line-oriented pipes."""
    def __init__(self, read_fd: int, write_fd: int):
        self._read_fd, self._write_fd = read_fd, write_fd
        self._buffer = bytearray()
        self._lock, self._closed = threading.Lock(), False

    def send_line(self, line: str) -> None:
        with self._lock:
            if self._closed:
                raise EOFError("closed")
            os.write(self._write_fd, (line.rstrip("\r\n") + "\n").encode())

    def recv_line(self):
        return _read_line(self._read_fd, self._buffer)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            write_fd = self._write_fd
            self._write_fd = -1
        _close_fd(write_fd)

    def close_reader(self) -> None:
        with self._lock:
            read_fd, self._read_fd = self._read_fd, -1
        _close_fd(read_fd)


class FakeDaemon:
    """Scripted JSON-RPC daemon running over a real full-duplex socketpair."""
    def __init__(self):
        daemon_read, client_write = os.pipe()
        client_read, daemon_write = os.pipe()
        self.transport = PipeTransport(client_read, client_write)
        self._read_fd, self._write_fd = daemon_read, daemon_write
        self._buffer = bytearray()
        self._scripts: queue.Queue[tuple[str, object, tuple[dict, ...]]] = queue.Queue()
        self._write_lock = threading.Lock()
        self.received: queue.Queue[dict] = queue.Queue()
        self.responses: queue.Queue[dict] = queue.Queue()
        self.errors: queue.Queue[BaseException] = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="fake-codex-daemon", daemon=True)
        self._thread.start()

    def expect(self, method: str, result=None, *, push: tuple[dict, ...] = ()) -> None:
        self._scripts.put((method, {} if result is None else result, push))

    def push_notification(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def push_request(self, request_id, method: str, params: dict) -> None:
        self._send({"id": request_id, "method": method, "params": params})

    def _send(self, value: dict) -> None:
        with self._write_lock:
            if self._closed or self._write_fd < 0:
                return
            os.write(self._write_fd, (json.dumps(value, ensure_ascii=False) + "\n").encode())

    def _close_output(self) -> None:
        with self._write_lock:
            write_fd, self._write_fd = self._write_fd, -1
        _close_fd(write_fd)

    def _run(self) -> None:
        try:
            while (line := _read_line(self._read_fd, self._buffer)) is not None:
                value = json.loads(line)
                if "method" not in value:
                    self.responses.put(value)
                    continue
                self.received.put(value)
                method, result, pushes = self._scripts.get(timeout=2)
                assert value["method"] == method
                for pushed in pushes:
                    self._send(pushed)
                self._send({"id": value["id"], "result": result})
        except (OSError, ValueError):
            pass
        except BaseException as exc:  # test harness must surface script failures
            self.errors.put(exc)
        finally:
            self._close_output()

    def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_output()

    def close(self) -> None:
        self.disconnect()
        self.transport.close()
        self._thread.join(timeout=1)
        read_fd, self._read_fd = self._read_fd, -1
        _close_fd(read_fd)
        self.transport.close_reader()
        if not self.errors.empty():
            raise self.errors.get_nowait()


def _initialize(daemon: FakeDaemon, **capabilities):
    daemon.expect("initialize", {
        "protocolVersion": 1,
        "schemaVersion": "0.149.0",
        "serverInfo": {"name": "codex", "version": "0.149.0"},
        "capabilities": capabilities,
    })
    adapter = CodexAppServerAdapter("/tmp/project", transport=daemon.transport)
    info = adapter.initialize(InitializeRequest("ws_test"))
    return adapter, info


def _request(daemon: FakeDaemon, method: str) -> dict:
    value = daemon.received.get(timeout=1)
    assert value["method"] == method
    return value


def _event(stream, kind: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = next(stream)
        if value.kind == kind:
            return value
    raise AssertionError(f"event not observed: {kind}")


def _start_turn(daemon: FakeDaemon, adapter: CodexAppServerAdapter):
    daemon.expect("thread/start", {"thread": {"id": "thread-1"}})
    adapter.start_session(StartSessionRequest(workspace_id="ws_test"))
    daemon.expect("turn/start", {"turn": {"id": "provider-turn-1"}})
    return adapter.start_turn(StartTurnRequest(
        "hello", run_id="run-1", turn_id="turn-1"))


def test_initialize_handshake_and_capabilities_are_truthful():
    daemon = FakeDaemon()
    adapter = CodexAppServerAdapter("/tmp/project", transport=daemon.transport)
    assert all(not enabled for enabled in adapter.runtime_info().capabilities.capabilities.values())
    adapter.close()
    daemon.close()

    daemon = FakeDaemon()
    adapter, info = _initialize(
        daemon, review=True, subagents=True, images=True, instructionSources=True)
    request = _request(daemon, "initialize")
    assert request["params"]["clientInfo"] == {"name": "pi-batch", "version": "1"}
    assert request["params"]["protocolVersion"] == 1
    enabled = info.capabilities
    for name in ("structured_events", "message_stream", "interrupt", "usage",
                 "session_resume", "session_fork", "turn_steer", "typed_approvals",
                 "review", "subagents", "images", "instruction_sources"):
        assert enabled.supports(name) is True
    assert enabled.message_queue is False
    assert adapter.server_info["server_version"] == "0.149.0"
    assert adapter.server_info["schema_version"] == "0.149.0"
    adapter.close()
    daemon.close()


def test_session_and_turn_request_shapes_and_handle_scoped_control():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    start_session = _request(daemon, "thread/start")
    assert start_session["params"] == {"cwd": "/tmp/project"}
    start_turn = _request(daemon, "turn/start")
    assert start_turn["params"] == {
        "threadId": "thread-1", "input": [{"type": "text", "text": "hello"}]}

    before = daemon.received.qsize()
    with pytest.raises(ValueError, match="active turn"):
        adapter.steer_turn(SteerRequest("not-current", "do not send"))
    assert daemon.received.qsize() == before

    daemon.expect("turn/steer", {"turnId": "provider-turn-1"})
    adapter.steer_turn(SteerRequest(handle.turn_id, "more"))
    steer = _request(daemon, "turn/steer")
    assert steer["params"]["expectedTurnId"] == "provider-turn-1"
    assert steer["params"]["input"][0]["text"] == "more"

    daemon.expect("turn/interrupt", {})
    adapter.interrupt_turn(InterruptRequest(handle.turn_id))
    interrupt = _request(daemon, "turn/interrupt")
    assert interrupt["params"] == {
        "threadId": "thread-1", "turnId": "provider-turn-1"}
    adapter.close()
    daemon.close()


def test_resume_fork_and_list_sessions_use_provider_thread_ids():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    daemon.expect("thread/resume", {"thread": {"id": "thread-parent"}})
    resumed = adapter.resume_session({"provider_session_id": "thread-parent"})
    assert resumed.provider_session_id == "thread-parent"
    assert _request(daemon, "thread/resume")["params"] == {"threadId": "thread-parent"}
    daemon.expect("thread/fork", {"thread": {"id": "thread-child"}})
    forked = adapter.fork_session({"provider_session_id": "thread-parent"})
    assert forked.provider_session_id == "thread-child"
    assert forked.session_id != resumed.session_id
    _request(daemon, "thread/fork")
    daemon.expect("thread/list", {"data": [{
        "id": "thread-child", "name": "Branch", "status": "idle", "updatedAt": 42}]})
    assert adapter.list_sessions({"limit": 10}) == [{
        "id": "thread-child", "name": "Branch", "status": "idle", "updated": 42}]
    assert _request(daemon, "thread/list")["params"] == {"limit": 10}
    adapter.close()
    daemon.close()


def test_notification_mapping_bounding_and_terminal_singleton():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-1", "turn": {"id": "provider-turn-1", "status": "inProgress"}})
    daemon.push_notification("item/started", {"item": {
        "id": "m1", "type": "agentMessage", "text": ""}})
    daemon.push_notification("item/agentMessage/delta", {
        "itemId": "m1", "delta": "partial"})
    daemon.push_notification("item/completed", {"item": {
        "id": "m1", "type": "agentMessage", "text": "complete"}})
    huge = "x" * (128 * 1024)
    daemon.push_notification("item/completed", {"item": {
        "id": "c1", "type": "commandExecution", "command": "big",
        "aggregatedOutput": huge, "exitCode": 7}})
    daemon.push_notification("thread/tokenUsage/updated", {
        "tokenUsage": {"total": {"inputTokens": 11, "outputTokens": 3}}})
    completed = {"threadId": "thread-1", "turn": {
        "id": "provider-turn-1", "status": "completed"}}
    daemon.push_notification("turn/completed", completed)
    daemon.push_notification("turn/completed", completed)
    daemon.push_notification("warning", {"message": "ordering barrier"})

    events = []
    while not handle.done or not any(event.kind == "turn.completed" for event in events):
        events.append(next(stream))
    assert _event(stream, "runtime.warning").payload["message"] == "ordering barrier"
    assert [event.kind for event in events[:4]] == [
        "turn.queued", "turn.started", "message.started", "message.delta"]
    assert any(event.kind == "message.completed" and
               event.payload["text"] == "complete" for event in events)
    command = next(event for event in events if event.kind == "command.completed")
    assert command.payload["exit_code"] == 7
    assert command.payload["truncated"] is True
    assert command.payload["aggregated_output"].endswith("...[truncated]")
    assert any(event.kind == "usage.updated" for event in events)
    assert sum(event.kind == "turn.completed" for event in events) == 1
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    daemon.disconnect()
    assert [event.kind for event in stream] == ["runtime.error"]
    assert handle.state == "SUCCEEDED"
    assert all(not value for value in adapter.runtime_info().capabilities.capabilities.values())
    adapter.close()
    daemon.close()


def test_approval_round_trip_unknown_and_type_mismatch_are_fail_closed():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    stream = adapter.events()
    daemon.push_request("approve-1", "execCommandApproval", {
        "command": ["git", "status"], "cwd": "/tmp/project"})
    requested = _event(stream, "approval.requested")
    assert requested.payload["request_id"] == "approve-1"
    assert requested.payload["approval_type"] == "exec_command"
    assert adapter.resolve_approval(ApprovalDecision("approve-1", "allow")) is True
    response = daemon.responses.get(timeout=1)
    assert response == {"id": "approve-1", "result": {"decision": "approved"}}
    resolved = _event(stream, "approval.resolved")
    assert resolved.payload["request_id"] == "approve-1"

    assert adapter.resolve_approval(ApprovalDecision("unknown", "allow")) is False
    assert adapter.resolve_approval({
        "request_id": "approve-1", "decision": "allow",
        "approval_type": "file_change"}) is False
    with pytest.raises(queue.Empty):
        daemon.responses.get(timeout=0.05)
    adapter.close()
    daemon.close()
    daemon.close()


def test_disconnect_invalidates_pending_approval_then_ends_event_stream():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    stream = adapter.events()
    daemon.push_request(9, "item/fileChange/requestApproval", {
        "threadId": "thread-1", "turnId": "provider-turn", "changes": ["x.py"]})
    assert _event(stream, "approval.requested").payload["request_id"] == 9
    daemon.disconnect()
    tail = list(stream)
    assert [event.kind for event in tail] == ["approval.expired", "runtime.error"]
    assert tail[0].payload["target"] == "connection lost, approval invalidated"
    assert tail[1].payload["message"] == "connection lost, approval invalidated"
    assert adapter.resolve_approval(ApprovalDecision(9, "allow")) is False
    assert all(not value for value in adapter.runtime_info().capabilities.capabilities.values())
    with pytest.raises(queue.Empty):
        daemon.responses.get(timeout=0.05)
    adapter.close()


def test_ttl_unknown_notification_and_unsupported_review():
    daemon = FakeDaemon()
    daemon.expect("initialize", {"protocolVersion": 1, "capabilities": {"review": False}})
    adapter = CodexAppServerAdapter(
        "/tmp/project", transport=daemon.transport, approval_ttl=0.05)
    adapter.initialize(InitializeRequest())
    _request(daemon, "initialize")
    stream = adapter.events()
    daemon.push_request("short", "item/commandExecution/requestApproval", {
        "command": "echo hi"})
    _event(stream, "approval.requested")
    expired = _event(stream, "approval.expired")
    assert expired.payload["request_id"] == "short"
    assert adapter.resolve_approval(ApprovalDecision("short", "accept")) is False

    daemon.push_notification("future/notification", {"large": "z" * (128 * 1024)})
    unknown = _event(stream, "runtime.unknown_item")
    assert unknown.payload["method"] == "future/notification"
    assert len(json.dumps(unknown.payload).encode()) <= 64 * 1024
    with pytest.raises(UnsupportedOperation, match="review/start"):
        adapter.start_review({})
    adapter.close()
    daemon.close()


def test_initialize_eof_is_structured_unavailability():
    daemon = FakeDaemon()
    adapter = CodexAppServerAdapter("/tmp/project", transport=daemon.transport)
    daemon.disconnect()
    with pytest.raises(RuntimeError, match="^codex app-server unavailable: connection lost"):
        adapter.initialize(InitializeRequest())
    assert all(not enabled for enabled in adapter.runtime_info().capabilities.capabilities.values())
    daemon.close()


def test_transport_protocol_is_runtime_checkable():
    daemon = FakeDaemon()
    assert isinstance(daemon.transport, Transport)
    daemon.close()
