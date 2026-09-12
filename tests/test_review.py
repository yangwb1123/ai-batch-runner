"""Local contract tests for the headless Codex review command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import review
from pbatch.user_dirs import project_runtime_dir


FINDING_FIELDS = {"severity", "file", "line", "problem", "evidence", "fix_hint"}


def _agent_message(text: str) -> dict:
    return {"type": "item.completed",
            "item": {"id": "message", "type": "agent_message", "text": text}}


def _file_change() -> dict:
    return {"type": "item.completed",
            "item": {"id": "change", "type": "file_change",
                     "changes": [{"path": "src/a.py", "kind": "update"}]}}


def _finding(severity: str = "low", file_name: str = "src/a.py") -> dict:
    return {"severity": severity, "file": file_name, "line": 7,
            "problem": "problem", "evidence": "evidence", "fix_hint": "fix it"}


def _runner(events: list[dict], exit_code: int = 0, stderr: str = ""):
    def run(argv, cwd, timeout, capture):
        run.command = list(argv)
        run.cwd = cwd
        run.timeout = timeout
        for event in events:
            capture.consume_jsonl(json.dumps(event))
        return review.RunOutcome(exit_code, 23, stderr=stderr)
    run.command = []
    run.cwd = ""
    run.timeout = 0.0
    return run


def _run_json(tmp_path: Path, events: list[dict], **runner_kwargs):
    output = tmp_path / "review.json"
    runner = _runner(events, **runner_kwargs)
    code = review.review_main(
        ["--uncommitted", "--json", "--cwd", str(tmp_path),
         "--output", output.name], runner=runner)
    return code, json.loads(output.read_text(encoding="utf-8")), runner


def test_event_fixture_extracts_all_findings_and_schema(tmp_path, capsys):
    first = """Review result.
```json
[
  {"severity":"CRITICAL","file":"src/a.py","line":12,"problem":"unsafe",
   "evidence":"call path","fix_hint":"validate"},
  {"severity":"high","file":"src/b.py","line":"","problem":"race",
   "fix_hint":"lock"},
  {"severity":"LOW","file":"tests/test_a.py","problem":"coverage",
   "evidence":"missing branch","fix_hint":"add test"}
]
```
"""
    events = [
        {"type": "turn/started"},
        _agent_message(first),
        _agent_message("No additional structured findings."),
        {"type": "turn/completed"},
    ]
    code, payload, runner = _run_json(tmp_path, events)
    assert code == 1
    assert [item["severity"] for item in payload["findings"]] == [
        "critical", "high", "low"]
    assert payload["findings"][1]["evidence"] == ""
    assert payload["invalid_findings"] == []
    assert payload["item_counts"] == {"agent_message": 2}
    assert all(set(item) == FINDING_FIELDS for item in payload["findings"])
    assert runner.command == ["codex", "exec", "review", "--json", "--uncommitted"]
    assert json.loads(capsys.readouterr().out)["report_path"] == str(tmp_path / "review.json")


def test_multiple_fences_bare_array_unknown_and_invalid_items():
    messages = [
        "```json\n[" + json.dumps(_finding("info", "a.py")) + "]\n```\n"
        "```json\n[" + json.dumps(_finding("urgent", "b.py")) + "]\n```",
        "fallback: [{\"severity\":\"low\",\"file\":\"c.py\"},"
        "{\"severity\":\"low\"},42] trailing text",
    ]
    findings, invalid = review.extract_findings(messages)
    assert [item["file"] for item in findings] == ["a.py", "b.py", "c.py"]
    assert findings[1]["severity"] == "medium"
    assert any("unknown severity 'urgent'" in item for item in invalid)
    assert any("missing file" in item for item in invalid)
    assert any("not an object" in item for item in invalid)


@pytest.mark.parametrize(
    "args",
    [
        ["--uncommitted", "--base", "main"],
        ["--base", "main", "--commit", "abc"],
        ["--commit", "abc", "--custom", "focus on auth"],
    ],
)
def test_scope_and_custom_are_mutually_exclusive(args):
    with pytest.raises(SystemExit) as exc:
        review.review_main(args)
    assert exc.value.code == 2


def test_custom_is_positional_codex_prompt(tmp_path):
    events = [_agent_message(json.dumps([_finding("low")]))]
    output = tmp_path / "custom.md"
    runner = _runner(events)
    code = review.review_main(
        ["--custom", "focus on auth", "--cwd", str(tmp_path),
         "--output", output.name], runner=runner)
    assert code == 0
    assert runner.command == ["codex", "exec", "review", "--json", "focus on auth"]
    assert "Scope: `custom`" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("events", "runner_kwargs", "expected"),
    [
        ([_agent_message(json.dumps([_finding("critical")]))], {}, 1),
        ([_agent_message("No JSON findings")], {}, 2),
        ([_agent_message(json.dumps([_finding("info")]))], {}, 0),
        ([_agent_message(json.dumps([_finding("low")]))], {"exit_code": 9}, 2),
        ([_agent_message(json.dumps([_finding("low")])), _file_change()], {}, 3),
    ],
)
def test_exit_code_mapping(tmp_path, events, runner_kwargs, expected):
    code, payload, _ = _run_json(tmp_path, events, **runner_kwargs)
    assert code == expected
    assert payload["flagged_writes"] is (expected == 3)


def test_file_change_fixture_sets_read_only_alarm(tmp_path):
    events = [_agent_message(json.dumps([_finding("low")])), _file_change()]
    code, payload, _ = _run_json(tmp_path, events)
    assert code == 3
    assert payload["item_counts"] == {"agent_message": 1, "file_change": 1}
    assert "read-only contract violation" in payload["summary"]


def test_timeout_kills_spawned_process_group(tmp_path, monkeypatch):
    processes = []

    def hanging_spawn(_argv, cwd):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.append(process)
        return process

    monkeypatch.setattr(review, "_spawn_process", hanging_spawn)
    output = tmp_path / "timeout.json"
    code = review.review_main(
        ["--uncommitted", "--json", "--cwd", str(tmp_path),
         "--output", output.name, "--timeout", "0.05"])
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 2
    assert processes[0].poll() is not None
    assert payload["codex_exit"] == -1
    assert "timed out" in payload["summary"]


def test_output_must_stay_within_cwd(tmp_path):
    with pytest.raises(SystemExit) as exc:
        review.review_main(
            ["--uncommitted", "--cwd", str(tmp_path), "--output", "../escape.md"])
    assert exc.value.code == 2
    assert not (tmp_path.parent / "escape.md").exists()


def test_default_json_report_path_is_inside_private_runtime(tmp_path):
    runner = _runner([_agent_message(json.dumps([_finding("low")]))])
    assert review.review_main(
        ["--uncommitted", "--json", "--cwd", str(tmp_path)], runner=runner) == 0
    reports = list((project_runtime_dir(str(tmp_path)) / "reviews").glob("review-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["report_path"] == str(reports[0])


def test_review_registration_drives_shell_completion():
    from pbatch.cli import _SUBCOMMANDS
    from pbatch.product_cli import _completion_script

    assert _SUBCOMMANDS["review"] == "review:review_main"
    assert " review " in _completion_script("bash")
