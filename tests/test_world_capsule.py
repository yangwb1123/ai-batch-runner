"""Tests for 世界状态平面 / DecisionCapsule / N-version / OR 算子 / 门禁分级.

world: 快照捕获（git HEAD/工作区）、计划绑定、漂移检查与 replan 裁决。
capsule: 版本化上下文胶囊（可复现性字段）与差异对比。
nversion: N 次执行一致性判定（确定性收敛 / 随机发散）。
pipeline OR: 候选任务按序执行、第一个通过者胜出、全败 fail closed。
profile: 人工门禁分级 + 提问价值。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from pbatch import capsule, nversion, world
from pbatch.profile import approval_level, question_value, task_profile


# ---------------------------------------------------------------------------
# world：世界状态平面
# ---------------------------------------------------------------------------

def test_capture_world_snapshot_shape(tmp_path):
    snap = world.capture_world_snapshot(str(tmp_path))
    assert snap["version"]
    assert snap["repository_head"] == "unknown"  # 非 git 仓库 → fail closed
    assert snap["fabric_protocol"]
    assert snap["as_of"]


def test_capture_world_snapshot_in_git_repo():
    snap = world.capture_world_snapshot(".")
    assert snap["repository_head"]  # 本仓库是 git 仓库
    assert isinstance(snap["dirty_files"], int)


def test_plan_bind_and_drift_identical():
    snap = world.capture_world_snapshot(".")
    bound = world.plan_bind(snap)
    drift = world.drift_between(bound, snap)
    assert drift["verdict"] == "proceed"
    assert drift["exceeds_threshold"] is False


def test_drift_detects_head_change():
    bound = world.plan_bind({"version": "v1", "repository_head": "aaa",
                             "dirty_files": 0, "as_of": "now"})
    current = {"version": "v2", "repository_head": "bbb", "dirty_files": 0}
    drift = world.drift_between(bound, current)
    assert drift["head_changed"] is True
    assert drift["exceeds_threshold"] is True
    assert drift["verdict"] == "replan_required"


def test_drift_dirty_delta_below_threshold():
    bound = world.plan_bind({"version": "v1", "repository_head": "aaa",
                             "dirty_files": 2, "as_of": "now"})
    current = {"version": "v2", "repository_head": "aaa", "dirty_files": 4}
    assert world.drift_between(bound, current, threshold=3)[
        "exceeds_threshold"] is False
    assert world.drift_between(bound, current, threshold=1)[
        "exceeds_threshold"] is True


def test_world_cli_bind_drift(tmp_path, capsys):
    from pbatch.world import world_main
    bound_file = tmp_path / "bound.json"
    with pytest.raises(SystemExit) as exc:
        world_main(["bind", "--out", str(bound_file), "--cwd", str(tmp_path)])
    assert exc.value.code == 0
    capsys.readouterr()  # 清空 bind 输出
    with pytest.raises(SystemExit) as exc2:
        world_main(["drift", "--bound", str(bound_file),
                    "--cwd", str(tmp_path), "--json"])
    assert exc2.value.code == 0  # 无变化 → proceed
    data = json.loads(capsys.readouterr().out)
    assert data["drift"]["verdict"] == "proceed"


# ---------------------------------------------------------------------------
# capsule：DecisionCapsule
# ---------------------------------------------------------------------------

def test_capsule_replayable_fields(tmp_path):
    cap1 = capsule.capture_context_capsule("写一个登录页", cwd=str(tmp_path))
    cap2 = capsule.capture_context_capsule("写一个登录页", cwd=str(tmp_path))
    # 同一上下文 → 可复现性字段一致（capsule_id/时间除外）
    assert cap1["prompt_hash"] == cap2["prompt_hash"]
    assert cap1["rule_set_version"] == cap2["rule_set_version"]
    assert cap1["tool_versions"]["fabric"] == cap2["tool_versions"]["fabric"]
    assert "capsule_id" in cap1
    # 不同 prompt → 哈希不同
    cap3 = capsule.capture_context_capsule("写一个订单页", cwd=str(tmp_path))
    assert cap1["prompt_hash"] != cap3["prompt_hash"]


def test_capsule_diff():
    base = capsule.capture_context_capsule("x")
    changed = dict(base)
    changed["rule_set_version"] = "other"
    diffs = capsule.capsule_diff(base, changed)
    assert any("rule_set_version" in d for d in diffs)
    assert capsule.capsule_diff(base, dict(base)) == []


def test_capsule_rule_digests_use_installed_assets_and_project_overrides(
        tmp_path):
    captured = capsule.capture_context_capsule("x", cwd=str(tmp_path))
    assert set(captured["rule_digests"]) == set(capsule.RULE_REGISTRIES)
    assert all(value != "missing"
               for value in captured["rule_digests"].values())

    override = tmp_path / "ui-specs" / "rules.yaml"
    override.parent.mkdir()
    override.write_text("rules: []\n", encoding="utf-8")
    updated = capsule.capture_context_capsule("x", cwd=str(tmp_path))
    assert updated["rule_digests"]["ui-specs/rules.yaml"] == (
        capsule._file_digest(override))
    assert updated["rule_digests"]["backend-specs/rules.yaml"] == (
        captured["rule_digests"]["backend-specs/rules.yaml"])


def test_capsule_honors_explicit_empty_rule_digest_mapping(tmp_path):
    captured = capsule.capture_context_capsule(
        "x", cwd=str(tmp_path), rule_digests={})
    assert captured["rule_digests"] == {}


def test_capsule_eval_uses_installed_module_entrypoint(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(subprocess, "run",
                        lambda command, **_kwargs: calls.append(command) or Result())
    assert capsule._run_eval() == (True, "ok")
    assert calls == [[sys.executable, "-m", "pbatch", "eval"]]


def test_capsule_cli(tmp_path, capsys):
    from pbatch.capsule import capsule_main
    out = tmp_path / "cap.json"
    with pytest.raises(SystemExit) as exc:
        capsule_main(["--prompt", "任务", "--out", str(out)])
    assert exc.value.code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["prompt_hash"] and data["world_version"]


# ---------------------------------------------------------------------------
# nversion：N-version 执行
# ---------------------------------------------------------------------------

def test_nversion_deterministic_identical():
    report = nversion.run_n_versions(
        ["python3", "-c", "print('hello')"], n=3)
    assert report["consensus"] == "identical"
    assert report["identical_runs"] == 3


def test_nversion_divergent_detected():
    """随机输出 → 显式发散（不把单次输出当证据）。"""
    report = nversion.run_n_versions(
        ["python3", "-c", "import random; print(random.random())"], n=3)
    assert report["consensus"] == "divergent"
    assert report["identical_runs"] < 3


def test_nversion_failure_consensus():
    report = nversion.run_n_versions(
        ["python3", "-c", "import sys; sys.exit(3)"], n=2)
    assert report["consensus"] == "identical"
    assert report["runs"][0]["exit_code"] == 3


def test_nversion_cli(capsys):
    from pbatch.nversion import nversion_main
    with pytest.raises(SystemExit) as exc:
        nversion_main(["--n", "2", "--json", "--", "python3", "-c",
                       "print(1)"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consensus"] == "identical"


# ---------------------------------------------------------------------------
# pipeline OR 算子
# ---------------------------------------------------------------------------

def test_pipeline_schema_accepts_or_tasks():
    from pbatch.pipeline_schema import validate_pipeline
    errors = validate_pipeline({
        "stages": [{
            "name": "choose", "from_prompt": "task", "output": "out.md",
            "or_tasks": True,
            "tasks": [{"prompt": "A", "output": "a.md"},
                      {"prompt": "B", "output": "b.md"}],
        }],
    })
    assert errors == []


def test_pipeline_schema_or_requires_tasks():
    from pbatch.pipeline_schema import validate_pipeline
    errors = validate_pipeline({
        "stages": [{"name": "choose", "from_prompt": "task",
                    "output": "out.md", "or_tasks": True}],
    })
    assert any("or_tasks" in error for error in errors)


def test_or_tasks_stage_field_on_stage_model():
    from pbatch.models import Stage
    stage = Stage(name="s", or_tasks=True)
    assert stage.or_tasks is True


def test_execute_or_tasks_first_winner(tmp_path, monkeypatch):
    """OR：第一个通过验证器的候选胜出，其余不执行。"""
    import pbatch.pipeline as pipeline
    from pbatch.models import Stage, Task
    from pbatch.runner import TaskResult

    executed = []

    def fake_run_serial(tasks, **kwargs):
        executed.append(tasks[0].prompt)
        ok = tasks[0].prompt == "B"
        result = TaskResult(task=tasks[0], success=ok)
        if ok:
            result.task.output = str(tmp_path / "b.md")
            (tmp_path / "b.md").write_text("ok", encoding="utf-8")
        return [result]

    monkeypatch.setattr(pipeline, "run_serial", fake_run_serial)
    stage = Stage(name="or-stage", or_tasks=True)
    tasks = [Task(prompt="A", output=str(tmp_path / "a.md")),
             Task(prompt="B", output=str(tmp_path / "b.md")),
             Task(prompt="C", output=str(tmp_path / "c.md"))]
    results, outputs = pipeline._execute_or_tasks(
        stage, tasks, [], "", "new", "", "sid")
    assert executed == ["A", "B"]  # C 未执行
    assert results[-1].success is True
    assert str(tmp_path / "b.md") in outputs


def test_execute_or_tasks_all_fail_fails_stage(tmp_path, monkeypatch):
    """OR：全部候选失败 → 阶段失败（fail closed）。"""
    import pbatch.pipeline as pipeline
    from pbatch.models import Stage, Task
    from pbatch.runner import TaskResult

    def fake_run_serial(tasks, **kwargs):
        return [TaskResult(task=tasks[0], success=False)]

    monkeypatch.setattr(pipeline, "run_serial", fake_run_serial)
    stage = Stage(name="or-stage", or_tasks=True)
    tasks = [Task(prompt="A", output=str(tmp_path / "a.md")),
             Task(prompt="B", output=str(tmp_path / "b.md"))]
    results, outputs = pipeline._execute_or_tasks(
        stage, tasks, [], "", "new", "", "sid")
    assert all(not r.success for r in results)
    assert outputs == []


# ---------------------------------------------------------------------------
# 人工门禁分级 + 提问价值
# ---------------------------------------------------------------------------

def test_approval_level_grading():
    low = task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    assert approval_level(low) == "auto_report"
    high = task_profile("在 snaplink-console 增加文件审批，审批后记录审计并通知申请人")
    assert approval_level(high) in ("dry_run_approve", "double_review")
    # L3 审计任务 → 不可逆/重大 → 双人复核
    assert approval_level(high) == "double_review"


def test_question_value():
    """只有可能显著改变高影响决策时才值得打断用户。"""
    assert question_value(0.8, 0.9, interruption_cost=0.3) > 0   # 值得问
    assert question_value(0.1, 0.3, interruption_cost=0.3) < 0   # 不值得问
