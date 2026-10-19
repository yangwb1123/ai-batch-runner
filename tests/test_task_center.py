"""Global task facts: identity, telemetry coverage, evidence and honest gaps."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import task_center, task_sources


@pytest.fixture
def source(tmp_path, monkeypatch):
    home = tmp_path / "private-runtime"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    data = home / "projects" / "demo"
    data.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    item = task_sources.source("demo", str(data), str(workspace))
    task_sources.register_source(item)
    return item


def write_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def agent_row(**updates):
    return {"run_id": "run-a", "task_id": "task-a", "stage": "implement", "attempt": 1,
            "phase": "execution", "status": "succeeded", "ts": "2026-10-10T00:00:10Z",
            "elapsed_ms": 10000, "task_brief": "fix recovery", "agent": "pi",
            "provider": "provider-a", "model": "model-a", **updates}


def ledger(source, rows):
    write_rows(Path(source["data_dir"]) / "agents" / "ledger.jsonl", rows)


def only_task(source):
    view = task_center.snapshot(now=1791590500)
    assert len(view["sources"]) == 1  # explicit alias replaces the default directory
    assert len(view["tasks"]) == 1
    return view["tasks"][0]


def test_retries_count_usage_once_and_validation_does_not_duplicate_cost(source):
    usage = {"inputTokens": 10, "outputTokens": 5, "cacheRead": 8,
             "totalTokens": 15, "cost_total": 0.2}
    rows = [agent_row(status="failed", usage=usage),
            agent_row(phase="validation", status="failed", usage=usage),
            agent_row(attempt=2, ts="2026-10-10T00:00:30Z", usage=usage),
            agent_row(attempt=2, phase="validation", ts="2026-10-10T00:00:31Z", usage=usage)]
    ledger(source, rows)
    task = only_task(source)
    assert task["usage"]["calls"] == 2
    assert task["usage"]["total_tokens"] == 30  # cache is not added again
    assert task["usage"]["cost_total"] == pytest.approx(0.4)
    assert task["usage"]["token_coverage"] == "complete"
    assert task["analysis"]["failure_observations"] == 1
    assert task["efficiency"]["accepted_goals"] is None
    assert task["efficiency"]["tokens_per_accepted_goal"] is None


def test_same_title_different_run_or_task_does_not_collapse(source):
    ledger(source, [agent_row(), agent_row(run_id="run-b"), agent_row(task_id="task-b")])
    tasks = task_center.snapshot()["tasks"]
    assert len(tasks) == 3
    assert len({task["task_id"] for task in tasks}) == 3


def test_agent_and_metering_feed_are_one_invocation(source):
    usage = {"totalTokens": 123, "cost_total": 0.5}
    ledger(source, [agent_row(usage=usage)])
    write_rows(source["events_file"], [{"event": "task_finish", "run_id": "run-a",
               "task_id": "task-a", "stage_id": "implement", "attempt": 1,
               "phase": "execution", "ts": "2026-10-10T00:00:10Z", "usage": usage}])
    task = only_task(source)
    assert task["usage"]["calls"] == 1
    assert task["usage"]["total_tokens"] == 123
    assert task["llm"][0]["model"] == "model-a"
    assert len(task["observations"]) == 1


def test_missing_and_partial_usage_are_not_free(source):
    ledger(source, [agent_row(status="failed"),
                   agent_row(attempt=2, usage={"totalTokens": 50}, ts="2026-10-10T00:00:30Z")])
    task = only_task(source)
    assert task["usage"]["token_coverage"] == "partial"
    assert task["usage"]["total_tokens"] == 50
    assert task["usage"]["cost_total"] is None
    assert task["usage"]["cost_coverage"] == "unavailable"
    assert task["time"]["remaining_seconds"] is None
    assert task["flow"]["next_status"] == "untracked"


@pytest.mark.parametrize("invalid", [True, -1, float("nan"), float("inf"), 1.5, "123"])
def test_invalid_token_values_do_not_become_observed_usage(source, invalid):
    ledger(source, [agent_row(usage={"totalTokens": invalid})])
    task = only_task(source)
    assert task["usage"]["total_tokens"] is None
    assert task["usage"]["token_coverage"] == "unavailable"
    json.dumps(task, allow_nan=False)


def test_missing_total_is_not_inferred_from_ambiguous_cache_contract(source):
    ledger(source, [agent_row(usage={"input": 10, "output": 5, "cacheRead": 8})])
    usage = only_task(source)["usage"]
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 5
    assert usage["cache_tokens"] == 8 and usage["total_tokens"] is None
    assert usage["token_coverage"] == "unavailable"
    assert usage["calls_basis"] == "completed_execution_observations"
    assert usage["llm_requests"] is None


def test_reported_zero_is_distinct_from_missing(source):
    ledger(source, [agent_row(usage={"totalTokens": 0, "cost_total": 0})])
    task = only_task(source)
    assert task["usage"]["total_tokens"] == 0
    assert task["usage"]["cost_total"] == 0
    assert task["usage"]["token_coverage"] == "complete"


def test_artifact_presence_is_not_acceptance_and_paths_stay_scoped(source, tmp_path):
    workspace = Path(source["workspace"])
    (workspace / "result.md").write_text("real output", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    ledger(source, [agent_row(outputs=["result.md", "missing.md", str(outside), "link.txt"])])
    task = only_task(source)
    assert [row["availability"] for row in task["artifacts"]] == [
        "present", "missing", "outside_registered_roots", "unavailable"]
    assert task["delivery"]["present_files"] == 1
    assert task["delivery"]["acceptance"] == "untracked"
    assert all(row["validation"] == "not_reverified" for row in task["artifacts"])
    assert "real output" not in json.dumps(task)
    assert "private" not in json.dumps(task["artifacts"][2])


def test_secrets_are_redacted_before_display_and_no_control_enabled(source):
    ledger(source, [agent_row(task_brief="token=do-not-show fix recovery\x1b[31m",
                             reason="Authorization: Bearer abc123")])
    serialized = json.dumps(only_task(source))
    assert "do-not-show" not in serialized and "abc123" not in serialized
    assert "\\u001b" not in serialized
    assert only_task(source)["capabilities"] == {
        "control": False, "cleanup": False, "feedback_upload": False}


def test_campaign_identity_keeps_history_but_does_not_invent_child_cost(source):
    rows = [{"campaign": "self", "module": "pbatch", "direction_id": "fix-1",
             "direction": "fix process cleanup", "status": "RUNNING", "run_id": "run-a",
             "round_id": "r1", "updated_at": "2026-10-10T00:00:00Z"},
            {"campaign": "self", "module": "pbatch", "direction_id": "fix-1",
             "direction": "fix process cleanup", "status": "PIPELINE_FAILED", "run_id": "run-a",
             "round_id": "r1", "updated_at": "2026-10-10T00:00:20Z", "elapsed": 20,
             "reason": "isolated pipeline exited 1"}]
    write_rows(source["campaign_state"], rows)
    task = only_task(source)
    assert task["kind"] == "direction" and task["status"] == "PIPELINE_FAILED"
    assert task["time"]["elapsed_wall_seconds"] == 20
    assert task["usage"]["calls"] is None
    assert task["usage"]["cost_total"] is None
    assert task["analysis"]["latest_reason"] == "isolated pipeline exited 1"


def test_live_start_event_reports_elapsed_not_progress_or_verified_liveness(source):
    write_rows(source["events_file"], [{"event": "task_start", "run_id": "live",
               "task_id": "task-a", "ts": 100, "stage_id": "review", "attempt": 1}])
    task = task_center.snapshot(now=150)["tasks"][0]
    assert task["status"] == "running"
    assert task["time"]["elapsed_wall_seconds"] == 50
    assert task["time"]["liveness"] == "not_verified"
    assert task["usage"]["total_tokens"] is None


def test_invalid_and_partial_records_are_visible_and_make_coverage_partial(source):
    ledger(source, [agent_row(usage={"totalTokens": 3})])
    path = Path(source["data_dir"]) / "agents" / "ledger.jsonl"
    with path.open("a") as handle:
        handle.write('invalid\n{"status":')
    view = task_center.snapshot()
    info = next(row for row in view["sources"][0]["coverage"] if row["feed"] == "agents")
    assert info["malformed"] == 1 and info["incomplete"] is True
    assert view["tasks"][0]["usage"]["token_coverage"] == "partial"


def test_reading_missing_home_does_not_create_anything(tmp_path, monkeypatch):
    home = tmp_path / "absent"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    assert task_center.snapshot()["tasks"] == []
    assert not home.exists()


def test_cli_registration_list_and_inspection(source, capsys):
    from pbatch.ops import ops_main
    ledger(source, [agent_row()])
    assert ops_main(["sources", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["name"] == "demo"
    assert ops_main(["tasks", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["tasks"][0]["task_id"]
    assert ops_main(["tasks", "--id", task_id]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == task_id


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "201"],
                                  ["--interval", "nan"], ["--interval", "inf"]])
def test_bad_cli_limits_fail_before_snapshot(source, args, capsys):
    from pbatch.ops import ops_main
    assert ops_main(["tasks", *args]) == 2
    assert "ops tasks:" in capsys.readouterr().out


def test_generated_task_carries_a_bounded_execution_mapping(source):
    ledger(source, [agent_row(attempt=1, spawn_observed=True),
                    agent_row(attempt=2, status="failed",
                              ts="2026-10-10T00:00:30Z")])
    task = only_task(source)
    mapping = task["execution_mapping"]
    assert mapping["method"] == "verified-execution-mapping-v1"
    assert mapping["execution_observations"] == 2
    assert mapping["verified_spawn_and_outcome"] == 1
    assert mapping["outcome_without_spawn_observation"] == 1
    assert mapping["unknown_not_zero"] is False
