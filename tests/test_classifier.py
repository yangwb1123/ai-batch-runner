"""Tests for the task type classifier (pbatch/classifier.py), the classify
subcommand, and the --classify auto-routing hook in pbatch/cli.py."""

from __future__ import annotations

import io
import json
import sys

import pytest

from pbatch import classifier
from pbatch import cli
from pbatch.classifier import (FRONTEND, TaskClassification,
                               classify_main, classify_text,
                               load_keywords, should_route_frontend)
from pbatch.models import Task


def test_frontend_detection_with_platform_and_profile():
    item = classify_text("用 Flutter 实现 ERP 生产排程列表页，布局按规范")
    assert item.task_type == FRONTEND
    assert item.platform == "dart"
    assert item.profile == "erp"
    assert item.confident
    assert item.score >= 4


def test_frontend_platform_boost_tsx():
    item = classify_text("write a login page in react tsx")
    assert item.task_type == FRONTEND
    assert item.platform == "tsx"
    assert item.confident


def test_backend_and_analysis_do_not_route():
    backend = classify_text("写一个后端 API 查询接口")
    assert backend.task_type == "backend"
    analysis = classify_text("分析 MFA 子系统的错误处理")
    assert analysis.task_type == "analysis"
    assert should_route_frontend([backend, analysis]) is False


def test_admin_console_keywords():
    item = classify_text("生成客户管理后台")
    assert item.task_type == FRONTEND
    assert item.confident


def test_unknown_low_score_is_best_guess():
    item = classify_text("随便写点什么")
    assert not item.confident
    assert item.score == 0
    # A zero-score best guess must never divert a task into the UI pipeline.
    assert item.task_type == "unknown"
    assert should_route_frontend([item]) is False


def test_profile_only_frontend_signal():
    # No generic UI word: profile terms alone must still classify as UI.
    item = classify_text("生成一个营销落地页，特效风格")
    assert item.task_type == FRONTEND
    assert item.profile == "marketing"
    assert item.confident
    assert should_route_frontend([item]) is True


def test_backend_beats_weak_profile_signal():
    # "ERP 模块的后端接口" is backend work despite the erp profile word.
    item = classify_text("实现 ERP 模块的后端接口")
    assert item.task_type == "backend"
    assert item.confident


def test_keywords_yaml_overrides_defaults(tmp_path):
    override = tmp_path / "keywords.yaml"
    override.write_text(
        "task_types:\n  frontend_ui:\n    - 量子页面\n", encoding="utf-8")
    kw = load_keywords(str(override))
    assert "量子页面" in kw["task_types"]["frontend_ui"]
    # Sections absent from the override keep built-in defaults.
    assert "backend" in kw["task_types"]
    item = classify_text("做量子页面", kw)
    assert item.task_type == FRONTEND


@pytest.mark.parametrize("name,body", [
    ("missing.yaml", None),
    ("bad.yaml", "task_types: [broken"),
    ("shape.yaml", "task_types: []\n"),
    ("unknown.yaml", "mystery:\n  x: [y]\n"),
])
def test_explicit_keyword_index_fails_closed(tmp_path, name, body):
    index = tmp_path / name
    if body is not None:
        index.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        classify_main(["--index", str(index), "做一个页面"])
    assert exc.value.code == 2


@pytest.mark.parametrize("name,body", [
    ("bad.yaml", "tasks: [broken"),
    ("bad.json", '{"tasks": [}'),
    ("empty.yaml", "tasks: []\n"),
    ("missing-prompt.json", '{"tasks": [{"output": "x"}]}'),
])
def test_structured_classifier_source_fails_closed(tmp_path, name, body):
    source = tmp_path / name
    source.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        classify_main([str(source)])
    assert exc.value.code == 2


def test_platform_boost_lifts_tied_frontend():
    # "实现一个 flutter 后端接口" mentions backend words but flutter is a
    # decisive platform signal for UI work.
    item = classify_text("实现一个 flutter 后端接口")
    assert item.task_type == FRONTEND


def test_classify_tasks_majority_and_tie_break():
    frontend = TaskClassification(FRONTEND, 4, confident=True)
    backend = TaskClassification("backend", 4, confident=True)
    dominant, per = classifier.classify_tasks([frontend, backend, backend])
    assert dominant.task_type == "backend"
    # Tie goes to frontend (err toward the spec/review pipeline).
    dominant, per = classifier.classify_tasks([frontend, backend])
    assert dominant.task_type == FRONTEND


def test_should_route_frontend_ratio():
    frontend = TaskClassification(FRONTEND, 4, confident=True)
    backend = TaskClassification("backend", 4, confident=True)
    assert should_route_frontend([frontend]) is True
    assert should_route_frontend([frontend, backend]) is True
    assert should_route_frontend([frontend, backend, backend]) is False
    assert should_route_frontend([]) is False


def test_classify_subcommand_json(capsys):
    sys.argv = ["pi-batch.py", "classify", "--json", "用 React 生成仪表盘"]
    classify_main(sys.argv[2:])
    out = json.loads(capsys.readouterr().out)
    assert out["tasks"][0]["task_type"] == FRONTEND
    assert out["route_frontend"] is True
    assert out["frontend_pipeline"]


def test_classify_subcommand_plain_prompt(capsys):
    sys.argv = ["pi-batch.py", "classify", "分析数据库表结构"]
    classify_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "backend" in out
    assert "route to frontend pipeline: False" in out


def test_maybe_classify_route_frontend(monkeypatch, tmp_path):
    pipeline = tmp_path / "ui-gen.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setattr(classifier.config, "CLASSIFIER_FRONTEND_PIPELINE", str(pipeline))
    args = cli.argparse.Namespace(
        classify=True, pipeline="", prompt="用 flutter 做一个登录页",
        source="", from_dir="", output="", model="", provider="", timeout=0)
    cli._maybe_classify_route(args)
    assert args.pipeline == str(pipeline)


def test_maybe_classify_route_backend_routes_to_backend_pipeline(monkeypatch, tmp_path):
    pipeline = tmp_path / "be-impl.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setattr(classifier.config, "CLASSIFIER_BACKEND_PIPELINE", str(pipeline))
    args = cli.argparse.Namespace(
        classify=True, pipeline="", prompt="写一个订单创建接口，含支付，go 实现",
        source="", from_dir="", output="", model="", provider="", timeout=0)
    cli._maybe_classify_route(args)
    assert args.pipeline == str(pipeline)


def test_stdin_is_buffered_and_classified_before_execution(monkeypatch, tmp_path):
    pipeline = tmp_path / "be-impl.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    monkeypatch.setattr(classifier.config, "CLASSIFIER_BACKEND_PIPELINE", str(pipeline))
    monkeypatch.setattr(sys, "stdin", io.StringIO("写一个订单后端 API，用 Go 实现"))
    args = cli.argparse.Namespace(
        classify=True, pipeline="", prompt="", source="", from_dir="",
        output="", model="", provider="", timeout=0, suffix=".md")
    cli._buffer_stdin(args)
    cli._maybe_classify_route(args)
    assert args.stdin_prompt == "写一个订单后端 API，用 Go 实现"
    assert args.pipeline == str(pipeline)


def test_maybe_classify_route_explicit_pipeline_wins(monkeypatch):
    args = cli.argparse.Namespace(
        classify=True, pipeline="custom.yaml", prompt="用 flutter 做页面",
        source="", from_dir="", output="", model="", provider="", timeout=0)
    cli._maybe_classify_route(args)
    assert args.pipeline == "custom.yaml"


def test_maybe_classify_route_missing_pipeline_fails(monkeypatch):
    monkeypatch.setattr(classifier.config, "CLASSIFIER_FRONTEND_PIPELINE", "/nonexistent/ui.yaml")
    args = cli.argparse.Namespace(
        classify=True, pipeline="", prompt="用 flutter 做页面",
        source="", from_dir="", output="", model="", provider="", timeout=0)
    with pytest.raises(SystemExit) as exc:
        cli._maybe_classify_route(args)
    assert exc.value.code == 1


def test_ui_pipeline_schema_valid():
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/ui-generation-pipeline.yaml")
    names = [stage.name for stage in pipeline.stages]
    assert names == ["classify", "generate", "review", "gate"]
    generate = pipeline.stages[1]
    assert generate.validate_cmd == "uispacing,uicolor,uistyle,uifont,uigeometry"


def test_ui_validators_resolve():
    from pbatch.config import _resolve_validators
    commands = _resolve_validators("uispacing,uicolor,uistyle,uifont,uigeometry")
    assert all("check-ui-spec.py" in cmd for cmd in commands[:4])
    assert "check-ui-geometry.py" in commands[4]


def test_check_ui_spec_script_rejects_and_accepts(tmp_path):
    import subprocess
    bad = tmp_path / "bad.dart"
    bad.write_text("SizedBox(height: 3)\nEdgeInsets.only(left: 13)\n", encoding="utf-8")
    good = tmp_path / "good.dart"
    good.write_text("SizedBox(height: 24)\nEdgeInsets.all(16)\n", encoding="utf-8")
    script = "scripts/check-ui-spec.py"
    assert subprocess.run(["python", script, "--mode", "spacing", str(bad)]).returncode == 1
    assert subprocess.run(["python", script, "--mode", "spacing", str(good)]).returncode == 0
    tsx = tmp_path / "bad.tsx"
    tsx.write_text("style={{ marginLeft: 13 }}\ncolor: #1296db\n", encoding="utf-8")
    assert subprocess.run(["python", script, "--mode", "style", str(tsx)]).returncode == 1
    assert subprocess.run(["python", script, "--mode", "color", str(tsx)]).returncode == 1


def test_default_routing_pipelines_exist():
    """The classifier's default routing targets must exist (fail loud)."""
    from pathlib import Path
    assert Path(classifier.config.CLASSIFIER_FRONTEND_PIPELINE).exists()
    assert Path(classifier.config.CLASSIFIER_BACKEND_PIPELINE).exists()
    assert "frontend-implementation" in classifier.config.CLASSIFIER_FRONTEND_PIPELINE


def test_frontend_implementation_pipeline_schema():
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/frontend-implementation-pipeline.yaml")
    assert [s.name for s in pipeline.stages] == ["plan", "implement", "review", "gate"]
    assert pipeline.stages[1].validate_cmd == "uiquality,uicheck,uifont,uigeometry,completion"
    assert pipeline.stages[2].meta is True
    assert pipeline.stages[3].gate is True
    plan = pipeline.stages[0].from_prompt
    assert "pi-batch assess" in plan
    assert "layout-patterns" in plan
    assert "pi-batch context" in plan
    assert "component tree" in plan


def test_visual_review_pipeline_schema():
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/visual-review-pipeline.yaml")
    assert [s.name for s in pipeline.stages] == ["generate", "visual", "review", "gate"]
    assert len(pipeline.stages[1].commands) == 4  # 3 viewports + scoring
    assert "playwright screenshot" in pipeline.stages[1].commands[0]
    assert pipeline.stages[3].gate is True


def test_classify_prompt_with_slashes_is_not_a_path(capsys):
    """A task text containing '/' must not be treated as a file path."""
    sys.argv = ["pi-batch.py", "classify", "重构 pbatch/rule_matcher.py 降低复杂度"]
    from pbatch.classifier import classify_main
    classify_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "code_engineering" in out
    assert "File not found" not in out


class TestSystemType:
    """问题系统分类学：prompt → 系统类型 → 方法论路由信号。"""

    def test_state_machine_detected(self):
        r = classify_text("订单审批流，状态流转，审核流程")
        assert r.system_type == "state-machine"
        assert r.system_evidence

    def test_event_driven_detected(self):
        r = classify_text("事件驱动的消息队列通知系统")
        assert r.system_type == "event-driven"

    def test_search_detected(self):
        r = classify_text("客户搜索检索推荐，模糊匹配")
        assert r.system_type == "search"

    def test_optimization_detected(self):
        r = classify_text("生产排程调度，资源分配优化")
        assert r.system_type == "optimization"

    def test_knowledge_detected(self):
        r = classify_text("知识库问答系统，文档检索")
        assert r.system_type == "knowledge"

    def test_realtime_detected(self):
        r = classify_text("实时监控大屏，websocket 推送告警")
        assert r.system_type == "realtime"

    def test_default_deterministic(self):
        r = classify_text("做一个登录页 demo")
        assert r.system_type == "deterministic"

    def test_system_type_in_dict(self):
        r = classify_text("审批流")
        d = r.to_dict()
        assert d["system_type"] == "state-machine"
        assert isinstance(d["system_evidence"], list)
