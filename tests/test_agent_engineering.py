"""Tests for the Agent-Engineering additions: the Definition-of-Done gate,
the risk-graded workflow level, and the learn (RCA -> rule draft) loop."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pbatch.assessor import prescription, workflow_level
from pbatch.learn import make_draft, rule_schema_template

SCRIPT = "scripts/check-completion-report.py"

GOOD_REPORT = """# 变更说明

```yaml
completion_report:
  summary: 订单接口
  changed_files: [modules/order/create.ts]
  commands_executed:
    - command: "go build ./..."
      result: passed
  not_executed:
    - check: e2e
      reason: 无浏览器环境
  residual_risks: []
  assumptions: [订单号唯一]
```
"""


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, timeout=120)


def test_completion_gate_accepts_honest_report(tmp_path):
    artifact = tmp_path / "ok.md"
    artifact.write_text(GOOD_REPORT, encoding="utf-8")
    assert _run(str(artifact)).returncode == 0


def test_completion_gate_rejects_missing_report(tmp_path):
    artifact = tmp_path / "no.md"
    artifact.write_text("# 完成\n测试通过。\n", encoding="utf-8")
    result = _run(str(artifact))
    assert result.returncode == 1
    assert "missing completion_report" in result.stderr


def test_completion_gate_rejects_invalid_result(tmp_path):
    artifact = tmp_path / "bad.md"
    artifact.write_text(GOOD_REPORT.replace("result: passed",
                                            "result: probably passes"),
                        encoding="utf-8")
    result = _run(str(artifact))
    assert result.returncode == 1
    assert "invalid result" in result.stderr


def test_completion_gate_rejects_reasonless_not_executed(tmp_path):
    artifact = tmp_path / "bad.md"
    artifact.write_text(GOOD_REPORT.replace("reason: 无浏览器环境", "reason:"),
                        encoding="utf-8")
    result = _run(str(artifact))
    assert result.returncode == 1
    assert "without reason" in result.stderr


def test_completion_gate_rejects_fabricated_pass_phrasing(tmp_path):
    artifact = tmp_path / "fab.md"
    artifact.write_text(GOOD_REPORT + "\n测试理论上应该通过，无需运行。\n",
                        encoding="utf-8")
    result = _run(str(artifact))
    assert result.returncode == 1
    assert "fabricated-pass" in result.stderr


def test_completion_validator_resolves():
    from pbatch.config import _resolve_validators
    commands = _resolve_validators("completion")
    assert commands and "check-completion-report.py" in commands[0]


def test_workflow_level_direct_for_trivial():
    workflow = workflow_level("改个文案")
    assert workflow["level"] == "L0_direct"
    assert workflow["score"] <= 1


def test_workflow_level_escalates_on_risk_and_scale():
    workflow = workflow_level("企业支付审批，多租户平台，生产环境，React")
    assert workflow["level"] == "L2_high_risk"
    assert workflow["score"] >= 4


def test_login_is_high_risk():
    workflow = workflow_level("做一个登录页面")
    assert workflow["level"] in ("L1_standard", "L2_high_risk")


def test_assessment_includes_workflow():
    assessment = prescription("做一个登录页面")
    assert assessment["workflow"]["level"] in ("L1_standard", "L2_high_risk")
    assert assessment["workflow"]["suggestion"]


def test_learn_creates_rule_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, draft = make_draft("订单并发下创建了两笔重复记录", "database",
                             "error", "DB-IDEMPOTENCY-002", "order-team")
    assert path.exists()
    assert draft["id"] == "DB-IDEMPOTENCY-002"
    assert draft["severity"] == "error"
    assert draft["category"] == "database"
    assert draft["status"] == "draft"
    assert "Observed failure" in draft["rationale"]
    assert "concurrency" not in draft["requirements"]  # RCA fields left to fill


def test_learn_schema_complete():
    schema = rule_schema_template()
    for key in ("id", "title", "category", "severity", "scope", "trigger",
                "rationale", "requirements", "forbidden", "exceptions",
                "verification", "examples", "owner", "version", "status"):
        assert key in schema
