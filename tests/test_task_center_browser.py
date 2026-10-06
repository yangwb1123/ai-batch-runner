"""Execute the shipped page script in a bounded Node VM + minimal DOM, not a visual browser."""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_task_goals import define, source
from test_task_intents import import_value, manifest, message
from test_task_native_history import importing, pi_rows, write_native
from test_task_history_export import export, importing as import_range
from pbatch import task_center, task_intent_store
from pbatch.task_center_report import page_html

NODE = shutil.which("node")
FIXTURE = Path(__file__).parent / "fixtures" / "task_center_browser.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node runtime required for the isolated browser-script test")
MARKER = "PRIVATE-OLD-HINT"


def run_browser(views, mode="render", **kwargs):
    script = re.search(r"<script>(.*?)</script>", page_html(), re.S).group(1)
    result = subprocess.run([NODE, str(FIXTURE)], input=json.dumps({"script": script, "views": views,
        "mode": mode, **kwargs}), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def normalized_view(source, text=MARKER):
    goal = define(source)
    value = import_value(source, goal, manifest(goal, [message(text="实现 " + text + "。必须保留现场。当前优先复现。下一步回归。")]))
    return value, task_center.snapshot()


def test_normalized_history_has_separate_categories_citations_and_unverified_complete_claim(source):
    _, view = normalized_view(source)
    result = run_browser([view])
    for category in ("目标候选", "约束候选", "当前重点候选", "下一步候选"):
        assert category in result["panel"] and category in result["intents"]
    assert "导出者声称完整（未独立审计）" in result["intents"]
    assert "同一个已确认的目标" in result["panel"] and "demo" in result["panel"]
    assert "消息 SHA-256" in result["panel"] and "片段 SHA-256" in result["panel"]
    assert "字符位置" in result["panel"] and "片段已脱敏且有界" in result["panel"]
    assert "语义漂移未评估" in result["panel"] and "署名未认证" in result["panel"]
    assert result["requests"] == [{"path": "/api/tasks?limit=200", "aborted": False,
                                    "authorization": "Bearer fixture-token", "method": "GET"}]


def test_native_branch_is_not_labelled_operator_complete_or_independent_replay(source):
    goal = define(source)
    importing(source, goal, write_native(source, pi_rows(source)))
    result = run_browser([task_center.snapshot()])
    assert "显式原生用户投影，非完整 session" in result["intents"]
    assert "其他分支未选用户 1" in result["intents"]
    assert "完整性为操作人声明" not in result["intents"]
    assert "当前原生文件复验" in result["panel"]


def test_range_coverage_audit_and_import_only_replay_are_visible_without_paths_or_raw_leaf(source):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    receipt = export(source, goal, path, first=2, last=2)
    import_range(source, goal, receipt, path)
    result = run_browser([task_center.snapshot()])
    assert "最近包用户范围 2–2 / 所选分支用户 2" in result["intents"]
    assert "原生文件用户 3，记录 5" in result["intents"]
    assert "仅导入时重放，当前原生文件未重新核验" in result["panel"]
    assert "导入覆盖与回执（不相加，不代表全历史）" in result["panel"]
    assert "explicit export only" in result["panel"] and "未认证" in result["panel"]
    assert str(path) not in result["all"] and receipt["path"] not in result["all"]
    assert "private-pi-id" not in result["all"]


@pytest.mark.parametrize("status", ["revoked", "stale_contract", "goal_unavailable", "unsupported_state"])
def test_unavailable_authorization_cannot_render_stale_hints_even_in_inconsistent_client_data(source, status):
    _, original = normalized_view(source)
    changed = copy.deepcopy(original)
    changed["intents"][0]["status"] = status  # intentionally leave old analysis: browser must suppress it
    result = run_browser([original, changed], "refresh", old_marker=MARKER)
    assert MARKER not in result["all"]
    assert "analysis" in result["detail"] and json.loads(result["detail"])["analysis"] is None
    assert "历史审计和已记录消耗仍保留" in result["panel"]


def test_actual_revocation_is_seen_on_next_poll_and_no_old_excerpt_remains(source):
    value, original = normalized_view(source)
    task_intent_store.revoke(value["intent_id"], "operator", "withdraw consent", 1)
    result = run_browser([original, task_center.snapshot()], "refresh", old_marker=MARKER)
    assert "已撤销" in result["panel"] and "withdraw consent" in result["panel"]
    assert MARKER not in result["all"]
    assert "已记录本地导入 1 次" in result["panel"]


def test_poll_failure_or_missing_record_does_not_retain_authorized_text(source):
    _, view = normalized_view(source)
    failed = run_browser([view], "failure", old_marker=MARKER)
    assert MARKER not in failed["all"] and failed["panel_hidden"] is True
    assert "不能确认任务状态或历史授权" in failed["detail"]
    missing = copy.deepcopy(view)
    missing["intents"] = []
    result = run_browser([view, missing], "missing", old_marker=MARKER)
    assert MARKER not in result["all"] and result["panel_hidden"] is True
    assert "不代表已删除或仍有授权" in result["detail"]


@pytest.mark.parametrize("mode", ["switch", "stale_error"])
def test_source_change_aborts_old_request_and_out_of_order_responses_cannot_restore_old_project(source, mode):
    _, old = normalized_view(source)
    new = copy.deepcopy(old)
    new["sources"][0].update(source_id="src-" + "b" * 24, name="project-B")
    new["goals"][0]["source_id"] = new["sources"][0]["source_id"]
    intent = new["intents"][0]
    intent["source_id"] = new["sources"][0]["source_id"]
    for rows in intent["analysis"]["hints"].values():
        for hint in rows:
            hint["excerpt"] = "NEW-PROJECT-HINT"
    result = run_browser([old, new], mode, source=new["sources"][0]["source_id"],
                         old_marker=MARKER, new_marker="NEW-PROJECT-HINT")
    assert MARKER not in result["all"] and "project-B" in result["intents"]
    assert result["requests"][0]["aborted"] is True
    assert result["requests"][1]["path"].endswith("source=" + new["sources"][0]["source_id"])
    assert result["error"] == ""


def test_prompt_markup_and_untrusted_audit_labels_are_literal_text_not_dom_elements(source):
    markup = '<img src=x onerror="throw new Error(1)"><script>alert(1)</script>'
    _, view = normalized_view(source, markup)
    view["intents"][0]["audit"][-1].update(by=markup, reason=markup)
    result = run_browser([view])
    assert markup in result["panel"]
    assert result["tags"] == 0
    assert "<script>" in result["detail"]


def test_import_accounting_remains_separate_from_original_agent_cost_and_unknown_is_not_zero(source):
    _, view = normalized_view(source)
    result = run_browser([view])
    assert "此确定性导入模型请求 0 / Token 0" in result["panel"]
    assert "费用 未追踪" in result["panel"]
    assert "不是原 Agent 消耗或所有监测的全程账单" in result["panel"]
    missing = copy.deepcopy(view)
    missing["intents"][0]["accounting"].update(llm_requests=None, total_tokens=None)
    result = run_browser([missing])
    assert "模型请求 未追踪 / Token 未追踪" in result["panel"]


def test_restatement_cues_keep_previous_and_current_citations_without_a_drift_verdict(source):
    goal = define(source)
    import_value(source, goal, manifest(goal, [message(text="实现一个目标。"),
        message("two", text="目标改为加入复现证据。", second=1)]))
    result = run_browser([task_center.snapshot()])
    assert "重述/转向提示（未确认，非漂移判决）" in result["panel"]
    assert "前引用" in result["panel"] and "后引用" in result["panel"]
    assert "scope_change_cue / unconfirmed / 语义漂移 not_evaluated" in result["panel"]


def test_selecting_goal_clears_structured_history_panel(source):
    _, view = normalized_view(source)
    result = run_browser([view], "select_other", other="goals")
    assert result["panel_hidden"] is True and result["panel"] == ""
    assert "statement" in result["detail"]
