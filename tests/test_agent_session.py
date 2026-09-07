from __future__ import annotations

import queue
import json
import threading
import time
from dataclasses import dataclass

import pytest

from pbatch import agent_session as sdk
from pbatch.agent_session import (
    AgentSessionOptions,
    CompactionTimeout,
    QueueReceipt,
    TurnResult,
    create_agent_session,
)
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.tui_adapter_pi_rpc import PiRpcAdapter
from pbatch.tui_runtime import (
    CompactionHandle,
    RuntimeInfo,
    RuntimeCompactionResult,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeSessionStats,
    RuntimeThinkingLevels,
    RuntimeTokenTotals,
    TurnHandle,
    UnsupportedOperation,
)


_END = object()


@dataclass
class _Script:
    events: tuple[tuple[str, dict], ...]


class FakeRuntime:
    def __init__(self, *, capabilities=(), scripts=()):
        enabled = {name: True for name in capabilities}
        self.info = RuntimeInfo(
            "fake", 1, RuntimeCapabilities(runtime="fake", capabilities=enabled))
        self.queue: queue.Queue = queue.Queue()
        self.scripts = queue.Queue()
        for script in scripts:
            self.scripts.put(_Script(tuple(script)))
        self.session = None
        self.handles = []
        self.initialize_requests = []
        self.session_requests = []
        self.steers = []
        self.followups = []
        self.interrupts = []
        self.closed = False
        self.turn_started = threading.Event()
        self.seq = 0

    def runtime_info(self):
        return self.info

    def initialize(self, request):
        self.initialize_requests.append(request)
        return self.info

    def start_session(self, request):
        self.session_requests.append(("start", request))
        self.session = RuntimeSession(
            request.session_id, "provider_initial", request.workspace_id,
            "2026-01-01T00:00:00Z", "ACTIVE")
        return self.session

    def resume_session(self, request):
        return self._replace("resume", request)

    def fork_session(self, request):
        return self._replace("fork", request)

    def clone_session(self, request):
        return self._replace("clone", request)

    def _replace(self, operation, request):
        self.session_requests.append((operation, request))
        get = (request.get if isinstance(request, dict)
               else lambda name, default="": getattr(request, name, default))
        self.session = RuntimeSession(
            get("session_id"), get("provider_session_id"),
            get("workspace_id"), "2026-01-01T00:00:00Z", "ACTIVE")
        return self.session

    def start_turn(self, request):
        index = len(self.handles) + 1
        handle = TurnHandle(
            self.session.session_id, f"run_{index}", f"turn_{index}",
            "agent_main", "2026-01-01T00:00:00Z")
        self.handles.append(handle)
        self.turn_started.set()
        if not self.scripts.empty():
            self._emit_script(handle, self.scripts.get_nowait())
        return handle

    def _emit_script(self, handle, script):
        for kind, payload in script.events:
            self.emit(handle, kind, payload)

    def emit(self, handle, kind, payload=None, **identities):
        self.seq += 1
        event = RuntimeEvent(
            event_id=f"evt_{self.seq}", seq=self.seq,
            occurred_at="2026-01-01T00:00:00Z", workspace_id="ws",
            session_id=identities.get("session_id", handle.session_id),
            run_id=identities.get("run_id", handle.run_id),
            turn_id=identities.get("turn_id", handle.turn_id),
            actor_id="agent_main", kind=kind, payload=payload or {})
        self.queue.put(event)
        if kind in sdk.TERMINAL_EVENT_KINDS:
            handle.state = str(event.payload.get("state", ""))
            handle._done.set()

    def steer_turn(self, request):
        self.steers.append(request)

    def follow_up(self, request):
        self.followups.append(request)

    def interrupt_turn(self, request):
        self.interrupts.append(request)

    def events(self):
        while True:
            value = self.queue.get()
            if value is _END:
                return
            if isinstance(value, Exception):
                raise value
            yield value

    def end_stream(self, error=None):
        self.queue.put(error or _END)

    def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put(_END)


class PreReturnEventRuntime(FakeRuntime):
    """Prove the pump may dequeue before start_turn returns, but not dispatch."""

    def __init__(self, script):
        super().__init__(scripts=[script])
        self.event_dequeued = threading.Event()

    def start_turn(self, request):
        handle = super().start_turn(request)
        assert self.event_dequeued.wait(1)
        return handle

    def events(self):
        while True:
            value = self.queue.get()
            if value is _END:
                return
            self.event_dequeued.set()
            yield value


class ControlRuntime(FakeRuntime):
    def __init__(self):
        super().__init__(capabilities=(
            "session_state", "session_stats", "session_compact",
            "model_catalog", "thinking_levels",
            "model_select", "thinking_select"))
        self.control_requests = []
        self.compaction = None

    def _state(self):
        return RuntimeSessionState(
            self.session.session_id, self.session.provider_session_id,
            model=RuntimeModelRef("provider", "model"),
            thinking_level="medium")

    def get_session_state(self, request):
        self.control_requests.append(("state", request))
        return self._state()

    def get_session_stats(self, request):
        self.control_requests.append(("stats", request))
        return RuntimeSessionStats(
            self.session.session_id, self.session.provider_session_id,
            total_messages=3, tokens=RuntimeTokenTotals(total_tokens=8))

    def get_available_models(self, request):
        self.control_requests.append(("models", request))
        return RuntimeModelCatalog(
            self.session.session_id, (RuntimeModelRef("provider", "model"),))

    def get_available_thinking_levels(self, request):
        self.control_requests.append(("levels", request))
        return RuntimeThinkingLevels(
            self.session.session_id, ("off", "medium", "high"))

    def set_model(self, request):
        self.control_requests.append(("model", request))
        return self._state()

    def set_thinking_level(self, request):
        self.control_requests.append(("thinking", request))
        return self._state()

    def start_compaction(self, request):
        self.control_requests.append(("compact", request))
        self.compaction = CompactionHandle(
            "compact_1", self.session.session_id, "2026-01-01T00:00:00Z")
        return self.compaction


class ScriptedPiTransport:
    """Minimal real-adapter transport for the public-session integration."""

    def __init__(self, *, settle=True):
        self.incoming = queue.Queue()
        self.sent = []
        self.closed = False
        self.settle = settle

    def send_line(self, line):
        request = json.loads(line)
        self.sent.append(request)
        command = request["type"]
        data = ({"sessionFile": "/sessions/sdk.jsonl",
                 "sessionId": "sdk", "isStreaming": False}
                if command == "get_state" else {})
        self._push({"id": request["id"], "type": "response",
                    "command": command, "success": True, "data": data})
        if command == "prompt" and self.settle:
            assistant = {"role": "assistant", "content": [
                {"type": "text", "text": "Pi integration"}],
                "stopReason": "stop", "usage": {"input": 1, "output": 2}}
            self._push({"type": "agent_start"})
            self._push({"type": "message_end", "message": assistant})
            self._push({"type": "agent_settled"})

    def _push(self, value):
        self.incoming.put(json.dumps(value) + "\n")

    def recv_line(self):
        return self.incoming.get(timeout=2)

    def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put(None)

    def disconnect(self):
        self.incoming.put(None)


def _options(**values):
    defaults = {"cwd": "/tmp/project", "agent": "fake",
                "workspace_id": "ws", "timeout": 10}
    defaults.update(values)
    return AgentSessionOptions(**defaults)


def _success_script(text="final"):
    return (
        ("turn.started", {"state": TurnState.RUNNING}),
        ("message.delta", {"text": "draft"}),
        ("message.completed", {"text": text}),
        ("turn.completed", {"state": TurnState.SUCCEEDED}),
    )


def _run_prompt(session, result, text="hello"):
    try:
        result.append(session.prompt(text, timeout=1))
    except Exception as exc:  # assertions inspect the captured exception
        result.append(exc)


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return False


def test_prompt_waits_for_terminal_and_completed_message_is_authoritative():
    runtime = FakeRuntime(scripts=[_success_script("authoritative")])
    with create_agent_session(_options(), runtime=runtime) as session:
        result = session.prompt("hello", timeout=1)
    assert isinstance(result, TurnResult)
    assert result.succeeded is True
    assert result.text == "authoritative"
    assert result.state == TurnState.SUCCEEDED
    assert result.terminal_event.kind == "turn.completed"
    assert [event.kind for event in result.events] == [
        "turn.started", "message.delta", "message.completed", "turn.completed"]


def test_event_dequeued_before_start_turn_returns_reaches_new_tracker():
    runtime = PreReturnEventRuntime(_success_script("not lost"))
    with create_agent_session(_options(), runtime=runtime) as session:
        result = session.prompt("race", timeout=1)
    assert result.text == "not lost"
    assert [event.kind for event in result.events] == [
        "turn.started", "message.delta", "message.completed", "turn.completed"]


def test_public_session_runs_over_real_pi_rpc_adapter_contract():
    transport = ScriptedPiTransport()
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    options = _options(agent="pi", runtime="pi", session_name="integration")
    with create_agent_session(options, runtime=adapter) as session:
        result = session.prompt("hello", timeout=1)
        assert session.state["provider_session_id"] == "/sessions/sdk.jsonl"
        assert session.runtime_info().capabilities.message_queue is True
    assert result.text == "Pi integration"
    assert result.succeeded is True
    assert [item["type"] for item in transport.sent] == [
        "get_state", "set_session_name", "get_state", "prompt"]


def test_pi_rpc_eof_wakes_prompt_and_revokes_live_capabilities():
    transport = ScriptedPiTransport(settle=False)
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    session = create_agent_session(
        _options(agent="pi", runtime="pi"), runtime=adapter)
    result = []
    worker = threading.Thread(target=_run_prompt, args=(session, result))
    worker.start()
    assert _wait_for(lambda: any(
        item["type"] == "prompt" for item in transport.sent))
    transport.disconnect()
    worker.join(1)
    assert isinstance(result[0], RuntimeError)
    assert "without a terminal event" in str(result[0])
    assert not any(session.runtime_info().capabilities.capabilities.values())
    session.close()


def test_pi_rpc_busy_follow_up_is_explicitly_queued_then_settles_owner():
    transport = ScriptedPiTransport(settle=False)
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    session = create_agent_session(
        _options(agent="pi", runtime="pi"), runtime=adapter)
    result = []
    worker = threading.Thread(target=_run_prompt, args=(session, result))
    worker.start()
    assert _wait_for(lambda: any(
        item["type"] == "prompt" for item in transport.sent))
    receipt = session.prompt("afterward", streaming_behavior="followUp")
    assert isinstance(receipt, QueueReceipt)
    assert receipt.behavior == "follow_up"
    assert transport.sent[-1]["type"] == "follow_up"
    assistant = {"role": "assistant", "content": [
        {"type": "text", "text": "followed up"}], "stopReason": "stop"}
    transport._push({"type": "agent_start"})
    transport._push({"type": "message_end", "message": assistant})
    transport._push({"type": "agent_settled"})
    worker.join(1)
    session.close()
    assert result[0].text == "followed up"


def test_prompt_falls_back_to_deltas_when_completed_event_is_absent():
    script = (("message.delta", {"text": "one"}),
              ("message.delta", {"text": " two"}),
              ("turn.completed", {"state": TurnState.SUCCEEDED}))
    with create_agent_session(
            _options(), runtime=FakeRuntime(scripts=[script])) as session:
        result = session.prompt("hello", timeout=1)
    assert isinstance(result, TurnResult)
    assert result.text == "one two"


def test_subscribe_and_both_unsubscribe_forms():
    runtime = FakeRuntime(scripts=[
        _success_script("one"), _success_script("two"),
        _success_script("three")])
    with create_agent_session(_options(), runtime=runtime) as session:
        observed = []
        listener = observed.append
        stop = session.subscribe(listener)
        session.prompt("first", timeout=1)
        stop()
        first_count = len(observed)
        session.prompt("second", timeout=1)
        session.subscribe(listener)
        session.unsubscribe(listener)
        session.prompt("third", timeout=1)
    assert first_count == 4
    assert len(observed) == first_count


def test_prompt_events_yields_only_the_matching_turn_identity():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        observed = []
        worker = threading.Thread(
            target=lambda: observed.extend(session.prompt_events("go", timeout=1)))
        worker.start()
        assert runtime.turn_started.wait(1)
        handle = runtime.handles[0]
        runtime.emit(handle, "message.completed", {"text": "wrong"},
                     turn_id="turn_other")
        runtime.emit(handle, "message.completed", {"text": "right"})
        runtime.emit(handle, "turn.completed", {"state": TurnState.SUCCEEDED})
        worker.join(1)
    assert [event.kind for event in observed] == [
        "message.completed", "turn.completed"]
    assert all(event.turn_id == "turn_1" for event in observed)


def test_busy_prompt_requires_behavior_and_queue_receipts_are_explicit():
    capabilities = ("turn_steer", "message_queue", "interrupt")
    runtime = FakeRuntime(capabilities=capabilities)
    with create_agent_session(_options(), runtime=runtime) as session:
        first = []
        worker = threading.Thread(target=_run_prompt, args=(session, first))
        worker.start()
        assert runtime.turn_started.wait(1)
        with pytest.raises(RuntimeError, match="streaming_behavior"):
            session.prompt("ambiguous")
        steer = session.prompt("correct it", streaming_behavior="steer")
        follow = session.prompt("then explain", streaming_behavior="followUp")
        runtime.emit(runtime.handles[0], "turn.completed",
                     {"state": TurnState.SUCCEEDED})
        worker.join(1)
    assert steer == QueueReceipt(True, "steer", steer.session_id, "turn_1")
    assert follow.behavior == "follow_up"
    assert runtime.steers[0].text == "correct it"
    assert runtime.followups[0].text == "then explain"
    assert isinstance(first[0], TurnResult)


def test_queue_and_session_operations_fail_closed_on_capabilities():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        result = []
        worker = threading.Thread(target=_run_prompt, args=(session, result))
        worker.start()
        assert runtime.turn_started.wait(1)
        with pytest.raises(UnsupportedOperation, match="turn_steer"):
            session.steer("change")
        with pytest.raises(UnsupportedOperation, match="message_queue"):
            session.follow_up("later")
        runtime.emit(runtime.handles[0], "turn.failed",
                     {"state": TurnState.FAILED, "reason": "stop"})
        worker.join(1)
        with pytest.raises(UnsupportedOperation, match="session_resume"):
            session.resume_session("provider")


def test_capability_checks_use_the_runtime_current_snapshot():
    runtime = FakeRuntime(capabilities=("message_queue",))
    with create_agent_session(_options(), runtime=runtime) as session:
        result = []
        worker = threading.Thread(target=_run_prompt, args=(session, result))
        worker.start()
        assert runtime.turn_started.wait(1)
        runtime.info = RuntimeInfo(
            "fake", 1, RuntimeCapabilities(runtime="fake"))
        assert session.runtime_info().capabilities.message_queue is False
        with pytest.raises(UnsupportedOperation, match="message_queue"):
            session.follow_up("must not be accepted")
        runtime.emit(runtime.handles[0], "turn.failed",
                     {"state": TurnState.FAILED})
        worker.join(1)


def test_abort_checks_capability_and_targets_active_turn():
    runtime = FakeRuntime(capabilities=("interrupt",))
    with create_agent_session(_options(), runtime=runtime) as session:
        result = []
        worker = threading.Thread(target=_run_prompt, args=(session, result))
        worker.start()
        assert runtime.turn_started.wait(1)
        session.abort()
        runtime.emit(runtime.handles[0], "turn.interrupted",
                     {"state": TurnState.INTERRUPTED})
        worker.join(1)
    assert runtime.interrupts[0].turn_id == "turn_1"
    assert result[0].terminal_event.kind == "turn.interrupted"


def test_late_previous_turn_events_cannot_leak_into_next_turn():
    runtime = FakeRuntime(scripts=[_success_script("first")])
    with create_agent_session(_options(), runtime=runtime) as session:
        first = session.prompt("first", timeout=1)
        runtime.turn_started.clear()
        result = []
        worker = threading.Thread(target=_run_prompt,
                                  args=(session, result, "second"))
        worker.start()
        assert runtime.turn_started.wait(1)
        runtime.emit(runtime.handles[0], "message.completed", {"text": "late"})
        runtime.emit(runtime.handles[1], "message.completed", {"text": "second"})
        runtime.emit(runtime.handles[1], "turn.completed",
                     {"state": TurnState.SUCCEEDED})
        worker.join(1)
    assert first.text == "first"
    assert result[0].text == "second"


def test_stream_failure_and_timeout_are_reported_without_false_terminal():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        result = []
        worker = threading.Thread(target=_run_prompt, args=(session, result))
        worker.start()
        assert runtime.turn_started.wait(1)
        runtime.end_stream(ValueError("broken stream"))
        worker.join(1)
    assert isinstance(result[0], RuntimeError)
    assert "event stream failed" in str(result[0])

    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        with pytest.raises(TimeoutError):
            session.prompt("wait", timeout=0.01)


def test_session_replacement_clone_and_busy_guard():
    capabilities = ("session_resume", "session_fork")
    runtime = FakeRuntime(capabilities=capabilities)
    with create_agent_session(_options(), runtime=runtime) as session:
        resumed = session.resume_session("provider_resume")
        cloned = session.clone_session("provider_clone")
        fresh = session.new_session(name="fresh")
    assert resumed.provider_session_id == "provider_resume"
    assert cloned.provider_session_id == "provider_clone"
    assert fresh.session_id != cloned.session_id
    assert [item[0] for item in runtime.session_requests] == [
        "start", "resume", "clone", "start"]


def test_close_and_context_manager_are_idempotent_and_safe():
    runtime = FakeRuntime()
    session = create_agent_session(_options(), runtime=runtime)
    assert session.state["closed"] is False
    session.close()
    session.close()
    assert runtime.closed is True
    assert session.state["closed"] is True
    with pytest.raises(RuntimeError, match="closed"):
        session.prompt("no")


def test_close_wakes_a_blocked_prompt_without_a_false_terminal():
    runtime = FakeRuntime()
    session = create_agent_session(_options(), runtime=runtime)
    result = []
    worker = threading.Thread(target=_run_prompt, args=(session, result))
    worker.start()
    assert runtime.turn_started.wait(1)
    session.close()
    worker.join(1)
    assert worker.is_alive() is False
    assert isinstance(result[0], RuntimeError)
    assert "closed" in str(result[0])


@pytest.mark.parametrize(
    ("agent", "factory_name"),
    [("/opt/bin/pi", "_create_pi_runtime"),
     ("/opt/bin/codex --flag", "_create_codex_runtime"),
     ("other-agent", "_create_legacy_runtime")],
)
def test_auto_factory_selects_by_executable_basename(monkeypatch, agent,
                                                     factory_name):
    runtime = FakeRuntime()
    selected = []

    def factory(options):
        selected.append((factory_name, options.agent))
        return runtime

    monkeypatch.setattr(sdk, factory_name, factory)
    with create_agent_session(_options(agent=agent)):
        pass
    assert selected == [(factory_name, agent)]


def test_injected_runtime_bypasses_auto_factory(monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr(
        sdk, "_create_runtime",
        lambda _options: pytest.fail("auto factory must not be called"))
    with create_agent_session(_options(agent="pi"), runtime=runtime) as session:
        assert session.runtime_info().runtime == "fake"


def test_initialization_failure_closes_injected_runtime():
    runtime = FakeRuntime()

    def fail(_request):
        raise RuntimeError("handshake failed")

    runtime.initialize = fail
    with pytest.raises(RuntimeError, match="handshake failed"):
        create_agent_session(_options(), runtime=runtime)
    assert runtime.closed is True


def test_invalid_prompt_behavior_and_options_fail_before_provider_turn():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        with pytest.raises(ValueError, match="prompt must not be empty"):
            session.prompt("  ")
        with pytest.raises(ValueError, match="steer or followUp"):
            session.prompt("hello", streaming_behavior="later")
    assert runtime.handles == []

    with pytest.raises(ValueError, match="unsupported runtime"):
        create_agent_session(_options(runtime="mystery"), runtime=FakeRuntime())
    with pytest.raises(ValueError, match="only valid for Pi RPC"):
        create_agent_session(_options(
            runtime="codex", allow_persistent_settings=True))


def test_default_pi_binary_override_reaches_python_runtime_selection(monkeypatch):
    from pbatch import config

    monkeypatch.setattr(config, "AGENT_BIN", "pi")
    monkeypatch.setenv("PBATCH_PI_BIN", "/path with spaces/pi-edge")
    normalized = sdk._normalize_options(AgentSessionOptions())
    explicit = sdk._normalize_options(AgentSessionOptions(agent="pi"))

    assert normalized.agent == "/path with spaces/pi-edge"
    assert normalized.runtime == "pi"
    assert explicit.agent == "pi"
    assert explicit.runtime == "auto"


def test_public_control_facade_returns_typed_state_stats_and_readbacks():
    runtime = ControlRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        state = session.runtime_state()
        stats = session.stats()
        models = session.get_available_models()
        levels = session.get_available_thinking_levels()
        selected = session.set_model("provider", "model")
        thinking = session.set_thinking_level("high")

    assert state.model == RuntimeModelRef("provider", "model")
    assert stats.tokens.total_tokens == 8
    assert models.models[0].model_id == "model"
    assert levels.levels == ("off", "medium", "high")
    assert selected.model.model_id == "model"
    assert thinking.thinking_level == "medium"
    assert [name for name, _request in runtime.control_requests] == [
        "state", "stats", "models", "levels", "model", "thinking"]
    assert all(request.session_id == state.session_id
               for _name, request in runtime.control_requests)


def test_public_control_facade_fails_closed_without_capabilities():
    with create_agent_session(_options(), runtime=FakeRuntime()) as session:
        with pytest.raises(UnsupportedOperation, match="session_state"):
            session.runtime_state()
        with pytest.raises(UnsupportedOperation, match="session_stats"):
            session.stats()
        with pytest.raises(UnsupportedOperation, match="model_catalog"):
            session.get_available_models()
        with pytest.raises(UnsupportedOperation, match="thinking_levels"):
            session.get_available_thinking_levels()
        with pytest.raises(UnsupportedOperation, match="model_select"):
            session.set_model("provider", "model")
        with pytest.raises(UnsupportedOperation, match="thinking_select"):
            session.set_thinking_level("high")
        with pytest.raises(UnsupportedOperation, match="session_compact"):
            session.start_compaction()


def test_compaction_timeout_preserves_handle_for_late_authoritative_result():
    runtime = ControlRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        with pytest.raises(CompactionTimeout) as captured:
            session.compact("focus", timeout=0.01)
        handle = captured.value.handle
        assert handle is runtime.compaction
        assert handle.state == "RUNNING"
        assert session.state["busy"] is True
        assert session.state["compacting"] is True
        with pytest.raises(RuntimeError, match="compaction"):
            session.prompt("must wait")
        expected = RuntimeCompactionResult(
            session.session.session_id, "summary")
        handle._set_result(expected)
        assert handle.result(timeout=1) is expected
        assert session.state["busy"] is False
