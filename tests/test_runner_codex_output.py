"""Codex JSONL projection at the task-output trust boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pbatch import config, runner
from pbatch.models import Task


@pytest.fixture
def codex_json(monkeypatch):
    agent = "codex-json"
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {agent: {
        "argument_flags": {"prompt": ["exec"]},
        "capture_thread_id": True,
        "output_protocol": "codex-jsonl",
        "session_flags": {"start": ["exec", "{prompt}"]},
        "extra_args": ["--json"],
    }})
    monkeypatch.setattr(runner, "_pb_register_ok", lambda provider="": None)
    monkeypatch.setattr(
        runner, "_pb_register_fail", lambda reason, provider="": None)
    return agent


def _event(value) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


def _transcript(message: str, command_output: str = "") -> str:
    events = [
        {"type": "thread.started", "thread_id": "01abc-session"},
        {"type": "turn.started"},
    ]
    if command_output:
        events.append({"type": "item.completed", "item": {
            "type": "command_execution",
            "aggregated_output": command_output,
        }})
    events.extend([
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "intermediate"}},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {}},
    ])
    return "".join(_event(value) for value in events)


def _result(task: Task, stdout: str):
    return runner._result_from_proc(
        task, SimpleNamespace(returncode=0), [stdout], [], 0.01)


def test_codex_jsonl_writes_only_final_agent_message(
        tmp_path, codex_json):
    output = tmp_path / "answer.md"
    task = Task("work", output=str(output), agent=codex_json,
                cwd=str(tmp_path))
    raw = _transcript("# Final\n\nOnly this answer.\n",
                      command_output="x" * 20_000)
    chunks = [raw[index:index + 4096] for index in range(0, len(raw), 4096)]

    result = runner._result_from_proc(
        task, SimpleNamespace(returncode=0), chunks, [], 0.01)
    runner.save_result(task, result)

    assert result.success is True
    assert result.stdout == "# Final\n\nOnly this answer.\n"
    assert output.read_text(encoding="utf-8") == result.stdout
    assert "thread.started" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("body", [
    "# Ordinary Markdown\n\nKeep every line.\n",
    json.dumps({"type": "report", "status": "complete"}) + "\n",
])
def test_plain_agent_output_is_unchanged(tmp_path, body):
    task = Task("work", output=str(tmp_path / "answer.md"),
                agent="plain-agent", cwd=str(tmp_path))

    result = _result(task, body)
    runner.save_result(task, result)

    assert result.success is True
    assert result.stdout == body
    assert task.output_path().read_text(encoding="utf-8") == body


@pytest.mark.parametrize("body", [
    "# Not JSONL\n",
    json.dumps({"type": "report", "status": "complete"}) + "\n",
])
def test_declared_codex_protocol_rejects_non_transcript_output(
        tmp_path, codex_json, body):
    output = tmp_path / "answer.md"
    task = Task("work", output=str(output), agent=codex_json,
                cwd=str(tmp_path))

    result = _result(task, body)
    runner.save_result(task, result)

    assert result.success is False
    assert result.stdout == body
    assert not output.exists()


def test_other_agent_does_not_project_codex_shaped_jsonl(
        tmp_path, codex_json):
    del codex_json
    body = _transcript("answer")
    task = Task("work", output=str(tmp_path / "answer.md"),
                agent="plain-agent", cwd=str(tmp_path))

    result = _result(task, body)
    runner.save_result(task, result)

    assert result.success is True
    assert result.stdout == body
    assert task.output_path().read_text(encoding="utf-8") == body


@pytest.mark.parametrize(("body", "reason"), [
    (_event({"type": "thread.started", "thread_id": "01abc"})
     + "not-json\n"
     + _event({"type": "turn.completed"}), "malformed Codex JSONL"),
    (_event({"type": "thread.started", "thread_id": "01abc"})
     + _event({"type": "turn.started"})
     + _event({"type": "turn.completed"}), "no final agent message"),
])
def test_damaged_codex_transcript_is_rejected_without_artifact(
        tmp_path, codex_json, body, reason):
    output = tmp_path / "answer.md"
    task = Task("work", output=str(output), agent=codex_json,
                cwd=str(tmp_path))

    result = _result(task, body)
    runner.save_result(task, result)

    assert result.success is False
    assert reason in result.reason
    assert result.stdout == body
    assert not output.exists()


@pytest.mark.parametrize("body", [
    _event({"type": "thread.started", "thread_id": "01abc"}),
    (_event({"type": "thread.started", "thread_id": "01abc"})
     + _event({"type": "provider.future_event"})),
])
def test_declared_codex_singleton_or_unknown_incomplete_stream_is_rejected(
        tmp_path, codex_json, body):
    output = tmp_path / "answer.md"
    task = Task("work", output=str(output), agent=codex_json,
                cwd=str(tmp_path))

    result = _result(task, body)
    runner.save_result(task, result)

    assert result.success is False
    assert "no turn.completed" in result.reason
    assert result.stdout == body
    assert not output.exists()


def test_explicit_protocol_does_not_require_thread_capture(
        tmp_path, codex_json, monkeypatch):
    section = dict(config._AGENTS_CFG[codex_json])
    section["capture_thread_id"] = False
    monkeypatch.setattr(config, "_AGENTS_CFG", {codex_json: section})
    task = Task("work", agent=codex_json, cwd=str(tmp_path))

    result = _result(task, _transcript("projected"))

    assert result.success is True
    assert result.stdout == "projected"


def test_legacy_codex_json_config_still_infers_protocol(monkeypatch):
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {"codex": {
        "extra_args": ["--json"],
    }})

    assert config.agent_output_protocol("/usr/local/bin/codex") == "codex-jsonl"


def test_invalid_codex_thread_id_is_rejected(tmp_path, codex_json):
    body = (_event({"type": "thread.started", "thread_id": "not-a-thread"})
            + _event({"type": "item.completed", "item": {
                "type": "agent_message", "text": "must not escape"}})
            + _event({"type": "turn.completed"}))
    task = Task("work", output=str(tmp_path / "answer.md"),
                agent=codex_json, cwd=str(tmp_path))

    result = _result(task, body)
    runner.save_result(task, result)

    assert result.success is False
    assert "valid thread.started" in result.reason
    assert not task.output_path().exists()


@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_codex_jsonl_lf_or_crlf_preserves_unicode_line_separators(
        tmp_path, codex_json, separator):
    message = "left\u2028middle\u2029right"
    body = _transcript(message).replace("\n", separator)
    task = Task("work", agent=codex_json, cwd=str(tmp_path))

    result = _result(task, body)

    assert result.success is True
    assert result.stdout == message


@pytest.mark.parametrize("message", [
    "ERROR: this is the requested report",
    "fatal: a quoted example, not a CLI failure",
])
def test_structured_final_message_bypasses_plain_stdout_failure_signatures(
        tmp_path, codex_json, monkeypatch, message):
    failures = []
    monkeypatch.setattr(
        runner, "_pb_register_fail",
        lambda reason, provider="": failures.append(reason))
    task = Task("work", agent=codex_json, cwd=str(tmp_path))

    result = _result(task, _transcript(message))

    assert result.success is True
    assert result.stdout == message
    assert failures == []


def test_full_streaming_hides_raw_codex_jsonl_and_prints_projection_once(
        tmp_path, codex_json, monkeypatch, capsys):
    agent = tmp_path / codex_json
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "events = [\n"
        " {'type': 'thread.started', 'thread_id': '01abc-session'},\n"
        " {'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'Projected answer'}},\n"
        " {'type': 'turn.completed'},\n"
        "]\n"
        "for event in events:\n"
        " print(json.dumps(event))\n",
        encoding="utf-8")
    agent.chmod(0o755)
    section = dict(config._AGENTS_CFG[codex_json])
    monkeypatch.setattr(config, "_AGENTS_CFG", {str(agent): section})
    monkeypatch.setattr(config, "STREAM_OUTPUT", "full")
    task = Task("work", agent=str(agent), cwd=str(tmp_path))

    result = runner.run_serial([task])[0]
    captured = capsys.readouterr()

    assert result.success is True
    assert result.streamed_output is False
    assert result.streamed_stderr is True
    assert captured.out == "Projected answer"
    assert "thread.started" not in captured.out


@pytest.mark.parametrize(("body", "reason"), [
    (_event({"type": "thread.started", "thread_id": "01abc-session"})
     + "not-json\n", "malformed Codex JSONL"),
    (_event({"type": "thread.started", "thread_id": "01abc-session"})
     + _event({"type": "turn.failed", "error": "provider failed"}),
     "ended with turn.failed"),
])
def test_full_streaming_structured_failure_never_replays_raw_stdout(
        tmp_path, codex_json, monkeypatch, capsys, body, reason):
    agent = tmp_path / codex_json
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({body!r})\n"
        "sys.stderr.write('visible diagnostic\\n')\n",
        encoding="utf-8")
    agent.chmod(0o755)
    section = dict(config._AGENTS_CFG[codex_json])
    monkeypatch.setattr(config, "_AGENTS_CFG", {str(agent): section})
    monkeypatch.setattr(config, "STREAM_OUTPUT", "full")
    task = Task("work", agent=str(agent), cwd=str(tmp_path))

    result = runner.run_task(task)
    runner.save_result(task, result)
    captured = capsys.readouterr()

    assert result.success is False
    assert reason in result.reason
    assert result.stdout == body  # retained in-memory diagnostics only
    assert captured.out == ""
    assert "thread.started" not in captured.out
    assert "visible diagnostic" in captured.err
