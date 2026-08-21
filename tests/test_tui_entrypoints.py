"""Lightweight CLI routing contracts for the interactive terminal UI.

These tests stop at the entry boundary: they do not start the Textual app or
depend on terminal rendering.  Full TUI behavior belongs to the app tests.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from pbatch import cli


ROOT = Path(__file__).resolve().parents[1]


class _Stream(io.StringIO):
    def __init__(self, tty: bool, value: str = "") -> None:
        super().__init__(value)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "expected"),
    ((True, True, True), (True, False, False),
     (False, True, False), (False, False, False)),
)
def test_default_tui_requires_two_ttys(monkeypatch, stdin_tty, stdout_tty,
                                       expected):
    calls = []
    fake_tui = types.ModuleType("pbatch.tui")
    fake_tui.tui_main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "pbatch.tui", fake_tui)
    monkeypatch.setattr(sys, "argv", ["pi-batch"])
    monkeypatch.setattr(sys, "stdin", _Stream(stdin_tty))
    monkeypatch.setattr(sys, "stdout", _Stream(stdout_tty))

    assert cli._dispatch_default_tui() is expected
    assert calls == ([[]] if expected else [])


@pytest.mark.parametrize("arguments", (
    ["--help"], ["-p", "question"], ["classify", "question"],
    ["tui", "--help"],
))
def test_explicit_arguments_never_use_default_tui(monkeypatch, arguments):
    calls = []
    fake_tui = types.ModuleType("pbatch.tui")
    fake_tui.tui_main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "pbatch.tui", fake_tui)
    monkeypatch.setattr(sys, "argv", ["pi-batch", *arguments])
    monkeypatch.setattr(sys, "stdin", _Stream(True))
    monkeypatch.setattr(sys, "stdout", _Stream(True))

    assert cli._dispatch_default_tui() is False
    assert calls == []


def test_main_dispatches_default_tui_before_subcommands(monkeypatch):
    calls = []
    fake_tui = types.ModuleType("pbatch.tui")
    fake_tui.tui_main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "pbatch.tui", fake_tui)
    monkeypatch.setattr(sys, "argv", ["pi-batch"])
    monkeypatch.setattr(sys, "stdin", _Stream(True))
    monkeypatch.setattr(sys, "stdout", _Stream(True))
    monkeypatch.setattr(
        cli, "_dispatch_subcommand",
        lambda: pytest.fail("subcommand dispatch ran before the default TUI"),
    )

    cli.main()

    assert calls == [[]]


def test_explicit_tui_help_is_a_registered_subcommand():
    result = subprocess.run(
        [sys.executable, "-m", "pbatch", "tui", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "tui" in result.stdout.lower()


def test_tui_capability_requires_execution_trust():
    from pbatch.capabilities import build_registry

    capability = next(item for item in build_registry() if item["id"] == "tui")
    assert capability["domain"] == "execution"
    assert capability["risk"] == "medium"
    assert capability["trust_level"] == 3


def test_piped_no_args_preserves_legacy_stdin_batch(tmp_path):
    agent_dir = tmp_path / "bin"
    agent_dir.mkdir()
    agent = agent_dir / "pi"
    agent.write_text(
        "#!/bin/sh\nprintf 'LEGACY-STDIN-RESULT\\n'\n", encoding="utf-8")
    agent.chmod(0o755)
    env = {**os.environ, "PATH": f"{agent_dir}{os.pathsep}{os.environ['PATH']}"}

    result = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py")],
        cwd=tmp_path, env=env, input="analyze this from stdin\n",
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("LEGACY-STDIN-RESULT") == 1
