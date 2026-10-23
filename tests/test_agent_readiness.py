"""Operator-declared agent readiness probes: classification and CLI contract.

Readiness is an exit-code contract on an operator-supplied local command.
These tests pin the exact three-way classification, fail-closed configuration
handling, bounded/redacted evidence, no-shell argv parsing and the read-only
``agents readiness`` projection.  No model or provider is ever called.
"""

from __future__ import annotations

import json
import sys

import pytest

from pbatch import agent_readiness as readiness
from pbatch import config


@pytest.fixture(autouse=True)
def _clean_agent_config(monkeypatch):
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {})


def _configure(monkeypatch, value, agent="probeagent"):
    monkeypatch.setitem(config._AGENTS_CFG, agent, {"readiness_cmd": value})


def _python(code: str) -> list:
    return [sys.executable, "-c", code]


def test_unconfigured_probe_is_unknown_and_spawns_nothing(monkeypatch):
    monkeypatch.setattr(readiness, "run_bounded",
                        lambda *a, **k: pytest.fail("unconfigured probe must not spawn"))
    result = readiness.probe("probeagent")
    assert result.status == readiness.UNKNOWN
    assert result.configured is False
    assert result.exit_code is None
    assert result.duration_ms is None
    assert "no readiness_cmd configured" in result.reason
    row = result.as_dict()
    assert row["source"] is None and row["command"] is None
    assert row["diagnostic"] is None


def test_exit_zero_is_ready_even_with_failure_shaped_output(monkeypatch):
    _configure(monkeypatch, _python(
        "print('not logged in'); raise SystemExit(0)"))
    result = readiness.probe("probeagent")
    assert result.status == readiness.READY
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.duration_ms is not None and result.duration_ms >= 0


def test_nonzero_exit_is_not_ready_including_unauthenticated(monkeypatch):
    _configure(monkeypatch, _python(
        "print('Not logged in'); raise SystemExit(1)"))
    result = readiness.probe("probeagent")
    assert result.status == readiness.NOT_READY
    assert result.exit_code == 1
    assert "Not logged in" in result.tail


def test_spawn_failure_is_unknown(tmp_path, monkeypatch):
    _configure(monkeypatch, [str(tmp_path / "missing-probe")])
    result = readiness.probe("probeagent")
    assert result.status == readiness.UNKNOWN
    assert result.exit_code is None
    assert "did not complete" in result.reason


def test_timeout_is_unknown_and_bounded(monkeypatch):
    _configure(monkeypatch, _python("import time; time.sleep(5)"))
    monkeypatch.setattr(readiness, "DEFAULT_TIMEOUT_SECONDS", 0.2)
    result = readiness.probe("probeagent")
    assert result.status == readiness.UNKNOWN
    assert result.timed_out is True
    assert result.exit_code is None
    assert "timed out" in result.reason


def test_oversized_output_is_unknown(monkeypatch):
    _configure(monkeypatch, _python("import sys; sys.stdout.write('x' * 9000)"))
    result = readiness.probe("probeagent")
    assert result.status == readiness.UNKNOWN
    assert result.exit_code is None
    assert "output exceeded" in result.reason
    assert len(result.tail) <= readiness.MAX_DIAGNOSTIC_CHARS


@pytest.mark.parametrize("value", [
    "", "   ", "unterminated 'quote", [], [""], [1], 123, True, {"cmd": "x"},
    ["probe"] * (readiness.MAX_TOKENS + 1),
    ["probe", "x" * (readiness.MAX_TOKEN_CHARS + 1)],
    ["probe", "bad\x00token"],
])
def test_invalid_config_is_unknown_and_never_runs(monkeypatch, value):
    monkeypatch.setattr(readiness, "run_bounded",
                        lambda *a, **k: pytest.fail("invalid config must not spawn"))
    _configure(monkeypatch, value)
    result = readiness.probe("probeagent")
    assert result.status == readiness.UNKNOWN
    assert result.configured is False
    assert result.exit_code is None
    assert "invalid readiness configuration" in result.reason


def test_per_agent_override_wins_over_global(monkeypatch):
    monkeypatch.setitem(config._AGENT_CFG, "readiness_cmd",
                        _python("raise SystemExit(1)"))
    monkeypatch.setitem(config._AGENTS_CFG, "codex",
                        {"readiness_cmd": _python("raise SystemExit(0)")})
    assert readiness.probe("codex").status == readiness.READY
    assert readiness.probe("probeagent").status == readiness.NOT_READY


def test_string_is_split_to_argv_without_shell_interpolation(tmp_path, monkeypatch):
    marker = tmp_path / "shell-marker"
    _configure(monkeypatch, f"printf hi > {marker}")
    result = readiness.probe("probeagent", cwd=str(tmp_path))
    assert result.status == readiness.READY
    assert result.argv == ("printf", "hi", ">", str(marker))
    assert marker.exists() is False


def test_string_quotes_are_preserved_as_single_argv_entry(monkeypatch):
    _configure(monkeypatch, '"probe" "two words"')
    argv, source, error = readiness.declared_argv("probeagent")
    assert (argv, error) == (("probe", "two words"), "")
    assert source == "agent.agents.probeagent.readiness_cmd"


def test_redacts_credential_shaped_output_and_command(monkeypatch):
    _configure(monkeypatch, [
        sys.executable, "-c",
        "print('token=supersecret123 password: hunter2 "
        "MY_API_KEY=sk-env-secret'); raise SystemExit(1)",
        "--api-key", "sk-live-secret-value",
    ])
    result = readiness.probe("probeagent")
    assert result.status == readiness.NOT_READY
    assert "supersecret123" not in result.tail
    assert "hunter2" not in result.tail
    assert "sk-env-secret" not in result.tail
    assert "<redacted>" in result.tail
    row = json.dumps(result.as_dict())
    assert "sk-live-secret-value" not in row
    assert "<redacted>" in row


def test_probe_invokes_only_the_declared_argv(monkeypatch):
    from pbatch import runner
    from pbatch.process_exec import ValidationResult

    calls = []

    def _capture(argv, cwd, timeout, cap, shell, label, cancel_event=None):
        calls.append((argv, cwd, timeout, cap, shell, label, cancel_event))
        return ValidationResult(True, 0, "ok", "", False)

    monkeypatch.setattr(readiness, "run_bounded", _capture)
    monkeypatch.setattr(runner, "run_task", lambda *a, **k: pytest.fail("model call"))
    monkeypatch.setattr(runner, "run_argv", lambda *a, **k: pytest.fail("model call"))
    declared = _python("print('ok')")
    _configure(monkeypatch, declared)
    result = readiness.probe("probeagent", cwd="/tmp")
    assert result.status == readiness.READY
    assert calls == [(declared, "/tmp", readiness.DEFAULT_TIMEOUT_SECONDS,
                      readiness.DEFAULT_OUTPUT_CAP, False, "agent readiness", None)]


def test_json_subcommand_shape_is_bounded(monkeypatch, capsys):
    from pbatch import agent_ledger

    _configure(monkeypatch, _python(
        "print('x' * 5000 + ' token=topsecret'); raise SystemExit(1)"))
    monkeypatch.setattr(agent_ledger.config, "data_dir",
                        lambda *a, **k: pytest.fail("readiness must stay read-only"))
    assert agent_ledger.agents_main(
        ["readiness", "--agent", "probeagent", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert set(row) == {"agent", "status", "configured", "source", "command",
                        "exit_code", "timed_out", "duration_ms", "reason",
                        "diagnostic"}
    assert row["agent"] == "probeagent"
    assert row["status"] == readiness.NOT_READY
    assert row["configured"] is True
    assert row["source"] == "agent.agents.probeagent.readiness_cmd"
    assert row["exit_code"] == 1
    assert row["timed_out"] is False
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0
    assert len(row["command"]) <= readiness.MAX_COMMAND_CHARS
    assert len(row["diagnostic"]) <= readiness.MAX_DIAGNOSTIC_CHARS
    assert "topsecret" not in json.dumps(row)


def test_default_readiness_query_lists_default_agents(capsys):
    from pbatch import agent_ledger

    assert agent_ledger.agents_main(["readiness", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["agent"] for row in rows] == list(agent_ledger.DEFAULT_AGENTS)
    assert {row["status"] for row in rows} == {readiness.UNKNOWN}
    assert all(row["configured"] is False for row in rows)


def test_human_readiness_table_keeps_status_column(capsys):
    from pbatch import agent_ledger

    assert agent_ledger.agents_main(["readiness", "--agent", "probeagent"]) == 0
    out = capsys.readouterr().out
    assert "AGENT" in out and "STATUS" in out and "DIAGNOSTIC" in out
    assert "unknown" in out


def test_unsafe_agent_name_is_rejected_by_parser(capsys):
    from pbatch import agent_ledger

    with pytest.raises(SystemExit):
        agent_ledger.agents_main(["readiness", "--agent", "bad name"])
    assert "without spaces" in capsys.readouterr().err
