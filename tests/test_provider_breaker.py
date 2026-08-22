"""T17a: provider-level circuit breaker tests (retro finding)."""

from __future__ import annotations

import time

import pytest

from pbatch import provider_breaker as breaker


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch):
    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 3, "base_cooldown": 0.1, "max_cooldown": 2.0})
    breaker._streak = 0
    breaker._cooldown_until = 0.0
    breaker._cooldown_step = 0
    yield
    breaker._streak = 0
    breaker._cooldown_until = 0.0
    breaker._cooldown_step = 0


def test_trips_after_threshold_and_recovers_on_success():
    assert not breaker.register_provider_failure("quota")
    assert not breaker.register_provider_failure("quota")
    assert breaker.register_provider_failure("quota")  # 3rd trips cooldown
    assert breaker.cooldown_remaining() > 0
    breaker.register_success()
    assert breaker.cooldown_remaining() == 0
    assert not breaker.register_provider_failure("x")  # fresh window


def test_wait_or_fail_waits_then_passes():
    breaker.register_provider_failure("quota")
    breaker.register_provider_failure("quota")
    breaker.register_provider_failure("quota")  # cooldown 0.2s
    assert breaker.wait_or_fail(0.3) is None  # waited through the cooldown
    assert breaker.cooldown_remaining() == 0


def test_wait_budget_exceeded_returns_failure_reason():
    breaker._cooldown_until = time.monotonic() + 5.0  # long cooldown
    reason = breaker.wait_or_fail(0.05)
    assert reason == "provider breaker cooldown exceeded wait budget"


def test_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(breaker, "_settings", lambda: {"threshold": 0})
    assert not breaker.enabled()
    assert not breaker.register_provider_failure("quota")
    assert breaker.wait_or_fail(0.0) is None


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
    monkeypatch.setattr(breaker, "_cooldown_until", 0.0)
    assert runner._pb_cooldown(task) is None  # no cooldown: run
    # cooldown LONGER than the runner wait budget -> fails fast with reason
    breaker._cooldown_until = time.monotonic() + 400.0
    result = runner._pb_cooldown(task)
    assert result is not None and not result.success
    assert result.reason == "provider breaker cooldown exceeded wait budget"