import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch.campaign_metrics import (build_efficiency_metrics,
                                     execution_attempts)
from pbatch.campaign_strategy import (UsageSummary, plan_modules,
                                      read_module_signals, read_usage)
from pbatch.campaign_watchdog import finish_lease, prepare_lease


def test_focus_and_batch_are_deterministic():
    plan = plan_modules(["domains/auth", "protocols/http", "ui"],
                        focus="http protocol", batch_size=1)
    assert plan.selected == ("protocols/http",)
    assert plan.skipped == ("domains/auth", "ui")


def test_coverage_priority_moves_past_recently_analyzed_module():
    plan = plan_modules(["a", "b"], batch_size=1, priority={"a": -100})
    assert plan.selected == ("b",)


def test_provider_invocation_allowance_bounds_the_batch():
    plan = plan_modules(["a", "b", "c"], batch_size=4, max_calls=2)
    assert plan.selected == ("a", "b")
    assert plan.reason == "provider-budget bounded"


def test_provider_invocation_allowance_fails_closed_when_empty():
    plan = plan_modules(["a"], max_calls=0)
    assert plan.blocked is True


def test_token_budget_stops_when_current_run_is_exhausted():
    usage = UsageSummary(total_tokens=10_000, calls=2)
    plan = plan_modules(["a", "b", "c"], token_budget=11_000,
                        usage=usage, token_estimate=8_000)
    assert plan.selected == ()
    assert plan.blocked is True


def test_unmatched_focus_fails_closed():
    plan = plan_modules(["domains/auth"], focus="payments")
    assert plan.blocked is True
    assert plan.selected == ()


def test_focus_uses_bounded_module_content(tmp_path: Path):
    module = tmp_path / "src" / "billing"
    module.mkdir(parents=True)
    (module / "service.py").write_text("class PaymentLedger: pass", encoding="utf-8")
    signals = read_module_signals(tmp_path, ["src/billing"])
    plan = plan_modules(["src/billing", "src/auth"], focus="ledger",
                        batch_size=1, signals=signals)
    assert plan.selected == ("src/billing",)


def test_read_usage_is_scoped_to_analysis_run(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"run_id":"r","stage_id":"campaign-analysis","event":"task_finish","usage":'
        '{"input_tokens":100,"output_tokens":50}}\n'
        '{"run_id":"other","stage_id":"campaign-analysis","event":"task_finish","usage":'
        '{"total_tokens":9000}}\n'
        '{"run_id":"r","stage_id":"implementation","event":"task_finish","usage":'
        '{"total_tokens":9000}}\n', encoding="utf-8")
    assert read_usage(str(events), run_id="r").total_tokens == 150
    assert read_usage(str(events), run_id="r").calls == 1


def test_pipeline_lease_acquisition_is_atomic(tmp_path: Path):
    lease_path = tmp_path / "lease.json"
    payload = prepare_lease(
        lease_path, pid=os.getpid(), direction_id="direction",
        worktree=tmp_path, pipeline=tmp_path / "pipeline.yaml",
        logfile=tmp_path / "pipeline.log", timeout=60,
        no_progress_timeout=10)
    with pytest.raises(RuntimeError, match="already held"):
        prepare_lease(
            lease_path, pid=os.getpid(), direction_id="direction",
            worktree=tmp_path, pipeline=tmp_path / "pipeline.yaml",
            logfile=tmp_path / "pipeline.log", timeout=60,
            no_progress_timeout=10)
    finish_lease(lease_path, payload, "finished")
    assert not lease_path.exists()
    lease_path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        prepare_lease(
            lease_path, pid=os.getpid(), direction_id="direction",
            worktree=tmp_path, pipeline=tmp_path / "pipeline.yaml",
            logfile=tmp_path / "pipeline.log", timeout=60,
            no_progress_timeout=10)


def test_efficiency_metrics_report_bounded_round_observations(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"run_id":"r","event":"task_finish","attempt":1}\n'
        '{"run_id":"r","event":"task_fail","attempt":2}\n'
        '{"run_id":"other","event":"task_fail","attempt":4}\n',
        encoding="utf-8")
    outcomes = [
        SimpleNamespace(status="PASSED", elapsed=4, no_progress_seconds=0),
        SimpleNamespace(status="GATE_REJECTED", elapsed=2, no_progress_seconds=1.5),
    ]
    attempts, retries = execution_attempts(str(events), "r")
    metrics = build_efficiency_metrics(
        outcomes, 4, 2, UsageSummary(total_tokens=100, calls=1),
        attempts=attempts, retries=retries)
    assert metrics["analysis_tokens_per_successful_direction"] == 100
    assert metrics["gate_rejection_ratio"] == 0.5
    assert metrics["retry_ratio"] == 0.5
    assert metrics["concurrency_slot_utilization"] == 0.75
    assert metrics["no_progress_seconds"] == 1.5
