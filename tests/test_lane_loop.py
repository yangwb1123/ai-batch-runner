"""Supervision policy: anchored window, liveness-only restarts, bounded budget."""
from __future__ import annotations

import json

import pytest

from pbatch import config, lane_loop


@pytest.fixture(autouse=True)
def _private_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", "")
    yield


def test_window_is_anchored_once_and_a_restart_cannot_extend_it(tmp_path):
    first = lane_loop.start_window(tmp_path, hours=12, invocations=40, now=1000.0)
    again = lane_loop.start_window(tmp_path, hours=12, invocations=40, now=9000.0)
    assert first == again
    assert again.deadline_at == 1000.0 + 12 * 3600
    assert again.remaining_seconds(9000.0) == pytest.approx(1000.0 + 12 * 3600 - 9000.0)


def test_requesting_a_different_budget_refuses_instead_of_reinterpreting(tmp_path):
    lane_loop.start_window(tmp_path, hours=12, invocations=40, now=1000.0)
    with pytest.raises(lane_loop.LaneLedgerError, match="differs"):
        lane_loop.start_window(tmp_path, hours=12, invocations=400, now=1000.0)


@pytest.mark.parametrize("hours", [0, -1, 24 * 8, float("nan"), float("inf"), True])
def test_invalid_windows_fail_closed(tmp_path, hours):
    with pytest.raises(ValueError):
        lane_loop.start_window(tmp_path, hours=hours, invocations=10)


@pytest.mark.parametrize("budget", [0, -3, True, 1.5, "10"])
def test_invalid_budgets_fail_closed(tmp_path, budget):
    with pytest.raises(ValueError):
        lane_loop.start_window(tmp_path, hours=1, invocations=budget)


def test_completed_round_allows_the_next_round(tmp_path):
    lane_loop.start_window(tmp_path, hours=12, invocations=10, now=1000.0)
    decision = lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED,
                                invocations=2, now=1100.0)
    assert decision.action == lane_loop.FINISH and decision.consumed_invocations == 2


def test_missing_terminal_receipt_is_the_only_restart_reason(tmp_path):
    lane_loop.start_window(tmp_path, hours=12, invocations=10, now=1000.0)
    assert lane_loop.classify(terminal_receipt=False, exit_code=None, gate_ok=None) == lane_loop.CRASHED
    decision = lane_loop.decide(tmp_path, classification=lane_loop.CRASHED,
                                invocations=2, now=1100.0)
    assert decision.action == lane_loop.RESTART
    assert decision.backoff_seconds == 30.0


def test_restarts_back_off_and_stop_at_the_bounded_attempt_count(tmp_path):
    lane_loop.start_window(tmp_path, hours=12, invocations=50, now=1000.0)
    backoffs = []
    for step in range(3):
        decision = lane_loop.decide(tmp_path, classification=lane_loop.CRASHED,
                                    invocations=1, now=1100.0 + step)
        assert decision.action == lane_loop.RESTART
        backoffs.append(decision.backoff_seconds)
    assert backoffs == [30.0, 60.0, 120.0]
    final = lane_loop.decide(tmp_path, classification=lane_loop.CRASHED,
                             invocations=1, now=1200.0)
    assert final.action == lane_loop.STOP and "restart budget" in final.reason


@pytest.mark.parametrize("classification", [lane_loop.GATE_FAILED, lane_loop.BLOCKED])
def test_deterministic_failures_stop_instead_of_retrying(tmp_path, classification):
    lane_loop.start_window(tmp_path, hours=12, invocations=50, now=1000.0)
    decision = lane_loop.decide(tmp_path, classification=classification,
                                invocations=2, now=1100.0)
    assert decision.action == lane_loop.STOP
    assert "deterministic" in decision.reason


def test_deadline_and_budget_stop_before_new_work(tmp_path):
    lane_loop.start_window(tmp_path, hours=1, invocations=4, now=1000.0)
    lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED, invocations=2, now=1100.0)
    budget = lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED,
                              invocations=2, now=1200.0)
    assert budget.action == lane_loop.STOP and "budget" in budget.reason
    late = lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED,
                            invocations=1, now=1000.0 + 3600.0)
    assert late.action == lane_loop.STOP and "already stopped" in late.reason


def test_stop_is_terminal_and_recorded_once(tmp_path):
    lane_loop.start_window(tmp_path, hours=1, invocations=1, now=1000.0)
    first = lane_loop.decide(tmp_path, classification=lane_loop.BLOCKED, invocations=1, now=1001.0)
    second = lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED, invocations=0, now=1002.0)
    assert first.action == second.action == lane_loop.STOP
    assert "already stopped" in second.reason
    rows = lane_loop.status(tmp_path, now=1003.0)
    assert rows["stopped"] == "round blocked: retrying a deterministic failure hides it"


@pytest.mark.parametrize("content", ["not json\n", '{"kind":"x"}\n', "\n"])
def test_corrupt_or_unanchored_ledger_stops_without_starting_a_new_window(tmp_path, content):
    path = lane_loop.ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(lane_loop.LaneLedgerError):
        lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED, invocations=1, now=10.0)


def test_status_reports_unknown_before_any_window(tmp_path):
    assert lane_loop.status(tmp_path)["anchored"] is False


def test_ledger_lines_are_bounded_and_readable(tmp_path):
    lane_loop.start_window(tmp_path, hours=12, invocations=10, now=1000.0)
    lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED, invocations=1, now=1100.0)
    path = lane_loop.ledger_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(len(line.encode("utf-8")) <= lane_loop.LEDGER_MAX_LINE_BYTES for line in lines)
    assert all(json.loads(line)["schema_version"] == lane_loop.SCHEMA_VERSION for line in lines)


def test_operator_stop_is_recorded_once_and_is_terminal(tmp_path):
    lane_loop.start_window(tmp_path, hours=12, invocations=10, now=1000.0)
    lane_loop.stop(tmp_path, "backlog exhausted", now=1100.0)
    lane_loop.stop(tmp_path, "second stop", now=1200.0)
    assert lane_loop.status(tmp_path)["stopped"] == "backlog exhausted"
    decision = lane_loop.decide(tmp_path, classification=lane_loop.COMPLETED,
                                invocations=0, now=1300.0)
    assert decision.action == lane_loop.STOP


def test_stop_without_a_window_is_refused(tmp_path):
    with pytest.raises(lane_loop.LaneLedgerError):
        lane_loop.stop(tmp_path, "no window")


def test_stop_requires_a_reason(tmp_path):
    lane_loop.start_window(tmp_path, hours=1, invocations=1, now=1000.0)
    with pytest.raises(ValueError):
        lane_loop.stop(tmp_path, "")
