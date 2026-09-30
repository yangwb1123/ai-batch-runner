"""The bundled Go gate must preserve formatter failure before accepting output."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "posix", reason="local executable fixtures use POSIX shell")
@pytest.mark.parametrize("formatter", ["missing", "error", "dirty", "clean"])
def test_gofmt_gate_requires_successful_formatter_and_clean_output(tmp_path, formatter):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    programs = tmp_path / "bin"
    programs.mkdir()
    agent = workspace / "agent"
    agent.write_text("#!/bin/sh\nprintf 'package main\\n\\nfunc main() {}\\n'\n")
    agent.chmod(0o755)
    if formatter != "missing":
        commands = {"error": "echo 'fixture parser rejected input' >&2\nexit 2",
                    "dirty": "printf '%s\\n' \"$2\"", "clean": "exit 0"}
        executable = programs / "gofmt"
        executable.write_text("#!/bin/sh\n" + commands[formatter] + "\n")
        executable.chmod(0o755)
    output = workspace / "generated source.go"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PBATCH_", "XDG_"))}
    environment.update(PATH=str(programs), PBATCH_HOME=str(tmp_path / "home"),
                       PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"))
    result = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "-p", "generate Go", "-o", str(output),
         "--agent-bin", str(agent), "--validate", "gofmt", "--retries", "0",
         "--memory-mode", "off", "--log-file", ""],
        cwd=workspace, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == (0 if formatter == "clean" else 1), result.stderr
    assert "Traceback" not in result.stderr
    if formatter == "clean":
        assert output.read_text() == "package main\n\nfunc main() {}\n"
    else:
        assert not output.exists()
        assert "VALIDATION FAILED" in result.stderr
