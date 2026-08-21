"""Tests for the Agent-Engineering additions: the Definition-of-Done gate,
the risk-graded workflow level, and the learn (RCA -> rule draft) loop."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch.assessor import prescription, workflow_level
from pbatch.learn import (failure_to_draft, make_draft, memory_hit_to_failure,
                          rule_schema_template)

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


def test_completion_gate_stdlib_fallback_without_pyyaml(tmp_path):
    """The documented pure-stdlib gate remains usable when PyYAML is absent."""
    artifact = tmp_path / "ok.md"
    artifact.write_text(GOOD_REPORT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-S", SCRIPT, str(artifact)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr


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
    assert workflow["level"] == "L3_platform"
    assert workflow["score"] >= 6


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


@pytest.mark.parametrize("rule_id", ["../escaped", "/tmp/escaped", "bad/id", " bad"])
def test_learn_rule_id_cannot_escape_draft_directory(
        tmp_path, monkeypatch, rule_id):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="rule id must match"):
        make_draft("failure", "backend", "error", rule_id)
    assert not (tmp_path / "escaped").exists()


def test_learn_schema_complete():
    schema = rule_schema_template()
    for key in ("id", "title", "category", "severity", "scope", "trigger",
                "rationale", "requirements", "forbidden", "exceptions",
                "verification", "examples", "owner", "version", "status"):
        assert key in schema


def test_learn_never_overwrites_same_day_draft(tmp_path, monkeypatch):
    import pbatch.learn as learn
    monkeypatch.setattr(learn, "DRAFT_DIR", tmp_path)
    first, _ = learn.make_draft("first failure", "backend", "error", "SAFE-1")
    second, _ = learn.make_draft("second failure", "backend", "error", "SAFE-1")
    assert first != second
    assert "first failure" in first.read_text(encoding="utf-8")
    assert "second failure" in second.read_text(encoding="utf-8")


def test_learn_rejects_symlink_draft_directory(tmp_path, monkeypatch):
    import pbatch.learn as learn
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "drafts"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(learn, "DRAFT_DIR", linked)
    with pytest.raises(ValueError, match="symlink"):
        learn.make_draft("failure", "backend", "error", "SAFE-2")


def test_learn_uses_one_bounded_failure_to_schema_conversion():
    failure = "  duplicate   order  " + "x" * 1200
    draft = failure_to_draft(failure, "database", "error", "DB-BOUNDED-001")
    assert draft["title"].startswith("duplicate order")
    observed = draft["rationale"].split(". Root-cause", 1)[0]
    assert len(observed.removeprefix("Observed failure: ")) == 1000
    with pytest.raises(ValueError, match="must not be empty"):
        failure_to_draft(" \n ", "database", "error", "DB-EMPTY-001")


def test_memory_hit_maps_public_memory_fields_to_incident():
    failure = memory_hit_to_failure("订单并发", {
        "session_id": "s1", "domains": ["backend", "database"],
        "stage": "gate", "status": "FAILED_OBSERVED",
        "observed_failure": "订单并发创建重复记录",
        "reason": "less-specific fallback",
    })
    assert failure.startswith("订单并发创建重复记录")
    assert "domains=backend,database" in failure
    assert "stage=gate" in failure
    assert "less-specific fallback" not in failure
    with pytest.raises(ValueError, match="no incident evidence"):
        memory_hit_to_failure("订单并发", {"status": "PASSED", "session_id": "ok"})
    with pytest.raises(ValueError, match="no incident evidence"):
        memory_hit_to_failure("订单并发", {
            "status": "PASSED", "reason": "all checks passed", "session_id": "ok"})


def test_assess_from_file(tmp_path, capsys):
    req = tmp_path / "req.md"
    req.write_text("做一个开源文件上传产品，断点续传，商业化收费", encoding="utf-8")
    sys.argv = ["pi-batch.py", "assess", "--file", str(req)]
    from pbatch.assessor import assess_main
    assess_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "产品化: L3_product_feature" in out


def test_rules_check_passes():
    sys.argv = ["pi-batch.py", "rules", "--check"]
    from pbatch.rule_cli import rules_main
    try:
        rules_main(sys.argv[2:])
        code = 0
    except SystemExit as exc:
        code = exc.code
    assert code == 0


def test_rules_check_fails_on_broken_registry(tmp_path, monkeypatch):
    broken = tmp_path / "rules.yaml"
    broken.write_text("rules:\n  bad-rule:\n    description: x\n",
                      encoding="utf-8")
    from pbatch.rule_cli import check_registry
    from pbatch.rule_matcher import load_registry
    violations = check_registry(load_registry(str(broken)))
    assert any("min_tier" in v for v in violations)
    assert any("files" in v for v in violations)


def test_learn_from_memory(tmp_path, monkeypatch, capsys):
    import pbatch.learn as learn
    import pbatch.memory as memory
    monkeypatch.chdir(tmp_path)
    hits = [
        {"session_id": "passed", "domains": ["backend"], "status": "PASSED"},
        {"session_id": "s1", "domains": ["backend"],
         "status": "FAILED_OBSERVED",
         "observed_failure": "订单并发创建重复记录"},
    ]
    monkeypatch.setattr(memory, "find", lambda q, **kw: hits)
    sys.argv = ["pi-batch.py", "learn", "--from-memory", "订单并发",
                "--category", "database", "--rule-id", "DB-002"]
    learn.learn_main(sys.argv[2:])
    drafts = list(Path(tmp_path).glob("docs/rules/drafts/*.yaml"))
    assert drafts
    assert "重复" in drafts[0].read_text(encoding="utf-8")


def test_check_subcommand_passes():
    from pbatch.selfcheck import check_main
    sys.argv = ["pi-batch.py", "check", "--skip-eval"]
    try:
        check_main(sys.argv[2:])
        code = 0
    except SystemExit as exc:
        code = exc.code
    assert code == 0


def test_assess_prompt_compiles_instruction_block(capsys):
    sys.argv = ["pi-batch.py", "assess", "--prompt",
                "写一个订单创建接口，含支付审批，生产环境，go 实现"]
    from pbatch.assessor import assess_main
    assess_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "Execution instructions" in out
    assert "REQUIREMENT:" in out
    assert "WORKFLOW:" in out
    assert "RULES (load ONLY these files):" in out
    assert "persistence-modeling" in out
    assert "COMPLETION:" in out
