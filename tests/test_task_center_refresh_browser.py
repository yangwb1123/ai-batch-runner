"""No previously rendered local task/goal/activity facts during a new GET."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pbatch.task_center_report import page_html

NODE = shutil.which("node")
FIXTURE = Path(__file__).parent / "fixtures" / "task_center_pending_browser.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node runtime required for delivered-script VM")
OLD = "OWNED-OLD-REFRESH-FACT"
NEW = "OWNED-NEW-REFRESH-FACT"


def _resources():
    return {"usage": {"total_tokens": None, "token_coverage": "unavailable", "calls": None},
            "billing": {"currency_totals": [], "financial_coverage": "unavailable",
                        "unknown_currency_observations": 0, "missing_amount_observations": 0,
                        "provider_requests": {"observed_total": None},
                        "assistant_usage_messages": {"observed_total": None}}}


def _view(marker):
    task = {"task_id": "tc-" + "a" * 32, "source_id": "src-" + "a" * 24,
            "project": "owned-project", "kind": "agent", "title": marker,
            "status": "running", "phase": "execution", "local_task_id": "owned-task",
            "analysis": {"flags": []}, "flow": {"last_observed_stage": None,
            "next_stage": None, "next_status": "untracked"},
            "time": {"elapsed_wall_seconds": None, "remaining_seconds": None},
            "forecast": {"duration_range_seconds": None}, "llm": [], "goal_links": [],
            "delivery": {"present_files": None, "declared_files": None}, **_resources()}
    goal = {"goal_id": "g-" + "a" * 32, "source_id": task["source_id"],
            "project": "owned-project", "statement": marker, "acceptance": "pending",
            "progress": {"matching_attestations": 0, "criteria": 1}, "flow": None,
            "consumption": _resources()}
    return {"schema_version": 1, "sources": [{"source_id": task["source_id"],
            "name": "owned-project", "coverage": []}], "tasks": [task], "goals": [goal],
            "intents": [], "repairs": [], "resources": [], "feedback": [],
            "activity": {"processes": [{"workspace": marker, "pid": 123,
                         "presence": "unknown_host", "registered_status": "running",
                         "heartbeat_age_seconds": None}], "diagnostics": []},
            "diagnostics": [], "observed_task_count": 1, "generated_at": marker}


def _browser(mode, select="", timer=False, markup=False):
    script = re.search(r"<script>(.*?)</script>", page_html(), re.S).group(1)
    new_marker = NEW + (' <img src=x><script>quoted()</script>' if markup else '')
    old, new = _view(OLD), _view(new_marker)
    # IDs deliberately stay stable so a complete new snapshot may retain selection,
    # without a body/history lookup or label-based association.
    result = subprocess.run([NODE, str(FIXTURE)], input=json.dumps({"script": script,
        "views": [old, new], "mode": mode, "select": select, "timer": timer,
        "old_marker": OLD, "new_marker": NEW}), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("select", ["", "tasks", "goals"])
@pytest.mark.parametrize("timer", [False, True])
def test_pending_get_clears_all_old_fact_rows_details_and_old_timestamp(select, timer):
    result = _browser("pending", select, timer)
    pending = result["pending"]
    assert OLD not in pending["all"]
    assert pending["tasks"] == pending["goals"] == pending["activity"] == ""
    assert len(pending["requests"]) == 2
    assert "等待" in pending["summary"]


@pytest.mark.parametrize("mode", ["complete", "failure", "late_success", "late_error"])
@pytest.mark.parametrize("select", ["tasks", "goals"])
def test_old_reply_or_error_cannot_restore_task_goal_activity_facts(mode, select):
    result = _browser(mode, select)
    assert OLD not in result["final"]["all"]
    if mode != "failure":
        detail = json.loads(result["final"]["detail"])
        assert detail["title" if select == "tasks" else "statement"] == NEW
    assert all(request["method"] == "GET" for request in result["final"]["requests"])


def test_refreshed_fact_labels_remain_literal_without_dom_or_followup_reads():
    result = _browser("complete", "tasks", markup=True)
    assert "<script>quoted()</script>" in result["final"]["activity"]
    assert result["final"]["tags"] == 0 and len(result["final"]["requests"]) == 2
