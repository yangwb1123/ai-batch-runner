"""Contract tests for the earendil-works/Pi stdio RPC adapter."""

from __future__ import annotations

from collections import deque
import io
import json
import queue
import time

import pytest

from pbatch.tui_adapter_pi_rpc import PiRpcAdapter, Transport
from pbatch.tui_pi_rpc_support import (
    DefaultPiRpcTransport, decode_line, pi_rpc_argv,
)
from pbatch.tui_runtime import (
    InitializeRequest, InterruptRequest, SessionReconciliationError,
    StartSessionRequest, StartTurnRequest, SteerRequest, UnsupportedOperation,
)


def _state(path: str, name: str = "") -> dict:
    value = {
        "sessionFile": path, "sessionId": path.rsplit("/", 1)[-1],
        "isStreaming": False, "messageCount": 0, "pendingMessageCount": 0,
    }
    if name:
        value["sessionName"] = name
    return value


class FakeTransport:
    """Synchronous command script with an asynchronous stdout queue."""

    def __init__(self):
        self.scripts = deque()
        self.incoming: queue.Queue[str | None] = queue.Queue()
        self.sent: list[dict] = []
        self.closed = False

    def expect(
            self, command: str, data=None, *, success: bool = True,
            error: str = "", before=(), events=()) -> None:
        self.scripts.append((command, data, success, error, before, events))

    def send_line(self, line: str) -> None:
        request = decode_line(line)
        self.sent.append(request)
        expected, data, success, error, before, events = self.scripts.popleft()
        assert request["type"] == expected
        for item in before:
            self.push(item)
        response = {
            "id": request["id"], "type": "response",
            "command": expected, "success": success,
        }
        if data is not None:
            response["data"] = data
        if error:
            response["error"] = error
        self.push(response)
        for item in events:
            self.push(item)

    def recv_line(self):
        return self.incoming.get(timeout=2)

    def push(self, value: dict) -> None:
        self.incoming.put(json.dumps(value, ensure_ascii=False) + "\n")

    def push_raw(self, line: str) -> None:
        self.incoming.put(line)

    def disconnect(self) -> None:
        self.incoming.put(None)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.disconnect()


def _initialized(transport: FakeTransport, path: str = "/sessions/start.jsonl"):
    transport.expect("get_state", _state(path))
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    info = adapter.initialize(InitializeRequest("ws-test"))
    return adapter, info


def _claim(adapter: PiRpcAdapter, transport: FakeTransport, path: str) -> None:
    transport.expect("get_state", _state(path))
    adapter.start_session(StartSessionRequest(
        workspace_id="ws-test", session_id="ses-local"))


def _events_until(stream, terminal: str, maximum: int = 40):
    events = []
    for _ in range(maximum):
        event = stream.get(timeout=1)
        assert event is not None
        events.append(event)
        if event.kind == terminal:
            return events
    raise AssertionError(f"terminal event not observed: {terminal}")


def test_initialize_capabilities_and_first_session_adopts_startup_session():
    transport = FakeTransport()
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    assert not any(adapter.runtime_info().capabilities.capabilities.values())
    transport.expect("get_state", _state("/sessions/one.jsonl"))
    info = adapter.initialize({"workspace_id": "ws-local"})
    transport.expect("set_session_name")
    transport.expect("get_state", _state("/sessions/one.jsonl", "feature"))
    session = adapter.start_session({
        "workspace_id": "ws-local", "session_id": "ses-one", "name": "feature"})

    enabled = {name for name, value in info.capabilities.capabilities.items() if value}
    assert enabled == {
        "structured_events", "message_stream", "interrupt", "session_resume",
        "session_fork", "turn_steer", "message_queue", "usage",
        "instruction_sources", "session_state", "session_stats",
        "session_compact", "model_catalog", "thinking_levels",
    }
    assert session.provider_session_id == "/sessions/one.jsonl"
    assert [item["type"] for item in transport.sent] == [
        "get_state", "set_session_name", "get_state"]
    assert transport.sent[1]["name"] == "feature"
    assert info.capabilities.typed_approvals is False
    assert info.capabilities.model_select is False
    assert info.capabilities.thinking_select is False
    assert info.capabilities.session_read is False
    adapter.close()


def test_subsequent_start_session_uses_new_session_and_exact_state():
    transport = FakeTransport()
    adapter, _ = _initialized(transport, "/sessions/one.jsonl")
    _claim(adapter, transport, "/sessions/one.jsonl")
    transport.expect("new_session", {"cancelled": False})
    transport.expect("set_session_name")
    transport.expect("get_state", _state("/sessions/two.jsonl", "second"))
    session = adapter.start_session({"session_id": "ses-two", "name": "second"})
    assert session.provider_session_id == "/sessions/two.jsonl"
    assert [item["type"] for item in transport.sent[-3:]] == [
        "new_session", "set_session_name", "get_state"]
    adapter.close()


def test_resume_switches_and_requires_exact_session_file_readback():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    target = "/sessions/target.jsonl"
    transport.expect("switch_session", {"cancelled": False})
    transport.expect("get_state", _state(target))
    session = adapter.resume_session({
        "provider_session_id": target, "session_id": "ses-target"})
    assert session.provider_session_id == target
    assert transport.sent[-2]["sessionPath"] == target

    wrong = "/sessions/wrong.jsonl"
    transport.expect("switch_session", {"cancelled": False})
    transport.expect("get_state", _state(wrong))
    with pytest.raises(SessionReconciliationError, match="different sessionFile"):
        adapter.resume_session({"provider_session_id": target})
    adapter.close()


def test_fork_switches_source_then_clones_whole_active_branch():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    source, child = "/sessions/source.jsonl", "/sessions/child.jsonl"
    transport.expect("switch_session", {"cancelled": False})
    transport.expect("get_state", _state(source))
    transport.expect("clone", {"cancelled": False})
    transport.expect("get_state", _state(child))
    session = adapter.fork_session({
        "provider_session_id": source, "session_id": "ses-child"})
    assert session.provider_session_id == child
    assert [item["type"] for item in transport.sent[-4:]] == [
        "switch_session", "get_state", "clone", "get_state"]
    assert adapter.events().get(timeout=1).kind == "session.forked"
    adapter.close()


def _assistant(text: str, stop: str = "stop", output: int = 2) -> dict:
    return {
        "role": "assistant", "content": [{"type": "text", "text": text}],
        "stopReason": stop,
        "usage": {"input": 10, "output": output, "cacheRead": 3,
                  "cacheWrite": 0, "totalTokens": 10 + output + 3},
    }


def _turn_events() -> tuple[dict, ...]:
    final = _assistant("Hello world")
    return (
        {"type": "agent_start"},
        {"type": "message_start", "message": _assistant("", output=0)},
        {"type": "message_update", "usage": {"input": 10, "output": 1,
         "cacheRead": 3, "cacheWrite": 0, "totalTokens": 14},
         "assistantMessageEvent": {"type": "text_delta", "delta": "Hello "}},
        {"type": "tool_execution_start", "toolCallId": "call-1",
         "toolName": "read", "args": {"path": "README.md"}},
        {"type": "tool_execution_update", "toolCallId": "call-1",
         "toolName": "read", "partialResult": {"content": "partial"}},
        {"type": "tool_execution_end", "toolCallId": "call-1",
         "toolName": "read", "result": {"content": "done"}, "isError": False},
        {"type": "message_end", "message": final},
        {"type": "agent_end", "messages": [final], "willRetry": False},
        {"type": "extension_error", "extensionPath": "/ext/x.ts",
         "event": "tool_call", "error": "extension failed"},
        {"type": "agent_settled"},
    )


def test_prompt_response_is_acceptance_and_events_settle_the_turn():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    transport.expect("prompt", events=_turn_events())
    stream = adapter.events()
    assert stream.get(timeout=1).kind == "session.created"
    handle = adapter.start_turn(StartTurnRequest(
        "hello", run_id="run-1", turn_id="turn-1"))
    assert handle.wait(2)
    events = _events_until(stream, "turn.completed")
    kinds = [event.kind for event in events]
    assert kinds[:3] == ["turn.queued", "turn.started", "message.started"]
    assert "message.delta" in kinds
    assert "tool.started" in kinds and "tool.completed" in kinds
    assert "runtime.unknown_item" in kinds  # Pi has no neutral tool-update kind.
    assert "runtime.error" in kinds  # extension_error is diagnostic, not terminal.
    completed = next(event for event in events if event.kind == "message.completed")
    assert completed.payload["text"] == "Hello world"
    assert events[-1].payload["usage"]["total_tokens"] == 15
    assert handle.state == "SUCCEEDED"
    adapter.close()


def test_each_idless_assistant_message_gets_a_distinct_local_item_id():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    first, second = _assistant("tool requested"), _assistant("final answer")
    transport.expect("prompt", events=(
        {"type": "agent_start"},
        {"type": "message_start", "message": _assistant("", output=0)},
        {"type": "message_end", "message": first},
        {"type": "message_start", "message": _assistant("", output=0)},
        {"type": "message_end", "message": second},
        {"type": "agent_settled"},
    ))
    stream = adapter.events()
    assert stream.get(timeout=1).kind == "session.created"
    handle = adapter.start_turn("two assistant messages")
    assert handle.wait(2)
    events = _events_until(stream, "turn.completed")
    starts = [event for event in events if event.kind == "message.started"]
    ends = [event for event in events if event.kind == "message.completed"]
    assert [event.payload["item_id"] for event in starts] == [
        event.payload["item_id"] for event in ends]
    assert starts[0].payload["item_id"] != starts[1].payload["item_id"]
    adapter.close()


def test_retry_error_is_cleared_by_later_authoritative_success_message():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    failed = _assistant("temporary failure", stop="error")
    failed["errorMessage"] = "transient provider error"
    recovered = _assistant("recovered", stop="stop")
    transport.expect("prompt", events=(
        {"type": "agent_start"},
        {"type": "message_start", "message": _assistant("", output=0)},
        {"type": "message_end", "message": failed},
        {"type": "agent_end", "messages": [failed], "willRetry": True},
        {"type": "message_start", "message": _assistant("", output=0)},
        {"type": "message_end", "message": recovered},
        {"type": "agent_end", "messages": [recovered], "willRetry": False},
        {"type": "agent_settled"},
    ))
    stream = adapter.events()
    assert stream.get(timeout=1).kind == "session.created"
    handle = adapter.start_turn("retry")
    assert handle.wait(2)
    events = _events_until(stream, "turn.completed")
    assert events[-1].kind == "turn.completed"
    assert handle.state == "SUCCEEDED"
    adapter.close()


def test_new_session_event_does_not_inherit_the_settled_turn_identity():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    stream = adapter.events()
    assert stream.get(timeout=1).kind == "session.created"
    transport.expect("prompt", events=(
        {"type": "agent_start"}, {"type": "agent_settled"}))
    handle = adapter.start_turn(StartTurnRequest(
        "finish", run_id="run-old", turn_id="turn-old"))
    assert handle.wait(2)
    _events_until(stream, "turn.completed")

    transport.expect("new_session", {"cancelled": False})
    transport.expect("get_state", _state("/sessions/fresh.jsonl"))
    adapter.start_session({"session_id": "ses-fresh"})
    event = stream.get(timeout=1)
    assert event.kind == "session.created"
    assert (event.run_id, event.turn_id) == ("", "")
    adapter.close()


def test_steer_follow_up_and_abort_map_to_real_queue_commands():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    transport.expect("prompt")
    handle = adapter.start_turn(StartTurnRequest("work", turn_id="turn-active"))
    transport.expect("steer")
    adapter.steer_turn(SteerRequest("turn-active", "change direction"))
    transport.expect("follow_up")
    adapter.follow_up({"turn_id": "turn-active", "text": "then summarize"})
    aborted = _assistant("", stop="aborted", output=0)
    transport.expect("get_state", _state("/sessions/start.jsonl"))
    transport.expect("abort", events=(
        {"type": "agent_start"},
        {"type": "message_end", "message": aborted},
        {"type": "agent_settled"},
    ))
    adapter.interrupt_turn(InterruptRequest("turn-active"))
    assert handle.wait(2)
    events = _events_until(adapter.events(), "turn.interrupted")
    assert any(event.kind == "turn.interrupting" for event in events)
    assert transport.sent[-4]["message"] == "change direction"
    assert transport.sent[-3]["message"] == "then summarize"
    assert handle.state == "INTERRUPTED"
    adapter.close()


def test_abort_refuses_pending_queue_without_clear_queue_support():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    transport.expect("prompt")
    handle = adapter.start_turn(StartTurnRequest(
        "work", turn_id="turn-pending"))
    state = _state("/sessions/start.jsonl")
    state["pendingMessageCount"] = 1
    transport.expect("get_state", state)

    with pytest.raises(UnsupportedOperation, match="queue_clear"):
        adapter.interrupt_turn(InterruptRequest("turn-pending"))
    assert transport.sent[-1]["type"] == "get_state"
    transport.push({"type": "agent_settled"})
    assert handle.wait(1)
    adapter.close()


def test_request_ids_correlate_and_unmatched_response_is_diagnostic():
    transport = FakeTransport()
    transport.expect("get_state", _state("/sessions/start.jsonl"), before=(
        {"id": "somebody-else", "type": "response", "command": "get_state",
         "success": True, "data": _state("/sessions/other.jsonl")},
    ))
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    adapter.initialize({})
    warning = adapter.events().get(timeout=1)
    assert warning.kind == "runtime.warning"
    assert adapter.session.provider_session_id == "/sessions/start.jsonl"
    assert transport.sent[0]["id"].startswith("pbatch-")
    adapter.close()


def test_failed_response_strict_framing_eof_and_unsupported_operations():
    failed = FakeTransport()
    failed.expect("get_state", success=False, error="not authenticated")
    with pytest.raises(RuntimeError, match="not authenticated"):
        PiRpcAdapter("/tmp/project", transport=failed).initialize({})

    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    with pytest.raises(UnsupportedOperation, match="session_read"):
        adapter.read_session({})
    with pytest.raises(UnsupportedOperation, match="session_list"):
        adapter.list_sessions()
    transport.push_raw('{"type":"agent_start"}')
    error = adapter.events().get(timeout=1)
    assert error.kind == "runtime.error"
    assert not any(adapter.runtime_info().capabilities.capabilities.values())
    adapter.close()


def test_active_turn_eof_finishes_waiter_as_disconnected():
    transport = FakeTransport()
    adapter, _ = _initialized(transport)
    _claim(adapter, transport, "/sessions/start.jsonl")
    transport.expect("prompt")
    handle = adapter.start_turn("hello")
    transport.disconnect()
    assert handle.wait(2)
    events = _events_until(adapter.events(), "runtime.error")
    assert events[-1].payload["reason"] == "Pi RPC connection lost"
    assert handle.state == "DISCONNECTED"
    adapter.close()


def test_decode_line_enforces_lf_jsonl_but_accepts_crlf_and_unicode_separator():
    value = decode_line('{"type":"x","text":"a\u2028b"}\r\n')
    assert value["text"] == "a\u2028b"
    with pytest.raises(ValueError, match="LF delimiter"):
        decode_line('{"type":"x"}')
    with pytest.raises(ValueError, match="valid JSON"):
        decode_line('{"type":"x","n":NaN}\n')
    with pytest.raises(ValueError, match="exceeds"):
        decode_line('{"type":"x"}\n', maximum=4)


class _CaptureInput(io.BytesIO):
    def flush(self) -> None:
        return None


class _ChunkStream:
    def __init__(self, chunks):
        self.chunks = deque(chunks)

    def read(self, _size: int):
        return self.chunks.popleft() if self.chunks else b""


class _FakeProcess:
    def __init__(self):
        self.stdin = _CaptureInput()
        self.stdout = _ChunkStream([b'{"type":"agent_start"}\n'])
        self.stderr = _ChunkStream([b"x" * 100_000])
        self.pid = None
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed, self.returncode = True, -9

    def wait(self, timeout=None):
        return self.returncode


def test_adapter_confirms_only_its_owned_no_tools_transport():
    managed = PiRpcAdapter("/tmp/project", no_tools_args=("--no-tools",))
    injected = PiRpcAdapter(
        "/tmp/project", transport=FakeTransport(),
        no_tools_args=("--no-tools",))

    assert managed.tools_disabled is True
    assert injected.tools_disabled is False


def test_pi_rpc_argv_applies_explicit_no_tools_policy():
    assert pi_rpc_argv("pi", no_tools_args=("--no-tools",)) == [
        "pi", "--mode", "rpc", "--no-tools"]
    with pytest.raises(ValueError, match="no_tools_args"):
        pi_rpc_argv("pi", no_tools_args="--no-tools")


def test_default_transport_builds_pi_rpc_argv_bounds_stderr_and_closes_process():
    process = _FakeProcess()
    calls = []

    def spawn(argv, cwd):
        calls.append((tuple(argv), cwd))
        return process

    transport = DefaultPiRpcTransport(
        "/tmp/project", agent="my-pi", provider="openai", model="gpt-x",
        session_name="named", spawn=spawn,
        no_tools_args=("--no-tools",))
    assert calls == [(tuple(pi_rpc_argv(
        "my-pi", provider="openai", model="gpt-x", session_name="named",
        no_tools_args=("--no-tools",))),
        "/tmp/project")]
    assert decode_line(transport.recv_line())["type"] == "agent_start"
    transport.send_line('{"type":"get_state"}\n')
    time.sleep(0.01)
    assert len(transport.unavailable_reason().encode()) < 10_000
    transport.close()
    assert process.killed is True
    assert transport.argv[:3] == ("my-pi", "--mode", "rpc")
