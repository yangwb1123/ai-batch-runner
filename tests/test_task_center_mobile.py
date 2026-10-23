"""Task-first mobile cards for the read-only global view; executed in a bounded Node VM."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pbatch import task_center_mobile
from pbatch.task_center_report import page_html

NODE = shutil.which("node")
FIXTURE = Path(__file__).parent / "fixtures" / "task_center_mobile_browser.js"
pytestmark = pytest.mark.skipif(
    NODE is None, reason="Node runtime required for the isolated mobile view test")
OLD = "MOBILE-OLD-TASK"
NEW = "MOBILE-NEW-TASK"
MARKUP = '<img src=x onerror="throw new Error(1)"><script>alert(1)</script>'


def _scripts() -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page_html(), re.S)[:2]


def _resources() -> dict:
    return {"usage": {"total_tokens": None, "token_coverage": "unavailable", "calls": None},
            "billing": {"currency_totals": [], "financial_coverage": "unavailable",
                        "unknown_currency_observations": 0, "missing_amount_observations": 0,
                        "provider_requests": {"observed_total": None},
                        "assistant_usage_messages": {"observed_total": None}}}


def _task(marker: str, **overrides) -> dict:
    task = {"task_id": "tc-" + "a" * 32, "source_id": "src-" + "a" * 24,
            "project": "owned-project", "kind": "direction", "title": marker,
            "status": "RUNNING", "phase": "execution", "local_task_id": "owned-task",
            "analysis": {"flags": []},
            "flow": {"last_observed_stage": "implement", "next_stage": None,
                     "next_status": "untracked"},
            "time": {"elapsed_wall_seconds": 12.5, "remaining_seconds": None},
            "forecast": {"duration_range_seconds": None},
            "llm": [{"agent": "codex", "provider": "openai-codex",
                     "model": "gpt-6.1-sol", "thinking": "max",
                     "origin": "recorded_configuration"}],
            "goal_links": [],
            "delivery": {"present_files": None, "declared_files": None,
                         "acceptance": "untracked", "merge_status": ""},
            "scheduling": {"queue_wait_seconds": 3.5, "execution_wall_seconds": 12,
                           "execution_status": "PASSED"}, **_resources()}
    task.update(overrides)
    return task


def _view(marker: str, tasks: list[dict] | None = None) -> dict:
    rows = tasks if tasks is not None else [_task(marker)]
    return {"schema_version": 1,
            "sources": [{"source_id": "src-" + "a" * 24, "name": "owned-project",
                         "coverage": []}],
            "tasks": rows, "goals": [], "intents": [], "repairs": [], "resources": [],
            "feedback": [], "activity": {"processes": [], "diagnostics": []},
            "diagnostics": [], "observed_task_count": len(rows), "generated_at": marker}


def _run(mode: str, views: list[dict], **kwargs) -> dict:
    result = subprocess.run(
        [NODE, str(FIXTURE)],
        input=json.dumps({"scripts": _scripts(), "mode": mode, "views": views,
                          "old_marker": OLD, "new_marker": NEW, **kwargs}),
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_mobile_cards_are_task_first_and_keep_desktop_rows_literal_and_bounded():
    failed = _task("MOBILE-FAILED-TASK", task_id="tc-" + "b" * 32, status="FAILED",
                   phase="validation", analysis={"flags": ["repeated_failure"]}, llm=[],
                   scheduling={"queue_wait_seconds": None, "execution_wall_seconds": None,
                               "execution_status": None})
    rejected = _task("MOBILE-REJECTED-TASK", task_id="tc-" + "c" * 32,
                     status="EXECUTION_COMPLETED",
                     scheduling={"queue_wait_seconds": 2,
                                 "execution_wall_seconds": 4,
                                 "execution_status": "GATE_REJECTED"})
    view = _view(MARKUP, [_task(MARKUP, phase="merge"), failed, rejected])
    result = _run("render", [view])
    mobile, desktop = result["mobile"], result["desktop"]
    assert MARKUP in mobile and MARKUP in desktop
    assert result["tags"] == 0 and result["mobile_count"] == 3
    assert result["desktop_rows"] == 3
    for text in ("owned-project", "状态 / 阶段：RUNNING / merge",
                 "执行结果 PASSED（执行完成不等于合并/验收）",
                 "合并记录 未追踪", "目标验收 未追踪",
                 "任务累计 12.5 秒", "调度排队 3.5 秒", "调度执行 12 秒",
                 "thinking max（记录的请求配置，非 provider 确认的强度或时长）"):
        assert text in mobile, text
    assert "需关注：MOBILE-FAILED-TASK；MOBILE-REJECTED-TASK" in result["mobile_warnings"]
    assert "执行结果 未追踪（不推断成功）" in mobile
    assert "模型配置未追踪；thinking 未追踪（不推断请求配置）" in mobile
    assert all("token=" not in request["path"] for request in result["requests"])


def test_unknown_thinking_and_unavailable_usage_stay_untracked_not_zero_filled():
    unknown = _task(OLD, llm=[{"agent": "codex", "provider": "openai-codex",
                               "model": "gpt-6.1-sol", "thinking": None,
                               "origin": "recorded_configuration"}])
    view = _view(OLD, [unknown, _task("MOBILE-NO-MODEL", llm=[], forecast={})])
    result = _run("render", [view])
    mobile = result["mobile"]
    assert "thinking 未追踪（不推断请求配置）" in mobile
    assert "模型配置未追踪；thinking 未追踪（不推断请求配置）" in mobile
    assert "未追踪 tokens (unavailable)" in mobile
    assert "币种/费用未追踪" in mobile
    assert "thinking=0" not in mobile and "0 秒" not in mobile
    assert "先验剩余区间 未追踪（非实时 ETA）" in mobile
    assert "合并记录 未追踪" in mobile
    assert result["tags"] == 0 and len(result["requests"]) == 1


def test_mobile_detail_selection_opens_recorded_task_without_extra_requests():
    view = _view(MARKUP)
    result = _run("detail", [view])
    selected = json.loads(result["detail"])
    assert selected["task_id"] == view["tasks"][0]["task_id"]
    assert selected["title"] == MARKUP
    assert result["tags"] == 0 and len(result["requests"]) == 1
    assert "旧数据" not in result["detail"]


@pytest.mark.parametrize("mode", ["pending", "source_switch", "late_reply", "timeout"])
def test_both_views_clear_on_refresh_pending_source_switch_timeout_and_late_replies(mode):
    result = _run(mode, [_view(OLD), _view(NEW)],
                   source="src-" + "b" * 24)
    if mode == "pending":
        cleared = result["pending"]
    elif mode == "timeout":
        cleared = result["timed_out"]
    else:
        cleared = result["cleared"]
    final = result["final"]
    assert cleared["mobile_count"] == 0 and cleared["desktop_rows"] == 0
    assert OLD not in cleared["all"]
    assert OLD not in final["all"]
    if mode == "timeout":
        assert final["mobile_count"] == 0 and final["desktop_rows"] == 0
    else:
        assert NEW in final["mobile"]
    assert all(request["method"] == "GET" for request in final["requests"])


def test_auth_denial_clears_both_views_stops_polling_and_requires_explicit_reconnect():
    result = _run("denial", [_view(OLD), _view(NEW)])
    denied, final = result["denied"], result["final"]
    assert denied["mobile_count"] == 0 and denied["desktop_rows"] == 0
    assert OLD not in denied["all"]
    assert "认证" in denied["connection"]
    assert denied["form_hidden"] is False
    assert NEW in final["mobile"] and final["form_hidden"] is True
    assert final["requests"][-1]["authorization"] == "Bearer fresh-token"
    assert all("token=" not in request["path"] for request in final["requests"])


def test_mobile_script_adds_no_requests_storage_or_controls_and_keeps_legacy_scripts_first():
    html = page_html()
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "public shell element identities must be unique"
    for needle in ('id="mobile-tasks"', 'id="mobile-warnings"', 'id="tasks"',
                   'id="attention"', 'id="detail"'):
        assert html.count(needle) == 1, needle
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    first, mobile = scripts[0], scripts[1]
    for needle in ("function renderTask", "function resources", "td.textContent=text",
                   "async function refresh", "setInterval(refresh,5000)",
                   "/api/tasks?limit=200", "function clearView"):
        assert needle in first, needle
    for banned in ("fetch(", "XMLHttpRequest", "localStorage", "sessionStorage",
                   "document.cookie", "innerHTML", "/api/", "Authorization"):
        assert banned not in mobile, banned
    assert "pbatchMobileView" in mobile and "textContent" in mobile
    assert "@media(max-width:600px)" in task_center_mobile.MOBILE_STYLE
    assert ".desktop-tasks{display:none}" in task_center_mobile.MOBILE_STYLE
    assert "#mobile-tasks-view{display:block}" in task_center_mobile.MOBILE_STYLE
    assert any("navigator.serviceWorker.register" in script for script in scripts[2:])
