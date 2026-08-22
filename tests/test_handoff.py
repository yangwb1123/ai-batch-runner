from __future__ import annotations

import json

import pytest

from pbatch import agent_ledger, config
from pbatch.handoff import handoff_main


def _record(data_dir, *, agent, run_id, session="", status="succeeded",
            outputs=(), ts="2026-08-22T01:00:00Z"):
    assert agent_ledger.record(
        data_dir, agent=agent, run_id=run_id, session_name=session,
        status=status, task_brief="recorded task", outputs=outputs, ts=ts)


def _agent(tmp_path, capture, exit_code=0):
    path = tmp_path / f"agent-{exit_code}.sh"
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-p\" ]; then\n"
        "  printf '%s' \"$2\" > \"$HANDOFF_CAPTURE\"\n"
        "fi\n"
        "printf 'target result\\n'\n"
        f"exit {exit_code}\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def ledger_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "MEMORY_MODE", "off")
    source = tmp_path / "source.md"
    source.write_text("source evidence: keep this fact\n", encoding="utf-8")
    _record(data_dir, agent="A", run_id="run-a-001", session="session-a",
            outputs=[source], ts="2026-08-22T01:00:00Z")
    _record(data_dir, agent="A", run_id="run-a-failed", session="session-f",
            status="failed", ts="2026-08-22T02:00:00Z")
    _record(data_dir, agent="B", run_id="run-b-001", session="session-b",
            ts="2026-08-22T03:00:00Z")
    return tmp_path, data_dir, source


def _run_args(agent, selector, source, output=""):
    args = ["continue the task", selector, source, "--agent", str(agent), "--no-lock"]
    if output:
        args.extend(["--output", str(output)])
    return args


def test_from_agent_injects_fenced_evidence_and_records_target(
        ledger_workspace, monkeypatch):
    root, data_dir, source = ledger_workspace
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture)
    output = root / "target.md"

    assert handoff_main(_run_args(agent, "--from-agent", "A", output)) == 0
    prompt = capture.read_text(encoding="utf-8")
    assert str(source) in prompt
    assert '<evidence source="source">' in prompt
    assert "source evidence: keep this fact" in prompt
    assert output.read_text(encoding="utf-8") == "target result\n"
    rows = agent_ledger.query(data_dir, agent=str(agent), limit=10)
    assert rows[-1]["status"] == "succeeded"
    assert rows[-1]["outputs"] == [output.name]


def test_from_run_unique_prefix_and_ambiguous_prefix(
        ledger_workspace, monkeypatch, capsys):
    root, data_dir, _ = ledger_workspace
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture)
    _record(data_dir, agent="A", run_id="unique-run-002", session="session-a2",
            outputs=[root / "source.md"], ts="2026-08-22T04:00:00Z")

    assert handoff_main(_run_args(agent, "--from-run", "unique-run")) == 0
    assert handoff_main(_run_args(agent, "--from-run", "run-a-")) == 2
    assert "ambiguous" in capsys.readouterr().err


def test_from_session_matches_session_id(ledger_workspace, monkeypatch):
    root, _, _ = ledger_workspace
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture)

    assert handoff_main(_run_args(agent, "--from-session", "session-a")) == 0
    assert "source evidence" in capture.read_text(encoding="utf-8")


def test_missing_outputs_and_all_unreadable_sources_fail_closed(
        ledger_workspace, monkeypatch, capsys):
    root, data_dir, _ = ledger_workspace
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture)
    _record(data_dir, agent="C", run_id="run-c-001", session="session-c",
            outputs=[root / "does-not-exist.md"], ts="2026-08-22T05:00:00Z")

    assert handoff_main(_run_args(agent, "--from-agent", "B")) == 2
    assert "no outputs" in capsys.readouterr().err
    assert handoff_main(_run_args(agent, "--from-agent", "C")) == 2
    assert "missing or unreadable" in capsys.readouterr().err


def test_source_selectors_are_mutually_exclusive(ledger_workspace, capsys):
    args = ["task", "--from-agent", "A", "--from-run", "run-a-001"]
    assert handoff_main(args) == 2
    assert "not allowed with" in capsys.readouterr().err


def test_sources_and_max_bytes_bound_the_injected_evidence(
        ledger_workspace, monkeypatch):
    root, data_dir, _ = ledger_workspace
    first, second = root / "first.md", root / "second.md"
    first.write_text("abcdefghij", encoding="utf-8")
    second.write_text("klmnopqrst", encoding="utf-8")
    _record(data_dir, agent="many", run_id="run-many", session="many",
            outputs=[first, second], ts="2026-08-22T06:00:00Z")
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture)
    args = ["task", "--from-agent", "many", "--agent", str(agent),
            "--no-lock", "--sources", "1", "--max-bytes", "4"]

    assert handoff_main(args) == 0
    prompt = capture.read_text(encoding="utf-8")
    assert prompt.count("<evidence source=") == 1
    assert "[evidence truncated:" in prompt
    assert "omitted 1 source candidates" in prompt


def test_agent_failure_maps_to_one_and_does_not_save(
        ledger_workspace, monkeypatch, capsys):
    root, _, _ = ledger_workspace
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture, exit_code=1)
    output = root / "failed.md"

    assert handoff_main(_run_args(agent, "--from-agent", "A", output)) == 1
    assert not output.exists()
    assert "agent exited 1" in capsys.readouterr().err


def test_prompt_budget_is_checked_before_agent_start(
        ledger_workspace, monkeypatch, capsys):
    root, _, _ = ledger_workspace
    capture = root / "prompt.txt"
    monkeypatch.setenv("HANDOFF_CAPTURE", str(capture))
    agent = _agent(root, capture)
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 16)

    assert handoff_main(_run_args(agent, "--from-agent", "A")) == 2
    assert "prompt exceeds" in capsys.readouterr().err
    assert not capture.exists()


def test_ledger_fixture_is_json_lines(ledger_workspace):
    _, data_dir, _ = ledger_workspace
    lines = (data_dir / "agents" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert all(isinstance(json.loads(line), dict) for line in lines)
