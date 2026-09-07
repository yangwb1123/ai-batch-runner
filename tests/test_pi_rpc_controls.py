"""Failure-first contract tests for the managed Pi RPC control surface."""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from pbatch import tui_adapter_pi_rpc as adapter_module
from pbatch.pi_version import PiVersionProbe, parse_pi_stable_version
from pbatch.tui_adapter_pi_rpc import PiRpcAdapter
from pbatch.tui_runtime import (
    ActiveSessionRequest,
    CompactSessionRequest,
    CompactionHandle,
    RuntimeCompactionResult,
    RuntimeContextUsage,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSessionState,
    RuntimeSessionStats,
    RuntimeThinkingLevels,
    RuntimeTokenTotals,
    SetModelRequest,
    SetThinkingLevelRequest,
    UnsupportedOperation,
)


_MISSING = object()


class ControlTransport:
    """Strict fake transport with test-controlled response timing."""

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
            self, request: dict, *, data=_MISSING,
            success: bool = True, error: str = "") -> None:
        value = {
            "id": request["id"], "type": "response",
            "command": request["type"], "success": success,
        }
        if data is not _MISSING:
            value["data"] = data
        if error:
            value["error"] = error
        self.push(value)

    def push(self, value: dict) -> None:
        self.incoming.put(json.dumps(value, ensure_ascii=False) + "\n")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.incoming.put(None)


def _provider_state(
        *, path: str = "/sessions/control.jsonl", thinking: str = "medium",
        streaming: bool = False, compacting: bool = False) -> dict:
    return {
        "sessionFile": path,
        "sessionId": "provider-control",
        "sessionName": "control",
        "model": {
            "provider": "anthropic", "id": "claude-test", "name": "Claude Test",
        },
        "thinkingLevel": thinking,
        "isStreaming": streaming,
        "isCompacting": compacting,
        "steeringMode": "one-at-a-time",
        "followUpMode": "one-at-a-time",
        "autoCompactionEnabled": True,
        "messageCount": 7,
        "pendingMessageCount": 2,
    }


def _stats(path: str = "/sessions/control.jsonl") -> dict:
    return {
        "sessionFile": path,
        "sessionId": "provider-control",
        "userMessages": 3,
        "assistantMessages": 4,
        "toolCalls": 5,
        "toolResults": 5,
        "totalMessages": 17,
        "tokens": {
            "input": 100, "output": 20, "cacheRead": 30,
            "cacheWrite": 4, "total": 154,
        },
        "cost": 0.125,
        "contextUsage": {
            "tokens": 80, "contextWindow": 200, "percent": 40.0,
        },
    }


def _compaction_data() -> dict:
    return {
        "summary": "bounded summary",
        "firstKeptEntryId": "entry-kept",
        "tokensBefore": 1000,
        "estimatedTokensAfter": 250,
        "usage": {
            "input": 100, "output": 20, "cacheRead": 30,
            "cacheWrite": 4, "totalTokens": 154,
            "cost": {"total": 0.125},
        },
        "details": {"strategy": "default"},
    }


def _invoke(function, *args):
    outcome = []

    def run() -> None:
        try:
            outcome.append(function(*args))
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
        transport: ControlTransport, *, allow_settings: bool = False,
        safe_version: str = "", timeout: float = 5.0) -> PiRpcAdapter:
    probe = None
    if safe_version:
        probe = PiVersionProbe(
            "/test/pi", "observed", parse_pi_stable_version(safe_version))
    adapter = PiRpcAdapter(
        "/tmp/project", transport=transport, timeout=timeout,
        allow_persistent_settings=allow_settings, version_probe=probe)
    worker, outcome = _invoke(adapter.initialize, {})
    transport.respond(transport.wait_request(0), data=_provider_state())
    info = _finish(worker, outcome)
    assert info.runtime == "pi-rpc"
    return adapter


def _responding_call(
        transport: ControlTransport, index: int, function,
        request=_MISSING, *, data=_MISSING):
    worker, outcome = (_invoke(function) if request is _MISSING
                       else _invoke(function, request))
    wire = transport.wait_request(index)
    transport.respond(wire, data=data)
    return _finish(worker, outcome), wire


def test_control_capabilities_are_live_and_persistent_setters_require_opt_in():
    transport = ControlTransport()
    adapter = PiRpcAdapter("/tmp/project", transport=transport)
    before = adapter.runtime_info().capabilities
    assert not any(before.supports(name) for name in (
        "session_state", "session_stats", "session_compact",
        "model_select", "thinking_select",
    ))

    worker, outcome = _invoke(adapter.initialize, {})
    transport.respond(transport.wait_request(0), data=_provider_state())
    after = _finish(worker, outcome).capabilities
    assert all(after.supports(name) for name in (
        "session_state", "session_stats", "session_compact",
        "model_catalog", "thinking_levels",
    ))
    assert after.model_select is False
    assert after.thinking_select is False

    sent = len(transport.sent)
    with pytest.raises(UnsupportedOperation, match="model_select"):
        adapter.set_model(SetModelRequest(
            "anthropic", "claude-test", adapter.session.session_id))
    with pytest.raises(UnsupportedOperation, match="thinking_select"):
        adapter.set_thinking_level(SetThinkingLevelRequest(
            "high", adapter.session.session_id))
    assert len(transport.sent) == sent
    adapter.close()


def test_default_runtime_reuses_the_exact_probed_executable(monkeypatch):
    transport = ControlTransport()
    probe = PiVersionProbe(
        "/resolved/pi", "observed", parse_pi_stable_version("0.84.3"))
    probe_calls = []
    transport_calls = []

    def fake_probe(agent, *, cwd):
        probe_calls.append((agent, cwd))
        return probe

    def fake_transport(cwd, **kwargs):
        transport_calls.append((cwd, kwargs))
        return transport

    monkeypatch.setattr(adapter_module, "probe_pi_version", fake_probe)
    monkeypatch.setattr(adapter_module, "DefaultPiRpcTransport", fake_transport)
    adapter = PiRpcAdapter("/tmp/project", agent="pi")
    worker, outcome = _invoke(adapter.initialize, {})
    transport.respond(transport.wait_request(0), data=_provider_state())
    info = _finish(worker, outcome)

    assert probe_calls == [("pi", "/tmp/project")]
    assert transport_calls[0][1]["agent"] == "/resolved/pi"
    assert info.runtime_version == "0.84.3"
    assert info.settings_scope == "session"
    assert info.capabilities.model_select is True
    adapter.close()


def test_state_and_stats_use_exact_active_session_and_typed_wire_readback():
    transport = ControlTransport()
    adapter = _initialized(transport)
    session_id = adapter.session.session_id
    state, state_wire = _responding_call(
        transport, 1, adapter.get_session_state, data=_provider_state())
    stats, stats_wire = _responding_call(
        transport, 2, adapter.get_session_stats,
        ActiveSessionRequest(session_id), data=_stats())

    assert state_wire == {
        "id": state_wire["id"], "type": "get_state"}
    assert state == RuntimeSessionState(
        session_id=session_id,
        provider_session_id="/sessions/control.jsonl",
        name="control",
        model=RuntimeModelRef("anthropic", "claude-test", "Claude Test"),
        thinking_level="medium",
        is_streaming=False,
        is_compacting=False,
        auto_compaction_enabled=True,
        message_count=7,
        pending_message_count=2,
    )
    assert stats_wire == {
        "id": stats_wire["id"], "type": "get_session_stats"}
    assert stats == RuntimeSessionStats(
        session_id=session_id,
        provider_session_id="/sessions/control.jsonl",
        user_messages=3,
        assistant_messages=4,
        tool_calls=5,
        tool_results=5,
        total_messages=17,
        tokens=RuntimeTokenTotals(100, 20, 30, 4, 154),
        cost=0.125,
        context_usage=RuntimeContextUsage(80, 200, 40.0),
    )
    adapter.close()


def test_safe_version_setters_use_exact_wire_then_authoritative_state_readback():
    transport = ControlTransport()
    adapter = _initialized(transport, safe_version="0.84.3")
    caps = adapter.runtime_info().capabilities
    assert caps.model_select is True
    assert caps.thinking_select is True
    assert adapter.runtime_info().runtime_version == "0.84.3"
    assert adapter.runtime_info().settings_scope == "session"

    model_worker, model_outcome = _invoke(
        adapter.set_model, SetModelRequest(
            "anthropic", "claude-test", adapter.session.session_id))
    model_catalog = transport.wait_request(1)
    transport.respond(model_catalog, data={"models": [
        {"provider": "anthropic", "id": "claude-test", "name": "Claude Test"},
    ]})
    model_wire = transport.wait_request(2)
    transport.respond(model_wire, data={
        "provider": "anthropic", "id": "claude-test", "name": "response-name"})
    model_readback = transport.wait_request(3)
    transport.respond(model_readback, data=_provider_state())
    model_levels = transport.wait_request(4)
    transport.respond(model_levels, data={"levels": ["off", "medium", "high"]})
    model_state = _finish(model_worker, model_outcome)

    assert model_catalog["type"] == "get_available_models"
    assert model_wire == {
        "id": model_wire["id"], "type": "set_model",
        "provider": "anthropic", "modelId": "claude-test",
    }
    assert model_readback["type"] == "get_state"
    assert model_levels["type"] == "get_available_thinking_levels"
    assert model_state.model.name == "Claude Test"

    thinking_worker, thinking_outcome = _invoke(
        adapter.set_thinking_level, SetThinkingLevelRequest(
            "high", adapter.session.session_id))
    thinking_levels = transport.wait_request(5)
    transport.respond(thinking_levels, data={"levels": ["off", "medium", "high"]})
    thinking_wire = transport.wait_request(6)
    transport.respond(thinking_wire)
    thinking_readback = transport.wait_request(7)
    transport.respond(
        thinking_readback, data=_provider_state(thinking="medium"))
    thinking_state = _finish(thinking_worker, thinking_outcome)

    assert thinking_levels["type"] == "get_available_thinking_levels"
    assert thinking_wire == {
        "id": thinking_wire["id"], "type": "set_thinking_level", "level": "high"}
    assert thinking_readback["type"] == "get_state"
    assert thinking_state.thinking_level == "medium"  # provider clamp is authoritative
    adapter.close()


def test_confirmed_model_change_survives_thinking_level_refresh_failure():
    transport = ControlTransport()
    adapter = _initialized(transport, safe_version="0.84.3")

    worker, outcome = _invoke(
        adapter.set_model, SetModelRequest(
            "anthropic", "claude-test", adapter.session.session_id))
    model_catalog = transport.wait_request(1)
    transport.respond(model_catalog, data={"models": [
        {"provider": "anthropic", "id": "claude-test", "name": "Claude Test"},
    ]})
    model_wire = transport.wait_request(2)
    transport.respond(model_wire)
    model_readback = transport.wait_request(3)
    transport.respond(model_readback, data=_provider_state())
    levels_refresh = transport.wait_request(4)
    transport.respond(
        levels_refresh, success=False, error="thinking catalog unavailable")

    state = _finish(worker, outcome)
    assert isinstance(state, RuntimeSessionState)
    assert state.model == RuntimeModelRef(
        "anthropic", "claude-test", "Claude Test")
    assert levels_refresh["type"] == "get_available_thinking_levels"
    warning = adapter.events().get(timeout=1)
    assert warning.kind == "runtime.warning"
    assert warning.payload["operation"] == "get_available_thinking_levels"
    assert warning.payload["model_change_confirmed"] is True
    assert "thinking-level refresh failed" in warning.payload["text"]
    adapter.close()


def test_catalog_and_dynamic_thinking_reads_are_redacted_and_typed():
    transport = ControlTransport()
    adapter = _initialized(transport)
    session_id = adapter.session.session_id
    models, models_wire = _responding_call(
        transport, 1, adapter.get_available_models,
        ActiveSessionRequest(session_id), data={"models": [{
            "provider": "openai", "id": "gpt-test", "name": "GPT Test",
            "api": "responses", "baseUrl": "https://secret.invalid",
            "headers": {"Authorization": "secret"},
        }]})
    levels, levels_wire = _responding_call(
        transport, 2, adapter.get_available_thinking_levels,
        ActiveSessionRequest(session_id), data={"levels": []})

    assert models_wire["type"] == "get_available_models"
    assert models == RuntimeModelCatalog(
        session_id, (RuntimeModelRef("openai", "gpt-test", "GPT Test"),))
    assert "secret" not in repr(models)
    assert levels_wire["type"] == "get_available_thinking_levels"
    assert levels == RuntimeThinkingLevels(session_id, ())
    adapter.close()


def test_explicit_legacy_settings_opt_in_is_labeled_global():
    transport = ControlTransport()
    adapter = _initialized(transport, allow_settings=True)
    info = adapter.runtime_info()
    assert info.settings_scope == "global"
    assert info.capabilities.model_select is True
    adapter.close()


def test_active_turn_allows_reads_but_rejects_mutating_controls_before_wire():
    transport = ControlTransport()
    adapter = _initialized(transport, allow_settings=True)
    session_id = adapter.session.session_id
    turn_worker, turn_outcome = _invoke(
        adapter.start_turn, {"prompt": "work", "turn_id": "turn-control"})
    transport.respond(transport.wait_request(1))
    handle = _finish(turn_worker, turn_outcome)

    state, _ = _responding_call(
        transport, 2, adapter.get_session_state,
        ActiveSessionRequest(session_id),
        data=_provider_state(streaming=True))
    stats, _ = _responding_call(
        transport, 3, adapter.get_session_stats,
        ActiveSessionRequest(session_id), data=_stats())
    assert state.is_streaming is True
    assert stats.total_messages == 17

    sent = len(transport.sent)
    with pytest.raises(RuntimeError, match="active Pi turn"):
        adapter.set_model(SetModelRequest(
            "anthropic", "claude-test", session_id))
    with pytest.raises(RuntimeError, match="active Pi turn"):
        adapter.set_thinking_level(SetThinkingLevelRequest("high", session_id))
    with pytest.raises(RuntimeError, match="active Pi turn"):
        adapter.start_compaction(CompactSessionRequest(session_id, "summarize"))
    assert len(transport.sent) == sent

    transport.push({"type": "agent_settled"})
    assert handle.wait(1)
    adapter.close()


def test_stale_session_controls_fail_before_any_provider_command():
    transport = ControlTransport()
    adapter = _initialized(transport, allow_settings=True)
    sent = len(transport.sent)
    stale = ActiveSessionRequest("ses-stale")

    with pytest.raises(ValueError, match="active session"):
        adapter.get_session_state(stale)
    with pytest.raises(ValueError, match="active session"):
        adapter.get_session_stats(stale)
    with pytest.raises(ValueError, match="active session"):
        adapter.set_model(SetModelRequest(
            "anthropic", "claude-test", "ses-stale"))
    with pytest.raises(ValueError, match="active session"):
        adapter.set_thinking_level(SetThinkingLevelRequest(
            "high", "ses-stale"))
    with pytest.raises(ValueError, match="active session"):
        adapter.start_compaction(CompactSessionRequest("ses-stale", "summary"))
    assert len(transport.sent) == sent
    adapter.close()


def test_compaction_handle_times_out_then_accepts_one_late_typed_result():
    transport = ControlTransport()
    adapter = _initialized(transport, timeout=0.02)
    session_id = adapter.session.session_id
    handle = adapter.start_compaction(CompactSessionRequest(
        session_id=session_id, instructions="focus on decisions"))
    assert isinstance(handle, CompactionHandle)
    wire = transport.wait_request(1)
    assert wire == {
        "id": wire["id"], "type": "compact",
        "customInstructions": "focus on decisions",
    }
    assert handle.state == "RUNNING"
    assert handle.done is False
    assert handle.wait(0.06) is False  # exceeds ordinary RPC response timeout
    with pytest.raises(TimeoutError, match="timed out"):
        handle.result(timeout=0)

    sent = len(transport.sent)
    with pytest.raises(RuntimeError, match="compaction.*running"):
        adapter.start_compaction(CompactSessionRequest(session_id, "second"))
    assert len(transport.sent) == sent

    transport.respond(wire, data=_compaction_data())
    assert handle.wait(1)
    assert handle.done is True
    assert handle.state == "SUCCEEDED"
    assert handle.result() == RuntimeCompactionResult(
        session_id=session_id,
        summary="bounded summary",
        summary_truncated=False,
        tokens_before=1000,
        estimated_tokens_after=250,
        tokens=RuntimeTokenTotals(100, 20, 30, 4, 154),
        cost=0.125,
    )
    assert handle.result() is handle.result()
    adapter.close()


def test_close_finishes_pending_compaction_unknown_and_late_data_cannot_revive_it():
    transport = ControlTransport()
    adapter = _initialized(transport)
    handle = adapter.start_compaction(CompactSessionRequest(
        session_id=adapter.session.session_id, instructions="summary"))
    wire = transport.wait_request(1)
    adapter.close()

    assert handle.wait(1)
    assert handle.done is True
    assert handle.state == "UNKNOWN"
    with pytest.raises(RuntimeError, match="closed"):
        handle.result()

    transport.respond(wire, data=_compaction_data())
    time.sleep(0.01)
    assert handle.state == "UNKNOWN"
    with pytest.raises(RuntimeError, match="closed"):
        handle.result()
