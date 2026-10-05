"""Contract tests for the root engineering-gate CLI entry point."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import cli as gate_cli


def test_run_delegates_to_subprocess(monkeypatch):
    expected = subprocess.CompletedProcess(["tool", "arg"], 0)
    observed = {}

    def fake_run(args, **kwargs):
        observed.update(args=args, kwargs=kwargs)
        return expected

    monkeypatch.setattr(gate_cli.subprocess, "run", fake_run)
    assert gate_cli.run("tool", "arg", cwd="/tmp") is expected
    assert observed["args"] == ["tool", "arg"]
    assert observed["kwargs"] == {
        "capture_output": False, "text": True, "check": False, "cwd": "/tmp"}


def test_aggregate_fails_closed_when_gate_crashes(capsys):
    def broken():
        raise RuntimeError("boom")

    assert gate_cli._aggregate([("ok", lambda: 0), ("broken", broken)]) == 1
    assert "ERROR: broken crashed: boom" in capsys.readouterr().out


def test_cmd_check_runs_all_gates_and_harness_delegates(monkeypatch):
    called = []
    for name in ("_check_filesize", "_check_complexity", "_check_root", "_check_style"):
        monkeypatch.setattr(gate_cli, name,
                            lambda name=name: called.append(name) or 0)
    assert gate_cli.cmd_check() == 0
    # projects/ is machine-local, so its gate is opt-in, never part of the
    # versioned contract — see _check_project_gates' docstring.
    assert called == ["_check_filesize", "_check_complexity",
                      "_check_root", "_check_style"]
    monkeypatch.setattr(gate_cli, "cmd_check", lambda: 7)
    assert gate_cli.cmd_harness() == 7


def test_project_gates_is_opt_in_and_still_fails_closed(monkeypatch):
    calls = []

    class _Gates:
        @staticmethod
        def run() -> int:
            calls.append(1)
            return 1

    monkeypatch.setitem(sys.modules, "checks.project_gates", _Gates)
    monkeypatch.setattr(gate_cli, "_aggregate",
                        lambda gates, mode=False: [name for name, _ in gates])
    # Not reachable from cmd_check ...
    assert "_check_project_gates" not in gate_cli._aggregate([])
    # ... but explicitly invocable, and it propagates a failure verbatim.
    monkeypatch.setattr(sys, "argv", ["cli.py", "check-project-gates"])
    assert gate_cli.main() == 1
    assert calls == [1]


def test_cmd_check_test_uses_unittest_discovery(monkeypatch):
    observed = []

    def fake_run(*args):
        observed.extend(args)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(gate_cli, "run", fake_run)
    assert gate_cli.cmd_check_test() == 3
    assert observed == [sys.executable, "-m", "unittest", "discover",
                        "-s", "checks", "-p", "test_*.py"]


def test_main_help_and_unknown_command(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py"])
    assert gate_cli.main() == 0
    assert "Usage:" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["cli.py", "missing"])
    assert gate_cli.main() == 2
    assert "unknown command: missing" in capsys.readouterr().err


def test_main_dispatches_every_command(monkeypatch):
    commands = {
        "check": "cmd_check",
        "check-filesize": "_check_filesize",
        "check-complexity": "_check_complexity",
        "check-root": "_check_root",
        "check-style": "_check_style",
        "check-test": "cmd_check_test",
        "check-project-gates": "_check_project_gates",
        "project-gate": "cmd_project_gate",
        "harness": "cmd_harness",
    }
    for command, target in commands.items():
        called = []
        monkeypatch.setattr(gate_cli, target,
                            lambda command=command: called.append(command) or 0)
        monkeypatch.setattr(sys, "argv", ["cli.py", command])
        assert gate_cli.main() == 0
        assert called == [command]
