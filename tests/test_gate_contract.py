"""Structured Gate contract and legacy verdict compatibility."""

from __future__ import annotations

import json

from pbatch.gate_contract import (evaluate_gate_artifacts, extract_findings,
                                  parse_gate_text)
from pbatch.gate_fix import extract_gate_findings
from pbatch.pipeline import _gate_verdict


def _structured(verdict="PASS", **overrides):
    value = {
        "schema_version": 1,
        "verdict": verdict,
        "reason": "all required checks completed",
        "evidence": ["tests/test_gate_contract.py"],
        "validators": [{"name": "pytest", "status": "PASS",
                         "detail": "contract tests passed"}],
        "findings": [],
    }
    value.update(overrides)
    for key in ("evidence", "validators"):
        if value.get(key) is None:
            value.pop(key, None)
    return "```json\n" + json.dumps(value) + "\n```"


def test_structured_pass_requires_evidence_and_validator_results():
    decision = parse_gate_text(_structured())
    assert decision.valid is True
    assert decision.verdict == "PASS"
    assert decision.source == "structured"
    assert decision.evidence == ["tests/test_gate_contract.py"]
    assert decision.validators[0]["status"] == "PASS"

    missing = parse_gate_text(_structured(evidence=[]))
    assert missing.valid is False
    assert missing.verdict is None


def test_malformed_structured_json_does_not_fallback_to_text_pass():
    text = "```json\n{\"schema_version\": 1, \"verdict\": \"PASS\"\n```\nVERDICT: PASS - fallback"
    decision = parse_gate_text(text)
    assert decision.valid is False
    assert decision.verdict is None


def test_single_artifact_rejects_multiple_structured_decisions(tmp_path):
    pass_block = _structured()
    fail_block = _structured(
        "FAIL", evidence=None, validators=None, reason="blocking issue remains")
    for name, text in (("pass-then-fail", pass_block + "\n" + fail_block),
                       ("fail-then-pass", fail_block + "\n" + pass_block)):
        artifact = tmp_path / f"{name}.md"
        artifact.write_text(text, encoding="utf-8")
        decision = evaluate_gate_artifacts([str(artifact)])
        assert decision.valid is False
        assert decision.verdict is None
        assert "multiple structured Gate contracts" in decision.reason


def test_structured_pass_does_not_hide_later_malformed_json(tmp_path):
    artifact = tmp_path / "pass-then-malformed.md"
    artifact.write_text(
        _structured()
        + '\n```json\n{"schema_version": 1, "verdict": "FAIL"\n```',
        encoding="utf-8")
    decision = evaluate_gate_artifacts([str(artifact)])
    assert decision.valid is False
    assert decision.verdict is None
    assert "malformed structured Gate JSON" in decision.reason


def test_structured_fail_and_findings_feed_gate_fix():
    text = _structured(
        "FAIL", evidence=None, validators=None,
        reason="blocking issue remains",
        findings=[{"severity": "high", "file": "src/a.py", "line": 7,
                    "problem": "unsafe branch", "fix_hint": "add guard"}],
    )
    decision = parse_gate_text(text)
    assert decision.valid is True
    assert decision.verdict == "FAIL"
    assert decision.findings[0]["file"] == "src/a.py"
    assert extract_findings(text) == decision.findings
    assert extract_gate_findings(text) == decision.findings


def test_legacy_verdict_remains_supported():
    decision = parse_gate_text("VERDICT: PASS - legacy pipeline is ready")
    assert decision.valid is True
    assert decision.verdict == "PASS"
    assert decision.source == "legacy_text"


def test_artifact_aggregation_is_fail_closed_and_fail_wins(tmp_path):
    passed = tmp_path / "pass.md"
    failed = tmp_path / "fail.md"
    passed.write_text(_structured(), encoding="utf-8")
    failed.write_text(_structured("REJECT", evidence=None, validators=None),
                      encoding="utf-8")
    decision = evaluate_gate_artifacts([str(passed), str(failed)])
    assert decision.verdict == "REJECT"
    assert _gate_verdict([str(passed)]) == "PASS"

    malformed = tmp_path / "malformed.md"
    malformed.write_text(
        "```json\n{\"schema_version\": 1, \"verdict\": \"PASS\"\n```\n"
        "VERDICT: PASS - should not be accepted", encoding="utf-8")
    assert evaluate_gate_artifacts([str(malformed)]).verdict is None
