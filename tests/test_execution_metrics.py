"""Requested configuration and measured execution intervals, without model calls."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pbatch import agent_ledger, cli_batch, config, runner, task_center, task_sources
from pbatch.models import Pipeline, Task, TaskResult


def _result(task=None, **fields):
    return TaskResult(task or Task("safe task", model="model-a"), True,
                      run_id="run-a", task_id="task-a", attempt=1,
                      returncode=0, elapsed=2.5, **fields)


@pytest.mark.parametrize(("override", "requested", "expected"), [
    ("max", "low", "max"), ("", "high", "high"),
    (None, "minimal", "minimal"), ("off", "max", "off"),
    ("", "", None), ("", None, None), ("unknown", "high", None),
    (True, "high", None), (" MAX ", "low", None),
])
def test_result_records_the_same_requested_thinking_as_task_command(
        tmp_path, monkeypatch, override, requested, expected):
    monkeypatch.setattr(config, "AGENT_THINKING_OVERRIDE", override)
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    build = Mock(return_value=["not-executed"])
    monkeypatch.setattr(config, "build_agent_argv", build)
    task = Task("safe task", thinking=requested)
    task.to_cmd()
    assert build.call_args.kwargs["thinking"] == (override or requested)
    assert agent_ledger.record_result(_result(task), task, str(tmp_path))
    row = agent_ledger.query(tmp_path)[0]
    assert row.get("thinking") == expected
    assert row.get("thinking_origin") == (
        "requested_configuration" if expected else None)
    build.assert_called_once()  # recording must not expand or inspect argv


@pytest.mark.parametrize("level", ["off", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_direct_record_accepts_bounded_requested_levels(tmp_path, level):
    assert agent_ledger.record(tmp_path, agent="pi", status="succeeded",
                               task_brief="safe", thinking=level)
    row = agent_ledger.query(tmp_path)[0]
    assert row["thinking"] == level
    assert row["thinking_origin"] == "requested_configuration"
    assert len(row["thinking"]) <= 32
    assert agent_ledger.ledger_path(tmp_path).stat().st_size <= agent_ledger.LEDGER_MAX_LINE_BYTES


@pytest.mark.parametrize("invalid", [
    None, "", True, 1, {}, ["max"], "unknown", "MAX", "max\n", "high\x1b[31m",
    "max" * 10000, "token=do-not-store", "Authorization: Bearer private-value",
    "@/private/prompt.md", "--thinking max --token private-value",
], ids=["null", "empty", "bool", "number", "mapping", "list", "unknown", "case",
        "newline", "control", "oversized", "token", "bearer", "prompt-ref", "argv"])
def test_invalid_thinking_is_not_coerced_truncated_or_logged(tmp_path, invalid, caplog):
    assert agent_ledger.record(tmp_path, agent="pi", status="succeeded",
                               task_brief="safe", thinking=invalid)
    row = agent_ledger.query(tmp_path)[0]
    assert row.get("thinking") is None and row.get("thinking_origin") is None
    encoded = agent_ledger.ledger_path(tmp_path).read_text(encoding="utf-8")
    assert "private-value" not in encoded + caplog.text
    assert "do-not-store" not in encoded + caplog.text
    assert "prompt.md" not in encoded and "\\u001b" not in encoded
    assert len(encoded.encode("utf-8")) <= agent_ledger.LEDGER_MAX_LINE_BYTES


def test_legacy_direct_record_and_query_do_not_backfill_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_THINKING_OVERRIDE", "max")
    assert agent_ledger.record(tmp_path, agent="pi", status="succeeded", task_brief="old")
    before = agent_ledger.ledger_path(tmp_path).read_bytes()
    row = agent_ledger.query(tmp_path)[0]
    assert row.get("thinking") is None
    assert row.get("thinking_origin") is None
    assert agent_ledger.query_snapshot(tmp_path).records == (row,)
    assert agent_ledger.ledger_path(tmp_path).read_bytes() == before


def test_thinking_does_not_change_observational_write_failure(tmp_path, monkeypatch):
    def refused(*_args, **_kwargs):
        raise OSError("read-only fixture")

    warning = Mock()
    monkeypatch.setattr(agent_ledger.os, "open", refused)
    monkeypatch.setattr(agent_ledger.log, "warning", warning)
    assert not agent_ledger.record(tmp_path, agent="pi", status="failed",
                                   task_brief="safe", thinking="max")
    warning.assert_called_once()


def test_result_thinking_keeps_existing_routing_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_THINKING_OVERRIDE", "max")
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    task = Task("safe task", agent="pi", thinking="low", timeout=30,
                memory={"stage": "review", "role": "qa"})
    fields = (str(tmp_path.resolve()), "safe task", "review", "qa", "", "", "pi",
              "low", "max", "", "", "False", "30")
    expected = hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()[:32]
    assert agent_ledger.record_result(_result(task), task, str(tmp_path))
    assert agent_ledger.query(tmp_path)[0]["task_fingerprint"] == expected


@pytest.mark.parametrize("level", ["max", "", "unknown", "token=private-value"])
def test_real_ledger_projects_thinking_to_existing_task_center(
        tmp_path, monkeypatch, level):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    root = tmp_path / "runtime" / "fixture"
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": root)
    monkeypatch.setattr(config, "AGENT_THINKING_OVERRIDE", level)
    task = Task("safe task", agent="pi", provider="provider-a", model="model-a")
    result = _result(task)
    assert agent_ledger.record_result(result, task, str(tmp_path))
    assert agent_ledger.record_result(result, task, str(tmp_path), phase="validation")
    source = task_sources.source("fixture", str(root), str(tmp_path))
    monkeypatch.setattr(task_center, "discover_sources", lambda: ([source], []))
    monkeypatch.setattr(task_center, "activity_snapshot",
                        lambda: {"processes": [], "diagnostics": []})
    view = task_center.snapshot()
    assert len(view["tasks"]) == 1
    projected = view["tasks"][0]
    assert projected["llm"] == [{"agent": "pi", "provider": "provider-a",
        "model": "model-a", "thinking": "max" if level == "max" else None,
        "origin": "recorded_configuration"}]
    assert projected["usage"]["cost_total"] is None
    assert projected["delivery"]["acceptance"] == "untracked"
    assert "private-value" not in json.dumps(view)


def _durations():
    return [TaskResult(Task("first"), True, elapsed=6),
            TaskResult(Task("second"), False, elapsed=4, validation_exit=9)]


def test_legacy_summary_does_not_invent_cpu_or_batch_wall(capsys):
    runner.print_summary(_durations())
    report = capsys.readouterr().out
    assert "cumulative task elapsed wall: 10.0s" in report
    assert "execution wall: unknown (not measured)" in report
    assert "CPU time: unknown (not measured)" in report
    assert "total:     2" in report and "failed:    1" in report
    assert "[val exit=9]" in report


def test_summary_redacts_prompt_secrets_and_does_not_authorize_delivery(capsys):
    task = Task('token=synthetic-private-value fixture')
    runner.print_summary([_result(task)])
    report = capsys.readouterr().out
    assert 'synthetic-private-value' not in report
    assert '[REDACTED]' in report
    assert '仓库门禁、合并与目标验收' in report
    assert '可 --git-commit' not in report


@pytest.mark.parametrize("wall", [0, 7, 13])
def test_summary_distinguishes_zero_serial_and_overlapping_measured_wall(capsys, wall):
    runner.print_summary(_durations(), wall_seconds=wall, wall_scope="batch")
    report = capsys.readouterr().out
    assert "cumulative task elapsed wall: 10.0s" in report
    assert f"batch execution wall: {wall:.1f}s" in report
    assert "CPU time: unknown (not measured)" in report


@pytest.mark.parametrize("invalid", [None, True, -1, float("nan"), float("inf"), "10", 10 ** 1000],
                         ids=["null", "bool", "negative", "nan", "infinity", "string", "overflow"])
def test_summary_invalid_measured_wall_stays_unknown(capsys, invalid):
    runner.print_summary([], wall_seconds=invalid)
    report = capsys.readouterr().out
    assert "execution wall: unknown (not measured)" in report
    assert "CPU time: unknown (not measured)" in report


def _batch_args(mode="serial"):
    return SimpleNamespace(mode=mode, retries=1, retry_delay=3, retry_backoff=2,
                           min_interval=0, session_mode="new", workers=2)


@pytest.mark.parametrize("mode", ["serial", "parallel"])
def test_cli_measures_dispatch_interval_and_preserves_one_argument_hook(
        monkeypatch, capsys, mode):
    from pbatch import cli
    now = [100.0]
    events = []
    results = _durations()

    def execute(*_args, **options):
        assert options["retries"] == 1 and options["validate_cmd"] == "gate"
        for name, duration in (("attempt", 6), ("validation", 4), ("retry_wait", 3)):
            events.append(name)
            now[0] += duration
        return results

    def legacy_summary(items):
        assert items is results and events == ["attempt", "validation", "retry_wait"]
        runner.print_summary(items)

    monkeypatch.setattr(cli_batch, "monotonic", lambda: now[0], raising=False)
    monkeypatch.setattr(cli, "run_" + mode, execute)
    monkeypatch.setattr(cli, "print_summary", legacy_summary)
    actual, failed = cli_batch._execute_batch(
        [Task("work")], _batch_args(mode), "session", "gate")
    assert actual is results and failed is True
    report = capsys.readouterr().out
    assert "batch execution wall: 13.0s" in report
    assert "cumulative task elapsed wall: 10.0s" in report
    runner.print_summary(results)
    assert "execution wall: unknown (not measured)" in capsys.readouterr().out


def _pipeline_args(dry_run=False):
    return SimpleNamespace(dry_run=dry_run, model="", session_mode="new", decision_log="",
                           archive_dir="", reuse_legacy=False, reuse_fingerprint=False,
                           approve_file="", prompt="", retries=1, retry_delay=3, retry_backoff=2)


@pytest.mark.parametrize("failed_stages", [[], ["gate"]])
def test_pipeline_interval_and_legacy_summary_hook(monkeypatch, capsys, failed_stages):
    ticks = iter((40.0, 58.0))
    events = []
    results = _durations()

    def execute(*_args, **options):
        assert options["retries"] == 1 and options["validate_cmd"] == "gate"
        events.extend(["stages", "validation", "retry_wait"])
        return results, failed_stages

    def legacy_summary(items):
        assert items is results and events == ["stages", "validation", "retry_wait"]
        runner.print_summary(items)

    monkeypatch.setattr(cli_batch, "monotonic", lambda: next(ticks), raising=False)
    monkeypatch.setattr(cli_batch, "run_pipeline", execute)
    monkeypatch.setattr(cli_batch, "print_summary", legacy_summary)
    failed, keys = cli_batch._run_pipeline_round(
        _pipeline_args(), Pipeline(), "session", "gate", 0, False)
    assert failed is bool(failed_stages)
    assert ("stage:gate" in keys) is bool(failed_stages)
    report = capsys.readouterr().out
    assert "pipeline execution wall: 18.0s" in report
    runner.print_summary(results)
    assert "execution wall: unknown (not measured)" in capsys.readouterr().out


def test_dry_run_has_no_measured_execution_or_summary(monkeypatch):
    execute = Mock(return_value=([], []))
    summary = Mock()
    clock = Mock(side_effect=AssertionError("dry-run is not execution"))
    monkeypatch.setattr(cli_batch, "run_pipeline", execute)
    monkeypatch.setattr(cli_batch, "print_summary", summary)
    monkeypatch.setattr(cli_batch, "monotonic", clock, raising=False)
    assert cli_batch._run_pipeline_round(
        _pipeline_args(True), Pipeline(), "", "", 0, False) == (False, set())
    assert execute.call_args.kwargs["dry_run"] is True
    summary.assert_not_called()
    clock.assert_not_called()


def test_cli_real_retry_loop_includes_task_validation_and_backoff(
        tmp_path, monkeypatch, capsys):
    from pbatch import cli
    now, attempts, validations = [10.0], [], []

    def run_task(task, **options):
        attempts.append(options["attempt"])
        now[0] += 2
        return TaskResult(task, True, stdout="fixture", elapsed=2, returncode=0)

    def validate(_task, result, command):
        assert command == "gate"
        now[0] += 3
        validations.append(1)
        result.validation_ok = len(validations) == 2
        result.validation_exit = 0 if result.validation_ok else 1
        return result.validation_ok

    def scheduled_sleep(seconds):
        assert seconds == 4
        now[0] += seconds

    monkeypatch.setattr(cli_batch, "monotonic", lambda: now[0])
    monkeypatch.setattr(cli, "run_serial", runner.run_serial)
    monkeypatch.setattr(cli, "print_summary", runner.print_summary)
    monkeypatch.setattr(runner, "run_task", run_task)
    monkeypatch.setattr(runner, "_save_validated", validate)
    monkeypatch.setattr(runner, "_retry_wait", lambda *_args: 4)
    monkeypatch.setattr(runner, "_scheduled_sleep", scheduled_sleep)
    monkeypatch.setattr(runner, "capture_worktree_state", lambda *_args: None)
    monkeypatch.setattr(runner, "preserve_failed_worktree", lambda *_args: None)
    monkeypatch.setattr(runner, "record_task", Mock())
    monkeypatch.setattr(runner, "record_validation_event", Mock())
    results, failed = cli_batch._execute_batch(
        [Task("fixture", cwd=str(tmp_path))], _batch_args(), "session", "gate")
    assert not failed and results[0].attempt == 2
    assert attempts == [1, 2] and len(validations) == 2
    report = capsys.readouterr().out
    assert "batch execution wall: 14.0s" in report
    assert "cumulative task elapsed wall: 2.0s" in report
    assert "CPU time: unknown (not measured)" in report


def test_summary_projection_preserves_public_reexports():
    import pbatch
    from pbatch import cli, pipeline, runner_summary
    assert runner.print_summary is runner_summary.print_summary
    assert cli.print_summary is runner.print_summary
    assert pipeline.print_summary is runner.print_summary
    assert pbatch.print_summary is runner.print_summary


def test_timing_context_is_nested_exception_safe_and_not_shared_across_threads(capsys):
    from concurrent.futures import ThreadPoolExecutor
    from pbatch.runner_summary import summary_wall
    with summary_wall(12, "batch"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(runner.print_summary, []).result(timeout=5)
        assert "execution wall: unknown (not measured)" in capsys.readouterr().out
        with pytest.raises(RuntimeError), summary_wall(3, "pipeline"):
            runner.print_summary([])
            raise RuntimeError("fixture hook failed")
        assert "pipeline execution wall: 3.0s" in capsys.readouterr().out
        runner.print_summary([])
        assert "batch execution wall: 12.0s" in capsys.readouterr().out
        runner.print_summary([], wall_seconds=0, wall_scope="pipeline")
        assert "pipeline execution wall: 0.0s" in capsys.readouterr().out
    runner.print_summary([])
    assert "execution wall: unknown (not measured)" in capsys.readouterr().out


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_cli_does_not_turn_invalid_or_zero_clock_intervals_into_task_estimates(
        monkeypatch, capsys, duration):
    from pbatch import cli
    ticks = iter((10.0, 10.0 + duration))
    monkeypatch.setattr(cli_batch, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli, "run_serial", lambda *_args, **_kwargs: _durations())
    monkeypatch.setattr(cli, "print_summary", runner.print_summary)
    cli_batch._execute_batch([Task("fixture")], _batch_args(), "", "")
    expected = "0.0s" if duration == 0 else "unknown (not measured)"
    assert f"batch execution wall: {expected}" in capsys.readouterr().out


@pytest.mark.parametrize("elapsed", [None, True, -1, float("nan"), float("inf")])
def test_invalid_task_elapsed_is_not_silently_added_as_zero(capsys, elapsed):
    results = [TaskResult(Task("fixture"), True, elapsed=elapsed)]
    runner.print_summary(results)
    report = capsys.readouterr().out
    assert "cumulative task elapsed wall: unknown (not measured)" in report
    assert "execution wall: unknown (not measured)" in report
    assert "CPU time: unknown (not measured)" in report
