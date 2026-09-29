"""Structured meta task DAG parsing and deterministic execution contracts."""

from __future__ import annotations

import json
from pathlib import Path

from pbatch.meta import _run_meta_task_dag, _select_roles
from pbatch.meta_plan import parse_task_dag, task_dag_waves
from pbatch.models import Stage, TaskResult
from pbatch.pipeline_loader import parse_stage_def
from pbatch.pipeline_schema import validate_pipeline


def _plan():
    return [
        {"id": "design", "role": "designer", "task": "produce a design",
         "depends_on": []},
        {"id": "audit", "role": "auditor", "task": "audit inputs",
         "depends_on": []},
        {"id": "review", "role": "reviewer", "task": "review the design",
         "depends_on": ["design"]},
    ]


def test_task_dag_parser_validates_and_returns_stable_waves():
    plan, error = parse_task_dag("plan: " + json.dumps(_plan()), 4)

    assert error == ""
    assert [[item["id"] for item in wave] for wave in task_dag_waves(plan)] == [
        ["design", "audit"], ["review"]]


def test_task_dag_parser_fails_closed_on_unknown_dependency_and_cycle():
    unknown = [{"id": "a", "role": "reviewer", "depends_on": ["missing"]}]
    cyclic = [
        {"id": "a", "role": "reviewer", "depends_on": ["b"]},
        {"id": "b", "role": "auditor", "depends_on": ["a"]},
    ]

    assert "unknown task" in parse_task_dag(json.dumps(unknown), 4)[1]
    assert "cycle" in parse_task_dag(json.dumps(cyclic), 4)[1]
    assert "fan-out" in parse_task_dag(json.dumps(_plan()), 2)[1]


def test_pipeline_schema_and_loader_accept_only_meta_dag_flag():
    data = {"stages": [
        {"name": "source", "from_prompt": "idea", "output": "idea.md"},
        {"name": "review", "from_outputs": "source", "meta": True,
         "output_dir": "reviews", "meta_task_dag": True},
    ]}

    assert validate_pipeline(data) == []
    stage = parse_stage_def(data["stages"][1], False)
    assert stage.meta_task_dag
    assert stage.to_dict()["meta_task_dag"] is True
    data["stages"][1]["meta_task_dag"] = "yes"
    assert any("meta_task_dag must be boolean" in error
               for error in validate_pipeline(data))
    data["stages"][1]["meta_task_dag"] = True
    data["stages"][1]["max_roles_per_iteration"] = 65
    assert any("at most 64 tasks" in error for error in validate_pipeline(data))


def test_dag_scheduler_runs_independent_wave_then_injects_dependency(
        tmp_path, monkeypatch):
    stage, plan, base, observed, batches = _scheduler_fixture(tmp_path)

    def run_batch(_stage, role_tasks, _validate, _session, _reuse,
                  _legacy, _fingerprint, all_results, role_outputs,
                  evidence_outputs, _iteration, _cancel):
        batches.append([task.memory["dag_id"] for _, task in role_tasks])
        for _role, task in role_tasks:
            if task.memory["dag_id"] == "review":
                observed.append(task.prompt)
            output = task.output_path()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(task.memory["dag_id"] + "-result", encoding="utf-8")
            role_outputs.append(str(output))
            evidence_outputs.append(str(output))
            all_results.append(TaskResult(task, True, stdout="ok", returncode=0))
        return []

    monkeypatch.setattr("pbatch.meta._run_observed_role_batch", run_batch)
    assert not _run_meta_task_dag(
        stage, plan, "base context", "", 0, "", "", False, False,
        False, [], [], [str(base)], 1)
    assert batches == [["design", "audit"], ["review"]]
    assert "design-result" in observed[0]


def test_dag_failure_blocks_dependents_but_keeps_independent_work(
        tmp_path, monkeypatch):
    stage, plan, _base, _observed, batches = _scheduler_fixture(tmp_path)

    def run_batch(_stage, role_tasks, _validate, _session, _reuse,
                  _legacy, _fingerprint, all_results, role_outputs,
                  evidence_outputs, _iteration, _cancel):
        batches.append([task.memory["dag_id"] for _, task in role_tasks])
        failed = []
        for _role, task in role_tasks:
            success = task.memory["dag_id"] != "design"
            if success:
                output = task.output_path()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("independent result", encoding="utf-8")
                role_outputs.append(str(output))
                evidence_outputs.append(str(output))
            result = TaskResult(task, success, reason="failed" if not success else "",
                                returncode=0 if success else 1)
            all_results.append(result)
            if not success:
                failed.append(result)
        return failed

    monkeypatch.setattr("pbatch.meta._run_observed_role_batch", run_batch)
    results = []
    assert _run_meta_task_dag(
        stage, plan, "base context", "", 0, "", "", False, False,
        False, results, [], [str(_base)], 1)
    assert batches == [["design", "audit"]]
    assert sum(not result.success for result in results) == 2
    assert any("dependency task did not complete" in result.reason
               for result in results)


def test_dag_relevance_filter_cannot_leave_dangling_dependencies(monkeypatch):
    stage = Stage(name="review", meta=True, meta_task_dag=True,
                  max_roles_per_iteration=4, relevance_min_score=1)
    monkeypatch.setattr("pbatch.meta.load_role_keywords",
                        lambda _path: {"reviewer": ["security"]})
    plan = [
        {"id": "audit", "role": "auditor", "task": "", "depends_on": []},
        {"id": "review", "role": "reviewer", "task": "", "depends_on": ["audit"]},
    ]

    assert _select_roles(stage, "security", ["reviewer", "auditor"],
                         json.dumps(plan)) is None


def _scheduler_fixture(tmp_path: Path):
    base = tmp_path / "input.md"
    base.write_text("source context", encoding="utf-8")
    output_dir = tmp_path / "roles"
    stage = Stage(name="review", meta=True, meta_task_dag=True,
                  output_dir=str(output_dir), cwd=str(tmp_path), workers=2)
    observed, batches = [], []
    return stage, _plan(), base, observed, batches
