"""T17a: provider-level circuit breaker tests (retro finding)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pbatch import provider_breaker as breaker


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch, tmp_path):
    state_file = tmp_path / "provider-breaker.json"
    monkeypatch.setenv("PBATCH_PROVIDER_BREAKER_FILE", str(state_file))
    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 3, "base_cooldown": 0.1, "max_cooldown": 2.0})
    breaker.register_success()  # fresh shared state
    yield
    breaker.register_success()


def test_trips_after_threshold_and_recovers_on_success():
    assert not breaker.register_provider_failure("quota")
    assert not breaker.register_provider_failure("quota")
    assert breaker.register_provider_failure("quota")  # 3rd trips cooldown
    assert breaker.cooldown_remaining() > 0
    breaker.register_success()
    assert breaker.cooldown_remaining() == 0
    assert breaker.breaker_state()["streak"] == 0
    assert not breaker.register_provider_failure("x")  # fresh window


def test_wait_or_fail_waits_then_passes():
    breaker.register_provider_failure("quota")
    breaker.register_provider_failure("quota")
    breaker.register_provider_failure("quota")  # cooldown 0.2s
    assert breaker.wait_or_fail(0.3) is None  # waited through the cooldown
    assert breaker.cooldown_remaining() == 0


def test_wait_budget_exceeded_returns_failure_reason():
    import json
    from pathlib import Path
    state = {"streak": 0, "step": 1,
             "cooldown_until": time.monotonic() + 30.0}
    Path(breaker._state_path()).write_text(json.dumps(state), encoding="utf-8")
    reason = breaker.wait_or_fail(0.05)
    assert reason == "provider breaker cooldown exceeded wait budget"


def test_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(breaker, "_settings", lambda: {"threshold": 0})
    assert not breaker.enabled()
    assert not breaker.register_provider_failure("quota")
    assert breaker.wait_or_fail(0.0) is None


def test_corrupt_shared_state_is_recovered_fail_closed(tmp_path):
    state_file = tmp_path / "provider-breaker.json"
    state_file.write_text('{not-json', encoding="utf-8")
    assert not breaker.register_provider_failure("quota")
    state = breaker.breaker_state()
    assert state["streak"] == 1 and state["cooldown_remaining_s"] == 0


def test_runner_wires_breaker_into_result_path(monkeypatch, tmp_path):
    """A provider-failure rejection registers; success resets; the next
    task in cooldown fails with an explicit reason."""
    from pbatch import runner
    from pbatch.models import Task
    calls = []

    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 2, "base_cooldown": 60.0, "max_cooldown": 60.0})
    reason = runner.agent_failure_reason(0, "ERROR: insufficient_quota")
    assert reason
    runner._pb_register_fail(reason)
    assert breaker.cooldown_remaining() == 0  # below threshold
    runner._pb_register_fail(reason)  # trips
    assert breaker.cooldown_remaining() > 0

    task = Task(prompt="p")
    breaker.register_success()  # clear shared state
    assert runner._pb_cooldown(task) is None  # no cooldown: run
    # cooldown LONGER than the runner wait budget -> fails fast with reason
    Path(breaker._state_path()).write_text(json.dumps({
        "streak": 0, "step": 1,
        "cooldown_until": time.monotonic() + 400.0}), encoding="utf-8")
    result = runner._pb_cooldown(task)
    assert result is not None and not result.success
    assert result.reason == "provider breaker cooldown exceeded wait budget"