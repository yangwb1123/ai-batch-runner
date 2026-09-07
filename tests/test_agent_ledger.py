"""Contracts for the shared heterogeneous-agent run ledger."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pbatch import agent_ledger, config
from pbatch.models import Task


def _record(root, agent: str, ts: str, task: str = "task", **extra) -> bool:
    return agent_ledger.record(
        root, agent=agent, status=extra.pop("status", "succeeded"),
        task_brief=task, ts=ts, **extra)


def test_record_query_round_trip_filters_limits_and_since(tmp_path):
    assert _record(tmp_path, "codex", "2026-08-22T01:00:00Z", "first",
                   attempt=2)
    assert _record(tmp_path, "claude", "2026-08-22T02:00:00Z", "second")
    assert _record(tmp_path, "codex", "2026-08-22T03:00:00Z", "third")

    rows = agent_ledger.query(tmp_path)
    assert [row["task_brief"] for row in rows] == ["first", "second", "third"]
    assert rows[0]["attempt"] == 2
    assert [row["task_brief"] for row in
            agent_ledger.query(tmp_path, agent="codex")] == ["first", "third"]
    assert [row["task_brief"] for row in
            agent_ledger.query(tmp_path, limit=2)] == ["second", "third"]
    assert [row["task_brief"] for row in agent_ledger.query(
        tmp_path, since_ts="2026-08-22T02:00:00Z")] == ["second", "third"]


def test_query_snapshot_reports_corruption_and_result_truncation(tmp_path):
    for index in range(3):
        assert _record(tmp_path, "codex", f"0{index}", f"task-{index}")
    path = agent_ledger.ledger_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(b"{not-json}\n")
        handle.write(b"x" * (agent_ledger.LEDGER_READ_LINE_MAX_BYTES + 1) + b"\n")

    snapshot = agent_ledger.query_snapshot(tmp_path, limit=2)

    assert [row["task_brief"] for row in snapshot.records] == [
        "task-1", "task-2"]
    assert snapshot.skipped_records == 2
    assert snapshot.truncated is True
    assert snapshot.error == ""
    assert snapshot.path == str(path)
    assert snapshot.bytes_read == path.stat().st_size
    assert agent_ledger.query(tmp_path, limit=2) == list(snapshot.records)


def test_record_bounds_task_outputs_and_rejects_oversized_line(tmp_path, monkeypatch):
    outputs = [f"out-{index}.md" for index in range(12)]
    assert _record(tmp_path, "pi", "1", "x" * 200, outputs=outputs)
    row = agent_ledger.query(tmp_path)[0]
    assert len(row["task_brief"]) == 120
    assert len(row["outputs"]) == 8

    warning = Mock()
    monkeypatch.setattr(agent_ledger.log, "warning", warning)
    monkeypatch.setattr(agent_ledger, "LEDGER_MAX_LINE_BYTES", 100)
    assert not _record(tmp_path, "pi", "2", "oversized")
    assert warning.called
    assert len(agent_ledger.query(tmp_path)) == 1


def test_rotation_keeps_latest_lines_and_uses_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_ledger, "LEDGER_MAX_BYTES", 620)
    monkeypatch.setattr(agent_ledger, "LEDGER_KEEP_LINES", 3)
    replaced = Mock(wraps=agent_ledger.os.replace)
    monkeypatch.setattr(agent_ledger.os, "replace", replaced)

    for index in range(8):
        assert _record(tmp_path, "codex", f"{index:02d}", f"task-{index}")

    rows = agent_ledger.query(tmp_path, limit=20)
    assert replaced.called
    assert rows[-1]["task_brief"] == "task-7"
    assert len(rows) < 8
    assert agent_ledger.ledger_path(tmp_path).stat().st_size <= 620
    assert not list((tmp_path / "agents").glob("ledger.*.tmp"))


def test_write_failure_warns_without_raising(tmp_path, monkeypatch):
    warning = Mock()
    monkeypatch.setattr(agent_ledger.log, "warning", warning)

    def fail_open(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(agent_ledger.os, "open", fail_open)
    assert not _record(tmp_path, "gemini", "1")
    assert "write failed" in warning.call_args.args[0]


def test_agents_health_reports_binary_adapter_and_latest_status(tmp_path, monkeypatch):
    _record(tmp_path, "codex", "1", status="failed")
    monkeypatch.setattr(agent_ledger.shutil, "which",
                        lambda name: f"/fake/{name}" if name == "codex" else None)
    monkeypatch.setattr(config, "_agent_section",
                        lambda name: {"argument_flags": {}} if name == "codex" else {})
    monkeypatch.setattr(config, "_AGENT_CFG", {})

    rows = agent_ledger.agents_health(tmp_path, ["codex", "missing"])
    assert rows == [
        {"agent": "codex", "available": True,
         "configured": True, "last_status": "failed"},
        {"agent": "missing", "available": False,
         "configured": False, "last_status": ""},
    ]


def test_shipped_agent_adapter_contracts():
    assert config.agent_argument_flags("claude")["prompt"] == ("-p",)
    assert config.agent_argument_flags("claude")["model"] == ("--model",)
    assert config.agent_session_flags("continue", "sid", "name", "claude") == [
        "--resume", "sid"]
    assert config.agent_argument_flags("gemini")["prompt"] == ("-p",)
    assert config.agent_extra_args("gemini") == (
        "--approval-mode", "yolo", "--skip-trust")
    assert config.agent_argument_flags("opencode")["prompt"] == ("run",)
    assert config.agent_session_flags("fork", "sid", "name", "opencode") == [
        "-s", "sid", "--fork"]
    assert all("error_patterns" not in config._agent_section(name)
               for name in ("claude", "gemini", "opencode"))


@pytest.mark.parametrize(("success", "reason", "validation_ok", "status"), [
    (True, "", None, "succeeded"),
    (False, "user cancelled", None, "cancelled"),
    (False, "validation failed", False, "rejected"),
    (False, "agent exited 1", None, "failed"),
])
def test_record_result_maps_runner_status(
        tmp_path, monkeypatch, success, reason, validation_ok, status):
    captured = {}
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    monkeypatch.setattr(agent_ledger, "record",
                        lambda _root, **values: captured.update(values) or True)
    result = SimpleNamespace(
        success=success, reason=reason, validation_ok=validation_ok,
        run_id="run", session_id="sid", session_name="name",
        elapsed=1.25, returncode=0)
    assert agent_ledger.record_result(result, Task("brief", provider="provider"), str(tmp_path))
    assert captured["status"] == status
    assert captured["elapsed_ms"] == 1250


def test_agents_main_list_and_sessions_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    monkeypatch.setattr(agent_ledger.shutil, "which", lambda _name: None)
    _record(tmp_path, "opencode", "2026-08-22T01:00:00Z", "implement feature",
            session_name="shared-one")

    assert agent_ledger.agents_main(["list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [row["agent"] for row in listing] == list(agent_ledger.DEFAULT_AGENTS)
    assert set(listing[0]) == {"agent", "available", "configured", "last_status"}

    assert agent_ledger.agents_main(
        ["sessions", "--agent", "opencode", "--limit", "1", "--json"]) == 0
    sessions = json.loads(capsys.readouterr().out)
    assert sessions == [{
        "time": "2026-08-22T01:00:00Z", "agent": "opencode",
        "status": "succeeded", "session": "shared-one", "task": "implement feature",
    }]


def test_agents_main_unknown_command_exits_two():
    with pytest.raises(SystemExit) as error:
        agent_ledger.agents_main(["unknown"])
    assert error.value.code == 2


def test_agents_main_empty_sessions_is_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    assert agent_ledger.agents_main(["sessions"]) == 0
    assert capsys.readouterr().out.strip() == "no runs"
