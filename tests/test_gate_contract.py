"""Structured Gate contract and legacy verdict compatibility."""

from __future__ import annotations

import json

import pytest

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


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"evidence": ["   "]}, "evidence"),
        ({"evidence": ["---"]}, "evidence"),
        ({"validators": [{"name": "\t", "status": "PASS"}]},
         "validator name"),
    ],
)
def test_structured_pass_rejects_semantically_empty_proof(overrides,
                                                          reason_fragment):
    decision = parse_gate_text(_structured(**overrides))

    assert decision.valid is False
    assert decision.verdict is None
    assert reason_fragment in decision.reason


def test_malformed_structured_json_does_not_fallback_to_text_pass():
    text = "```json\n{\"schema_version\": 1, \"verdict\": \"PASS\"\n```\nVERDICT: PASS - fallback"
    decision = parse_gate_text(text)
    assert decision.valid is False
    assert decision.verdict is None


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ('"schema_version":1',
         '"schema_version":1,"schema_version":1'),
        ('"verdict":"PASS"',
         '"verdict":"FAIL","verdict":"PASS"'),
        ('"status":"PASS"',
         '"status":"FAIL","status":"PASS"'),
    ],
)
def test_structured_json_rejects_duplicate_object_keys(needle, replacement):
    body = (
        '{"schema_version":1,"verdict":"PASS",'
        '"reason":"all checks passed","evidence":["pytest"],'
        '"validators":[{"name":"pytest","status":"PASS"}]}'
    ).replace(needle, replacement)

    decision = parse_gate_text(f"```json\n{body}\n```")

    assert decision.valid is False
    assert decision.verdict is None
    assert "malformed structured Gate JSON" in decision.reason


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


@pytest.mark.parametrize("reason", ["-", "—", "**", " _ "])
def test_legacy_and_structured_reasons_require_substantive_text(reason):
    legacy = parse_gate_text(f"VERDICT: PASS {reason}")
    structured = parse_gate_text(_structured(reason=reason))

    assert legacy.valid is False and legacy.verdict is None
    assert structured.valid is False and structured.verdict is None


def test_bare_legacy_verdict_invalidates_other_verdicts_in_same_artifact():
    decision = parse_gate_text(
        "VERDICT: FAIL\nVERDICT: PASS - a later line must not unlock the gate")

    assert decision.valid is False
    assert decision.verdict is None
    assert decision.source == "legacy_text"
    assert "bare VERDICT" in decision.reason


def _codex_transcript(message: str, terminal: str = "turn.completed") -> str:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.completed", "item": {
            "id": "item-1", "type": "agent_message", "text": message,
        }},
        {"type": terminal, "usage": {"input_tokens": 10}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _codex_messages(*messages: str, terminal: str = "turn.completed") -> str:
    events = [{"type": "thread.started", "thread_id": "thread-1"}]
    events.extend({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": message},
    } for message in messages)
    events.append({"type": terminal, "usage": {}})
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_codex_jsonl_transcript_unwraps_legacy_and_structured_decisions():
    legacy = parse_gate_text(_codex_transcript(
        "VERDICT: FAIL - implementation evidence is missing"))
    assert legacy.valid is True
    assert legacy.verdict == "FAIL"
    assert legacy.source == "legacy_text"

    message = _structured(
        "REJECT", evidence=None, validators=None,
        findings=[{"severity": "high", "file": "src/a.py",
                   "problem": "unsafe branch", "fix_hint": "add guard"}],
    )
    structured = parse_gate_text(_codex_transcript(message))
    assert structured.valid is True
    assert structured.verdict == "REJECT"
    assert structured.source == "structured"
    assert extract_findings(_codex_transcript(message)) == structured.findings


def test_codex_jsonl_accepts_known_progress_event_types():
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "command_execution"}},
        {"type": "item.updated", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "VERDICT: PASS - checked",
        }},
        {"type": "turn.completed", "usage": {}},
    ]

    decision = parse_gate_text(
        "\n".join(json.dumps(event) for event in events) + "\n")

    assert decision.valid is True
    assert decision.verdict == "PASS"


def test_codex_jsonl_transcript_requires_clean_successful_terminal_event():
    missing = _codex_transcript("VERDICT: PASS - ready").splitlines()[:-1]
    decision = parse_gate_text("\n".join(missing))
    assert decision.valid is False and decision.verdict is None
    assert "no turn.completed" in decision.reason

    failed = parse_gate_text(_codex_transcript(
        "VERDICT: PASS - must not unlock", terminal="turn.failed"))
    assert failed.valid is False and failed.verdict is None
    assert "turn.failed" in failed.reason


def test_codex_jsonl_uses_only_final_agent_message_for_gate_decision():
    early_pass = _structured("PASS")
    decision = parse_gate_text(_codex_messages(
        early_pass, "VERDICT: FAIL - final verification found a regression"))

    assert decision.valid is True
    assert decision.verdict == "FAIL"
    assert decision.source == "legacy_text"

    final_pass = parse_gate_text(_codex_messages(
        "VERDICT: FAIL - preliminary review",
        _structured("PASS")))
    assert final_pass.valid is True
    assert final_pass.verdict == "PASS"
    assert final_pass.source == "structured"


@pytest.mark.parametrize("final_message", ["", "   ", None])
def test_codex_jsonl_empty_final_agent_message_invalidates_earlier_pass(
        final_message):
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "VERDICT: PASS - preliminary result",
        }},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": final_message,
        }},
        {"type": "turn.completed", "usage": {}},
    ]

    decision = parse_gate_text(
        "\n".join(json.dumps(event) for event in events) + "\n")

    assert decision.valid is False
    assert decision.verdict is None
    assert "final agent message is empty or non-string" in decision.reason


def test_codex_jsonl_rejects_events_after_terminal_event():
    transcript = _codex_transcript("VERDICT: PASS - ready")
    transcript += json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "late PASS"},
    })

    decision = parse_gate_text(transcript)

    assert decision.valid is False
    assert decision.verdict is None
    assert "after terminal" in decision.reason


@pytest.mark.parametrize(
    ("message_event", "terminal_event"),
    [
        (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"VERDICT: PASS - checked"}}',
            '{"type":"turn.failed","type":"turn.completed","usage":{}}',
        ),
        (
            '{"type":"item.completed","item":{"type":"tool_call",'
            '"type":"agent_message","text":"VERDICT: PASS - checked"}}',
            '{"type":"turn.completed","usage":{}}',
        ),
    ],
)
def test_codex_jsonl_rejects_duplicate_event_object_keys(
        message_event, terminal_event):
    transcript = "\n".join([
        '{"type":"thread.started","thread_id":"thread-1"}',
        message_event,
        terminal_event,
    ])

    decision = parse_gate_text(transcript)

    assert decision.valid is False
    assert decision.verdict is None
    assert "malformed Gate JSONL transcript" in decision.reason


def test_duplicate_jsonl_event_never_falls_back_to_text_pass():
    transcript = (
        '{"type":"turn.failed","type":"turn.completed"}\n'
        'VERDICT: PASS - transport text is not a Gate decision'
    )

    decision = parse_gate_text(transcript)

    assert decision.valid is False
    assert decision.verdict is None
    assert decision.source == "transcript"
    assert "malformed Gate JSONL transcript" in decision.reason


@pytest.mark.parametrize(
    "invalid_event",
    [
        {},
        {"type": ""},
        {"type": "   "},
        {"type": 7},
        {"type": "unknown.event"},
        {"type": "item.unknown"},
        [],
        "not an event object",
    ],
)
def test_codex_jsonl_rejects_missing_unknown_or_non_object_event(invalid_event):
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        invalid_event,
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "VERDICT: PASS - checked",
        }},
        {"type": "turn.completed", "usage": {}},
    ]

    decision = parse_gate_text(
        "\n".join(json.dumps(event) for event in events) + "\n")

    assert decision.valid is False
    assert decision.verdict is None
    assert decision.source == "transcript"
    assert "malformed Gate JSONL transcript" in decision.reason


@pytest.mark.parametrize(
    "trailing_line",
    [
        json.dumps({"verdict": "FAIL"}),
        json.dumps({"type": "unknown.event"}),
        json.dumps({"type": ""}),
        json.dumps([]),
        "trailing transport text",
    ],
)
def test_codex_jsonl_rejects_any_nonempty_line_after_terminal(trailing_line):
    transcript = _codex_transcript("VERDICT: PASS - checked") + trailing_line

    decision = parse_gate_text(transcript)

    assert decision.valid is False
    assert decision.verdict is None
    assert decision.source == "transcript"
    assert "after terminal" in decision.reason


def test_codex_jsonl_untyped_failure_cannot_be_hidden_by_pass_message():
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"error": "provider failed without a typed envelope"},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "VERDICT: PASS - accepted",
        }},
        {"type": "turn.completed", "usage": {}},
        {"verdict": "FAIL"},
    ]

    decision = parse_gate_text(
        "\n".join(json.dumps(event) for event in events) + "\n")

    assert decision.valid is False
    assert decision.verdict is None
    assert decision.source == "transcript"
    assert "malformed Gate JSONL transcript" in decision.reason


def test_artifact_aggregation_is_fail_closed_and_fail_wins(tmp_path):
    passed = tmp_path / "pass.md"
    failed = tmp_path / "fail.md"
    passed.write_text(_structured(), encoding="utf-8")
    failed.write_text(_structured("REJECT", evidence=None, validators=None),
                      encoding="utf-8")
    decision = evaluate_gate_artifacts([str(passed), str(failed)])
    assert decision.verdict == "REJECT"
    assert _gate_verdict([str(passed)]) == "PASS"


def test_bare_legacy_artifact_invalidates_pass_from_another_artifact(tmp_path):
    bare = tmp_path / "bare.md"
    passed = tmp_path / "pass.md"
    bare.write_text("VERDICT: FAIL\n", encoding="utf-8")
    passed.write_text("VERDICT: PASS - checked\n", encoding="utf-8")

    decision = evaluate_gate_artifacts([str(bare), str(passed)])

    assert decision.valid is False
    assert decision.verdict is None
    assert decision.source == "invalid"
    assert "bare VERDICT" in decision.reason


def test_missing_or_non_verdict_artifact_invalidates_other_pass(tmp_path):
    passed = tmp_path / "pass.md"
    plain = tmp_path / "plain.md"
    missing = tmp_path / "missing.md"
    passed.write_text("VERDICT: PASS - checked\n", encoding="utf-8")
    plain.write_text("supplemental text only\n", encoding="utf-8")

    no_verdict = evaluate_gate_artifacts([str(plain), str(passed)])
    no_file = evaluate_gate_artifacts([str(missing), str(passed)])

    assert no_verdict.valid is False and no_verdict.verdict is None
    assert "no usable Gate verdict" in no_verdict.reason
    assert no_file.valid is False and no_file.verdict is None
    assert "artifact is missing" in no_file.reason

    malformed = tmp_path / "malformed.md"
    malformed.write_text(
        "```json\n{\"schema_version\": 1, \"verdict\": \"PASS\"\n```\n"
        "VERDICT: PASS - should not be accepted", encoding="utf-8")
    assert evaluate_gate_artifacts([str(malformed)]).verdict is None


def test_symlink_gate_artifact_is_rejected_even_when_target_passes(tmp_path):
    target = tmp_path / "target.md"
    artifact = tmp_path / "gate.md"
    target.write_text("VERDICT: PASS - checked\n", encoding="utf-8")
    artifact.symlink_to(target)

    decision = evaluate_gate_artifacts([str(artifact)])

    assert decision.valid is False
    assert decision.verdict is None
    assert "symlinks are not allowed" in decision.reason
