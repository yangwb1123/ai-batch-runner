"""Runtime checkpoints survive window truncation without manufacturing acceptance."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from pbatch import config, metering, pipeline, task_center, task_sources
from pbatch import task_lifecycle as lifecycle
from pbatch.models import Pipeline, Stage, Task
from pbatch.task_center_io import read_rows
from pbatch.task_plan_view import plan_flow
from pbatch.task_state_io import append_bounded


@pytest.fixture
def source(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    data = home / "projects" / "demo"
    data.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(data))
    monkeypatch.setattr(config, "AGENT_LEDGER_ENABLED", True)
    item = task_sources.source("demo", str(data), str(project))
    task_sources.register_source(item)
    return item


def plan(source):
    return Pipeline(name="example", stages=[Stage(name="design", cwd=source["workspace"]),
        Stage(name="implement", cwd=source["workspace"], from_outputs="design"),
        Stage(name="gate", cwd=source["workspace"], from_outputs="implement", gate=True)])


def test_next_step_is_deterministic_plan_not_execution_permission(source):
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    try:
        assert plan_flow(facts.state)["next_status"] == "planned_ready"
        facts.stage_started("design")
        flow = plan_flow(facts.state)
        assert flow["next_stage"] == "implement" and flow["next_status"] == "planned_waiting"
        facts.stage_completed("design", True, False)
        flow = plan_flow(facts.state)
        assert flow["next_stage"] == "implement" and flow["next_status"] == "planned_ready"
        assert flow["grants_execution"] is False
        facts.stage_started("implement")
        facts.stage_completed("implement", False, False)
        facts.finish(True, ["implement"], False)
        assert plan_flow(facts.state)["next_status"] == "blocked_by_failed"
        assert plan_flow(facts.state)["next_stage"] is None
    finally:
        lifecycle.close_scope(facts, token, True, ["implement"], False)


def test_tail_without_start_keeps_complete_checkpoint_but_not_goal_acceptance(source, monkeypatch):
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    for name in ("design", "implement", "gate"):
        facts.stage_started(name)
        facts.stage_completed(name, True, False)
    lifecycle.close_scope(facts, token, True, [], False)
    from pbatch import task_center_io
    monkeypatch.setattr(task_center_io, "MAX_RECORDS", 1)
    view = task_center.snapshot()
    task = next(task for task in view["tasks"] if task["kind"] == "pipeline")
    assert task["plan"]["complete"] is True and len(task["plan"]["steps"]) == 3
    assert task["flow"]["next_status"] == "no_remaining_declared_stage"
    assert task["goal"]["acceptance"] == "untracked" and task["usage"]["calls"] is None
    assert task["analysis"]["partial_window"] is True
    stored = lifecycle.load_run(source, facts.state["run_id"])
    assert stored["status"] == "succeeded"


def test_children_use_explicit_same_source_parent_not_their_title(source):
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    record = {"run_id": "process", "task_id": "child", "attempt": 1, "stage": "design",
              "phase": "execution", "status": "running", "task_brief": "example",
              "ts": lifecycle.stamp(), "parent_run_id": facts.state["run_id"],
              "parent_source_id": source["source_id"]}
    append_bounded(Path(source["data_dir"]) / "tasks" / "attempts.jsonl", record)
    tasks = task_center.snapshot()["tasks"]
    parent = next(task for task in tasks if task["kind"] == "pipeline")
    child = next(task for task in tasks if task["kind"] == "agent")
    assert child["lineage"]["parent_task_id"] == parent["task_id"]
    assert child["lineage"]["resolution"] == "observed_in_window"
    assert parent["lineage"]["children"] == [child["task_id"]]
    record.update(task_id="other-child", parent_source_id="src-" + "f" * 24)
    append_bounded(Path(source["data_dir"]) / "tasks" / "attempts.jsonl", record)
    other = next(task for task in task_center.snapshot()["tasks"] if task["local_task_id"] == "other-child")
    assert other["lineage"]["resolution"] == "unavailable_in_window"
    lifecycle.close_scope(facts, token, False, [], False)


def test_parallel_bindings_do_not_leak_into_later_pipeline_or_unscoped_tasks(source):
    task = Task(prompt="body", cwd=source["workspace"])
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    lifecycle.bind_tasks([task])
    assert lifecycle.parent_fields(task)["parent_run_id"] == facts.state["run_id"]
    lifecycle.close_scope(facts, token, False, [], False)
    assert lifecycle.parent_fields(task) == {}
    another, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    lifecycle.bind_tasks([task])
    assert lifecycle.parent_fields(task)["parent_run_id"] == another.state["run_id"]
    assert another.state["run_id"] != facts.state["run_id"]
    lifecycle.close_scope(another, token, False, [], False)


def test_dry_run_and_disabled_ledger_create_no_runtime_checkpoints(source, monkeypatch):
    assert lifecycle.open_scope(plan(source), source["workspace"], True) == (None, None)
    monkeypatch.setattr(config, "AGENT_LEDGER_ENABLED", False)
    assert lifecycle.open_scope(plan(source), source["workspace"], False) == (None, None)
    assert not (Path(source["data_dir"]) / "tasks").exists()


def test_missing_or_corrupt_checkpoint_is_not_restored_from_guesswork(source):
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    path = lifecycle.run_path(source, facts.state["run_id"])
    path.write_text("broken")
    facts.stage_started("design")
    assert facts.enabled is False and path.read_text() == "broken"
    with pytest.raises(ValueError):
        lifecycle.load_run(source, facts.state["run_id"])
    lifecycle.close_scope(facts, token, False, [], False)


def test_broken_observation_does_not_change_execution(source, monkeypatch):
    from pbatch import pipeline_native
    monkeypatch.setattr(lifecycle.PipelineFacts, "publish", lambda self: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(pipeline, "_run_pipeline_stage", lambda *a, **k: (False, []))
    results, failed = pipeline_native.run_pipeline_observed(plan(source))
    assert results == [] and failed == []
    assert lifecycle._SCOPE.get() is None


def test_actual_cli_start_finish_and_parallel_lineage_are_observed(source, tmp_path, monkeypatch):
    from pbatch.runner import run_parallel
    fake = tmp_path / "fake-agent"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(0.05)\nprint('actual output')\n")
    fake.chmod(0o700)
    monkeypatch.setattr(metering, "BUDGET_MAX", 0)
    monkeypatch.setattr(metering, "DAILY_BUDGET", 0)
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    try:
        tasks = [Task(prompt="same", cwd=source["workspace"], agent=str(fake),
                      memory={"stage": "design"}) for _ in range(2)]
        results = run_parallel(tasks, workers=2)
        assert all(result.success for result in results)
        from pbatch.task_attempt_facts import flush
        assert flush(2)
        rows, coverage = read_rows(Path(source["data_dir"]) / "tasks" / "attempts.jsonl")
        assert not coverage["error"]
        starts = [row for row in rows if row["status"] == "running"]
        assert len(starts) == 2 and all(row["agent_pid"] > 0 for row in starts)
        assert {row["parent_run_id"] for row in rows} == {facts.state["run_id"]}
        observed = [task for task in task_center.snapshot()["tasks"] if task["kind"] == "agent"]
        assert len(observed) == 2 and all(task["usage"]["calls"] == 1 for task in observed)
        assert all(task["usage"]["llm_requests"] is None for task in observed)
    finally:
        lifecycle.close_scope(facts, token, False, [], False)


def test_same_process_repeated_pipeline_task_ids_do_not_merge_or_double_bill(source, tmp_path, monkeypatch):
    from pbatch.runner import run_task
    from pbatch.task_attempt_facts import flush
    fake = tmp_path / "fake-repeat"
    fake.write_text("#!/usr/bin/env python3\nprint('output')\n")
    fake.chmod(0o700)
    monkeypatch.setattr(metering, "BUDGET_MAX", 0)
    monkeypatch.setattr(metering, "DAILY_BUDGET", 0)
    monkeypatch.setattr(metering, "EVENTS_FILE", source["events_file"])
    result_ids, parent_ids = [], []
    for _ in range(2):
        facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
        result = run_task(Task(prompt="same", cwd=source["workspace"], agent=str(fake),
                              memory={"stage": "design"}))
        assert result.success
        result_ids.append(result.task_id)
        parent_ids.append(facts.state["run_id"])
        assert flush(2)
        lifecycle.close_scope(facts, token, False, [], False)
    assert result_ids[0] == result_ids[1]  # existing runner identity ABI is unchanged
    tasks = [row for row in task_center.snapshot()["tasks"] if row["kind"] == "agent"]
    assert len(tasks) == 2 and len({row["task_id"] for row in tasks}) == 2
    assert {row["lineage"]["parent_run_id"] for row in tasks} == set(parent_ids)
    assert all(row["usage"]["calls"] == 1 for row in tasks)


def test_slow_observation_writer_does_not_consume_the_agent_hard_deadline(source, tmp_path, monkeypatch):
    import threading
    from pbatch import task_attempt_facts
    from pbatch.runner import run_task
    assert task_attempt_facts.flush(2)
    entered, release = threading.Event(), threading.Event()
    original = task_attempt_facts.append_bounded

    def delayed(path, value):
        entered.set()
        release.wait(3)
        original(path, value)

    monkeypatch.setattr(task_attempt_facts, "append_bounded", delayed)
    fake = tmp_path / "fast-agent"
    fake.write_text("#!/usr/bin/env python3\nprint('fast output')\n")
    fake.chmod(0o700)
    monkeypatch.setattr(metering, "BUDGET_MAX", 0)
    monkeypatch.setattr(metering, "DAILY_BUDGET", 0)
    try:
        result = run_task(Task(prompt="same", cwd=source["workspace"], agent=str(fake), timeout=1))
        assert entered.is_set() and not release.is_set()
        assert result.success, result.reason
    finally:
        release.set()
        assert task_attempt_facts.flush(4)


def test_invalid_checkpoint_dependency_is_visible_not_a_ready_plan(source):
    facts, token = lifecycle.open_scope(plan(source), source["workspace"], False)
    bad = json.loads(json.dumps(facts.state))
    bad["steps"][0]["dependencies"] = ["gate"]
    path = Path(source["data_dir"]) / "tasks" / "lifecycle.jsonl"
    append_bounded(path, bad)
    view = task_center.snapshot()
    info = next(item for item in view["sources"][0]["coverage"] if item["feed"] == "lifecycle")
    assert info["malformed"] == 1
    lifecycle.close_scope(facts, token, False, [], False)
