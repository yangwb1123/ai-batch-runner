"""Delivered global-shell connection controller executed in an isolated Node VM."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pbatch.task_center_report import page_html

NODE = shutil.which("node")
FIXTURE = Path(__file__).parent / "fixtures" / "task_center_connection_browser.js"
pytestmark = pytest.mark.skipif(
    NODE is None, reason="Node runtime required for the isolated connection test")


def _script() -> str:
    return re.search(r"<script>(.*?)</script>", page_html(), re.S).group(1)


def run(mode: str, query: str = "?token=legacy-token") -> dict:
    result = subprocess.run(
        [NODE, str(FIXTURE)],
        input=json.dumps({"script": _script(), "mode": mode, "query": query}),
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_legacy_query_token_is_used_from_memory_and_stripped_from_the_url():
    state = run("legacy")
    assert state["requests"] == [{
        "path": "/api/tasks?limit=200", "aborted": False,
        "authorization": "Bearer legacy-token", "method": "GET"}]
    assert "最近成功刷新" in state["connection"]
    assert "未追踪" in state["tasks"] or "SCHED" not in state["tasks"]


def test_auth_denial_clears_data_stops_polling_and_requires_explicit_reconnect():
    state = run("denial")
    denied = state["denied"]
    assert "LIVE-A" not in denied["all"]
    assert denied["tasks"] == ""
    assert "认证" in denied["connection"]
    assert denied["form_hidden"] is False
    assert denied["input_value"] == ""
    assert len(denied["requests"]) == 3 and denied["requests"][1]["aborted"] is True
    final = state["final"]
    assert "FRESH-B" in final["tasks"]
    assert final["form_hidden"] is True
    assert final["requests"][-1]["authorization"] == "Bearer fresh-token"
    assert all("token=" not in request["path"] for request in final["requests"])


def test_request_timeout_is_a_stale_fact_not_an_empty_success():
    state = run("timeout")
    timed_out = state["timed_out"]
    assert timed_out["tasks"] == ""
    assert "超时" in timed_out["connection"]
    assert timed_out["summary"] == "数据不可用"
    assert "不能确认任务状态" in timed_out["detail"]
    assert "RECOVERED" in state["final"]["tasks"]


def test_network_failure_reports_disconnected_stale_and_the_next_poll_recovers():
    state = run("network")
    stale = state["stale"]
    assert "连接中断" in stale["connection"]
    assert "最近成功刷新" in stale["connection"]
    assert "刷新失败: offline" in stale["error"]
    assert stale["tasks"] == ""
    assert "BACK-ONLINE" in state["final"]["tasks"]


def test_foreground_visibility_refreshes_but_hidden_documents_do_not():
    state = run("foreground")
    assert len(state["requests"]) == 2
    assert "FOREGROUND-B" in state["tasks"]


def test_reopening_without_a_token_shows_the_form_and_denial_stops_polling():
    state = run("no_token", query="")
    assert state["requests"][0]["authorization"] == ""
    assert state["requests"][-1]["authorization"] == "Bearer typed-token"
    assert state["form_hidden"] is True
    assert state["input_value"] == ""
    assert all("token=" not in request["path"] for request in state["requests"])
    assert "NEW-SESSION" in state["tasks"]


def test_optional_scheduling_metrics_are_displayed_and_absent_means_untracked():
    state = run("scheduling")
    assert "排队 3.5 秒" in state["first"]["tasks"]
    assert "执行 12 秒" in state["first"]["tasks"]
    assert "执行结果 PASSED（非合并/验收）" in state["first"]["tasks"]
    assert "排队 未追踪" in state["final"]["tasks"]
    assert "3.5" not in state["final"]["tasks"].split("SCHED-B", 1)[-1]


def test_page_script_never_uses_browser_storage_cookies_or_url_bound_tokens():
    script = _script()
    for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB",
                   "location.search =", "location.assign", "location.replace",
                   "URLSearchParams(location.search).get('token')+"):
        assert banned not in script, banned
    assert "history.replaceState" in script
    assert "autocomplete=\"off\"" in page_html()
    assert 'type="password"' in page_html()
