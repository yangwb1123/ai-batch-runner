"""Routing evaluation reports only bounded ledger observations."""

from __future__ import annotations

import argparse
import json

import pytest

from pbatch import agent_ledger, config, execution_trace, metering, runner
from pbatch.models import Task, TaskResult
from pbatch.routing_eval import routing_eval_main, routing_report


def _record(root, *, task_id, status, model="fast", provider="provider",
            role="worker",
            attempt=1, elapsed=1000, usage=None, phase="execution",
            fingerprint="", route_source="", route_tier="",
            route_requested_tier="", route_downgraded=None,
            route_profile_mode=None, ts="1"):
    return agent_ledger.record(
        root, agent="pi", provider=provider, run_id="run-1",
        session_id="session", session_name="review", status=status,
        task_brief="private prompt excerpt", outputs=["out.md"],
        elapsed_ms=elapsed, exit_code=0 if status == "succeeded" else 1,
        attempt=attempt, model=model, stage="review", role=role,
        task_id=task_id, phase=phase, task_fingerprint=fingerprint,
        route_source=route_source, route_tier=route_tier,
        route_requested_tier=route_requested_tier,
        route_downgraded=route_downgraded,
        route_profile_mode=route_profile_mode, usage=usage, ts=ts)


def test_routing_report_measures_success_latency_rework_and_partial_cost(tmp_path):
    _record(tmp_path, task_id="task-a", status="failed", attempt=1,
            elapsed=1200, usage=None, ts="1")
    _record(tmp_path, task_id="task-a", status="succeeded", attempt=2,
            elapsed=800, usage={"cost_total": 0.01, "totalTokens": 80}, ts="2")
    _record(tmp_path, task_id="task-a", status="rejected", phase="validation", ts="3")
    _record(tmp_path, task_id="task-a", status="succeeded",
            phase="validation", ts="3.5")
    _record(tmp_path, task_id="task-b", status="succeeded", elapsed=400,
            usage={"cost_total": 0.03, "totalTokens": 120}, ts="4")
    _record(tmp_path, task_id="task-b", status="succeeded",
            phase="validation", ts="4.5")
    _record(tmp_path, task_id="task-c", status="succeeded", model="strong",
            role="security", elapsed=200, usage=None, ts="5")

    report = routing_report(data_dir=tmp_path, limit=100)
    fast = next(item for item in report["routes"] if item["model"] == "fast")
    strong = next(item for item in report["routes"] if item["model"] == "strong")

    assert fast["tasks"] == 2
    assert fast["calls"] == 3
    assert fast["success_rate"] == 1.0
    assert fast["reworked_tasks"] == fast["validator_rejected_tasks"] == 1
    assert fast["retried_tasks"] == 1
    assert fast["retry_rate"] == fast["rework_rate"] == 0.5
    assert fast["mean_latency_ms"] == 800
    assert fast["validator_pass_rate"] == 1.0
    assert fast["validator_attempt_pass_rate"] == 0.6667
    assert fast["cost_coverage"] == 0.6667
    assert fast["cost_total"] is None
    assert fast["cost_observed_sum"] == 0.04
    assert fast["tokens_total"] is None
    assert strong["success_rate"] == 1.0
    assert strong["cost_total"] is None
    assert report["validation_reject_tasks"] == 1


def test_paired_comparison_uses_prompt_hash_without_persisting_prompt(tmp_path):
    _record(tmp_path, task_id="baseline-task", status="succeeded",
            model="baseline", elapsed=300,
            usage={"cost_total": 0.1}, fingerprint="same-task", ts="1")
    _record(tmp_path, task_id="baseline-task", status="succeeded",
            model="baseline", phase="validation", fingerprint="same-task", ts="2")
    _record(tmp_path, task_id="candidate-task", status="failed",
            model="candidate", elapsed=900,
            usage={"cost_total": 0.2}, fingerprint="same-task", ts="3")
    _record(tmp_path, task_id="candidate-task", status="rejected",
            model="candidate", phase="validation", fingerprint="same-task", ts="4")

    report = routing_report(data_dir=tmp_path, limit=100,
                            baseline_model="baseline",
                            candidate_model="candidate")
    comparison = report["comparison"]

    assert comparison["paired_fingerprints"] == 1
    assert comparison["metrics"]["success_rate"][
        "candidate_minus_baseline"] == -1.0
    assert comparison["metrics"]["validator_pass_rate"][
        "candidate_minus_baseline"] == -1.0
    assert comparison["metrics"]["mean_latency_ms"][
        "candidate_minus_baseline"] == 600.0
    assert comparison["metrics"]["cost_per_task"][
        "candidate_minus_baseline"] == 0.1
    assert "private prompt excerpt" not in str(report)


def test_paired_comparison_respects_route_filters(tmp_path):
    _record(tmp_path, task_id="baseline", status="succeeded", model="base",
            fingerprint="same", ts="1")
    _record(tmp_path, task_id="candidate", status="succeeded", model="new",
            fingerprint="same", ts="2")

    included = routing_report(
        data_dir=tmp_path, limit=100, role="worker",
        baseline_model="base", candidate_model="new",
        baseline_provider="provider", candidate_provider="provider")
    excluded = routing_report(
        data_dir=tmp_path, limit=100, baseline_model="base",
        candidate_model="new", baseline_provider="other",
        candidate_provider="other")

    assert included["comparison"]["paired_fingerprints"] == 1
    assert excluded["comparison"]["paired_fingerprints"] == 0


def test_routing_report_since_filter_and_timezone_validation(tmp_path):
    _record(tmp_path, task_id="old", status="succeeded", ts="2026-08-20T00:00:00Z")
    _record(tmp_path, task_id="new", status="succeeded", ts="2026-08-22T00:00:00Z")

    report = routing_report(
        data_dir=tmp_path, limit=100, since_ts="2026-08-21T00:00:00+00:00")

    assert report["records_read"] == 1
    assert report["filters"]["since"] == "2026-08-21T00:00:00+00:00"
    from pbatch.routing_eval_cli import _since_value
    assert _since_value("2026-08-21T00:00:00Z").endswith("+00:00")
    with pytest.raises(argparse.ArgumentTypeError):
        _since_value("2026-08-21T00:00:00")


def test_route_filter_is_applied_before_the_record_limit(tmp_path):
    ledger = agent_ledger.ledger_path(tmp_path)
    ledger.parent.mkdir(parents=True)
    rows = [{"ts": "1", "agent": "pi", "run_id": "r", "task_id": "old",
             "phase": "execution", "status": "succeeded", "model": "target",
             "provider": "p"}]
    rows.extend({"ts": str(index + 2), "agent": "pi", "run_id": "r",
                 "task_id": f"noise-{index}", "phase": "execution",
                 "status": "succeeded", "model": "noise", "provider": "p"}
                for index in range(agent_ledger.QUERY_MAX_RECORDS + 2))
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                      encoding="utf-8")

    report = routing_report(data_dir=tmp_path, limit=1, model="target")

    assert report["records_read"] == 1
    assert report["truncated"] is False
    assert [route["model"] for route in report["routes"]] == ["target"]


def test_paired_filter_keeps_both_routes_before_record_limit(tmp_path):
    ledger = agent_ledger.ledger_path(tmp_path)
    ledger.parent.mkdir(parents=True)
    rows = [
        {"ts": "1", "agent": "pi", "run_id": "r", "task_id": "base",
         "phase": "execution", "status": "succeeded", "model": "baseline",
         "provider": "p", "task_fingerprint": "same", "elapsed_ms": 100},
        {"ts": "2", "agent": "pi", "run_id": "r", "task_id": "candidate",
         "phase": "execution", "status": "succeeded", "model": "candidate",
         "provider": "p", "task_fingerprint": "same", "elapsed_ms": 200},
    ]
    rows.extend({"ts": str(index + 3), "agent": "pi", "run_id": "r",
                 "task_id": f"noise-{index}", "phase": "execution",
                 "status": "succeeded", "model": "noise", "provider": "p"}
                for index in range(agent_ledger.QUERY_MAX_RECORDS + 2))
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                      encoding="utf-8")

    report = routing_report(
        data_dir=tmp_path, limit=2, baseline_model="baseline",
        candidate_model="candidate")

    assert report["records_read"] == 2
    assert report["truncated"] is False
    assert report["comparison"]["paired_fingerprints"] == 1


def test_paired_comparison_disambiguates_same_model_across_providers(
        tmp_path, monkeypatch, capsys):
    _record(tmp_path, task_id="baseline", status="succeeded", model="shared",
            provider="provider-a", fingerprint="same", ts="1")
    _record(tmp_path, task_id="candidate", status="succeeded", model="shared",
            provider="provider-b", fingerprint="same", ts="2")

    report = routing_report(
        data_dir=tmp_path, limit=100, baseline_model="shared",
        candidate_model="shared", baseline_provider="provider-a",
        candidate_provider="provider-b")

    assert report["comparison"]["paired_fingerprints"] == 1
    assert report["comparison"]["baseline_provider"] == "provider-a"
    assert report["comparison"]["candidate_provider"] == "provider-b"

    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    assert routing_eval_main([
        "--baseline-model", "shared", "--baseline-provider", "provider-a",
        "--candidate-model", "shared", "--candidate-provider", "provider-b",
        "--json"]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["comparison"]["paired_fingerprints"] == 1


def test_routing_report_marks_legacy_records_unattributed_and_truncation(tmp_path):
    for index in range(4):
        agent_ledger.record(
            tmp_path, agent="pi", status="succeeded", task_brief="legacy",
            ts=f"{index}")

    report = routing_report(data_dir=tmp_path, limit=2)

    assert report["truncated"] is True
    assert report["unattributed_execution_records"] == 2
    assert report["routes"] == []


def test_routing_eval_cli_json_and_model_filter(tmp_path, monkeypatch, capsys):
    _record(tmp_path, task_id="task-a", status="succeeded", model="fast",
            route_source="difficulty", route_tier="medium",
            route_requested_tier="high", route_downgraded=True,
            route_profile_mode=2)
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    assert routing_eval_main([
        "--json", "--model", "fast", "--source", "difficulty",
        "--tier", "medium", "--requested-tier", "high"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["routes"]) == 1
    assert result["routes"][0]["model"] == "fast"
    assert result["routes"][0]["route_source"] == "difficulty"
    assert result["routes"][0]["route_tier"] == "medium"
    assert result["routes"][0]["route_requested_tier"] == "high"
    assert result["routes"][0]["route_downgraded"] is True


def test_execution_trace_attaches_usage_and_records_validation_pass(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_LEDGER_ENABLED", True)
    monkeypatch.setattr(metering, "record_event",
                        lambda *_args, **_kwargs: {"cost_total": 0.25})
    recorded = []
    monkeypatch.setattr(
        agent_ledger, "record_result",
        lambda result, task, workdir, *, phase: recorded.append(
            (result, task, workdir, phase)) or True)
    task = Task("task", cwd=str(tmp_path))
    result = TaskResult(task, True)

    execution_trace.finish_task_result(
        result, task, "", "", str(tmp_path), None, "task-id", 1)
    result.validation_ok = True
    execution_trace.record_validation_event(result, task)

    assert result.usage == {"cost_total": 0.25}
    assert [item[3] for item in recorded] == ["execution", "validation"]


def test_runner_snapshots_usage_for_ledger_without_events_file(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_LEDGER_ENABLED", True)
    monkeypatch.setattr(metering, "EVENTS_FILE", "")
    monkeypatch.setattr(runner, "task_key", lambda _task: "key")
    monkeypatch.setattr(runner, "write_marker", lambda *_args: None)
    monkeypatch.setattr(metering, "session_usage",
                        lambda *_args: {"totalTokens": 4})
    task = Task("task", cwd=str(tmp_path))

    result = runner._prepare_task_run(
        task, ["pi", "-p", "task"], 1, 1, False, str(tmp_path), "session",
        "", "task-id")

    assert result[3] == {"totalTokens": 4}


def test_metering_returns_provider_usage_for_ledger_without_events_file(
        monkeypatch):
    monkeypatch.setattr(metering, "EVENTS_FILE", "")
    monkeypatch.setattr(config, "AGENT_LEDGER_ENABLED", True)
    monkeypatch.setattr(
        metering, "_session_usage",
        lambda *_args: {"totalTokens": 15, "cost_total": 0.3})

    usage = metering.record_event(
        "task_finish", "task", cwd="/workspace", session_name="session",
        usage_before={"totalTokens": 10, "cost_total": 0.1})

    assert usage == {"totalTokens": 5, "cost_total": 0.19999999999999998}
