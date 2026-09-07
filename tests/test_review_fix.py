"""Injection-only contracts for structured review remediation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import review, review_fix
from pbatch.process_exec import ValidationResult


def _finding(severity: str, file_name: str, problem: str) -> dict:
    return {
        "severity": severity, "file": file_name, "line": 7,
        "problem": problem, "evidence": f"evidence for {problem}",
        "fix_hint": f"fix {problem}",
    }


def _review_runner(findings: list[dict], *, exit_code: int = 0,
                   timed_out: bool = False):
    def run(argv, cwd, timeout, capture):
        run.calls.append((list(argv), cwd, timeout))
        capture.consume_jsonl(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(findings)},
        }))
        return review.RunOutcome(
            -1 if timed_out else exit_code, 12, timed_out=timed_out,
            stderr="review failed" if exit_code or timed_out else "")
    run.calls = []
    return run


def _outcome(*, ok: bool = True, exit_code: int = 0, stdout: str = "{}\n",
             stderr: str = "", timed_out: bool = False) -> ValidationResult:
    return ValidationResult(ok, exit_code, stdout, stderr, timed_out)


class FixRunner:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [_outcome()])
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append((list(argv), cwd, timeout))
        return self.outcomes.pop(0) if self.outcomes else _outcome()


def _run(tmp_path: Path, findings: list[dict], fix: FixRunner | None = None,
         extra: list[str] | None = None, **kwargs):
    output = tmp_path / "report.json"
    code = review_fix.review_fix_main(
        ["--uncommitted", "--json", "--cwd", str(tmp_path),
         "--output", output.name, *(extra or [])],
        review_runner=_review_runner(findings), fix_runner=fix or FixRunner(),
        status_reader=lambda _cwd: (), **kwargs)
    return code, json.loads(output.read_text(encoding="utf-8"))


def test_groups_two_files_into_serial_tasks_and_reuses_gate_prompt(tmp_path):
    findings = [
        _finding("high", "src/a.py", "A-one"),
        _finding("high", "src/a.py", "A-two"),
        _finding("low", "src/b.py", "B-one"),
    ]
    fixer = FixRunner([_outcome(), _outcome()])

    code, payload = _run(tmp_path, findings, fixer)

    assert code == 0
    assert [item["file"] for item in payload["files"]] == ["src/a.py", "src/b.py"]
    assert [len(item["findings"]) for item in payload["files"]] == [2, 1]
    assert len(fixer.calls) == 2
    assert fixer.calls[0][0][:2] == ["codex", "exec"]
    assert "--approve-for-me" in fixer.calls[0][0]
    assert "--json" in fixer.calls[0][0]
    prompts = [" ".join(call[0]) for call in fixer.calls]
    assert "A-one" in prompts[0] and "A-two" in prompts[0] and "B-one" not in prompts[0]
    assert "B-one" in prompts[1]
    assert all('"severity": "' in prompt for prompt in prompts)
    assert all("Do NOT commit" in prompt for prompt in prompts)
    assert all("line numbers are hints" in prompt for prompt in prompts)
    assert payload["summary"] == {
        "findings": 3, "files": 2, "fixed": 2, "failed": 0,
        "critical_high_failed": 0,
    }


@pytest.mark.parametrize(
    ("outcome", "status", "expected"),
    [
        (_outcome(), "FIXED", 0),
        (_outcome(ok=False, exit_code=9, stdout="", stderr="boom"), "FAILED", 1),
        (_outcome(ok=False, exit_code=-1, stdout="", timed_out=True), "TIMED_OUT", 1),
        (_outcome(stdout="   "), "NO_OUTPUT", 1),
    ],
)
def test_fix_outcome_states_and_high_severity_exit_mapping(
        tmp_path, outcome, status, expected):
    fixer = FixRunner([outcome])
    code, payload = _run(
        tmp_path, [_finding("high", "src/a.py", "problem")], fixer)
    result = payload["files"][0]["fix_result"]
    assert code == expected
    assert result["status"] == status
    assert result["attempt_count"] == 1
    assert result["attempts"][0]["timed_out"] is outcome.timed_out
    assert payload["summary"]["fixed"] == (status == "FIXED")


def test_low_failure_is_reported_but_non_blocking(tmp_path):
    fixer = FixRunner([_outcome(ok=False, exit_code=4, stdout="", stderr="failed")])
    code, payload = _run(
        tmp_path, [_finding("low", "src/b.py", "minor")], fixer)
    assert code == 0
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["critical_high_failed"] == 0


def test_validation_failure_retries_fix_once_then_passes(tmp_path):
    fixer = FixRunner([_outcome(stdout="first\n"), _outcome(stdout="second\n")])
    validation_calls = []
    validations = [
        _outcome(ok=False, exit_code=1, stdout="", stderr="red"),
        _outcome(stdout="green\n"),
    ]

    def validator(command, cwd, timeout):
        validation_calls.append((command, cwd, timeout))
        return validations.pop(0)

    code, payload = _run(
        tmp_path, [_finding("high", "src/a.py", "retry")], fixer,
        extra=["--validate", "check {files} --root {cwd}"],
        validate_runner=validator)

    item = payload["files"][0]
    assert code == 0
    assert len(fixer.calls) == 2
    assert item["fix_result"]["status"] == "FIXED"
    assert item["fix_result"]["attempt_count"] == 2
    assert item["validation"]["status"] == "PASSED"
    assert item["validation"]["attempt_count"] == 2
    assert validation_calls[0][0] == f"check src/a.py --root {tmp_path}"
    assert "VALIDATION RETRY FEEDBACK" in " ".join(fixer.calls[1][0])
    assert "red" in " ".join(fixer.calls[1][0])


def test_two_validation_failures_end_failed_validation(tmp_path):
    fixer = FixRunner([_outcome(), _outcome()])

    def validator(_command, _cwd, _timeout):
        return _outcome(ok=False, exit_code=2, stdout="", stderr="still red")

    code, payload = _run(
        tmp_path, [_finding("critical", "a.py", "gate")], fixer,
        extra=["--validate", "false"], validate_runner=validator)
    item = payload["files"][0]
    assert code == 1
    assert item["fix_result"]["status"] == "FAILED_VALIDATION"
    assert item["validation"]["status"] == "FAILED"
    assert item["validation"]["attempt_count"] == 2


def test_clean_review_writes_clean_report_without_fix_turn(tmp_path):
    output = tmp_path / "clean.json"
    fixer = FixRunner()
    code = review_fix.review_fix_main(
        ["--uncommitted", "--json", "--cwd", str(tmp_path),
         "--output", output.name],
        review_runner=_review_runner([]), fix_runner=fixer,
        status_reader=lambda _cwd: ())
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["status"] == "clean"
    assert payload["files"] == []
    assert fixer.calls == []


def test_review_failure_exits_two_with_summary(tmp_path):
    output = tmp_path / "failed.json"
    code = review_fix.review_fix_main(
        ["--commit", "abc", "--json", "--cwd", str(tmp_path),
         "--output", output.name],
        review_runner=_review_runner([], exit_code=7),
        fix_runner=FixRunner(), status_reader=lambda _cwd: ())
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 2
    assert payload["status"] == "review_failed"
    assert payload["review"]["codex_exit"] == 7
    assert "Codex exited 7" in payload["review"]["summary"]


def test_json_report_schema_custom_scope_and_detected_changes(tmp_path):
    states = iter([set(), {"src/a.py", "tests/test_a.py"}])
    output = tmp_path / "schema.json"
    fixer = FixRunner()
    code = review_fix.review_fix_main(
        ["--base", "main", "--custom", "preserve the API", "--json",
         "--cwd", str(tmp_path), "--output", output.name],
        review_runner=_review_runner([_finding("medium", "src/a.py", "schema")]),
        fix_runner=fixer, status_reader=lambda _cwd: next(states))
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert set(payload) == {
        "scope", "status", "review", "files", "detected_changed_files",
        "summary", "exit_code", "report_path",
    }
    assert set(payload["files"][0]) == {"file", "findings", "fix_result", "validation"}
    assert payload["scope"] == "base:main"
    assert payload["detected_changed_files"] == ["src/a.py", "tests/test_a.py"]
    assert "preserve the API" in " ".join(fixer.calls[0][0])


def test_output_escape_fails_closed(tmp_path):
    with pytest.raises(SystemExit) as exc:
        review_fix.review_fix_main(
            ["--uncommitted", "--cwd", str(tmp_path), "--output", "../escape.md"])
    assert exc.value.code == 2
    assert not (tmp_path.parent / "escape.md").exists()


def test_cli_registration_drives_completion():
    from pbatch.cli import _SUBCOMMANDS
    from pbatch.product_cli import _completion_script

    assert _SUBCOMMANDS["review-fix"] == "review_fix:review_fix_main"
    assert " review-fix " in _completion_script("bash")
