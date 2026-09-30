"""Regression coverage for operational-log and model-message separation."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch.runner import _read_stream


ROOT = Path(__file__).resolve().parents[1]
PI_BATCH = ROOT / "pi-batch.py"


def _agent(tmp_path: Path) -> Path:
    path = tmp_path / "agent.sh"
    path.write_text(
        "#!/bin/sh\nprintf 'UNIQUE-MODEL-BODY\\n'\nprintf 'UNIQUE-MODEL-ERR\\n' >&2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("mode,expected", [("auto", 0), ("full", 1)])
def test_cli_stream_policy_separates_body_from_artifact(tmp_path, mode, expected):
    output = tmp_path / f"{mode}.md"
    command = [
        sys.executable, str(PI_BATCH), "-p", "question", "-o", str(output),
        "--agent-bin", str(_agent(tmp_path)), "--memory-mode", "off",
        "--stream-output", mode, "--no-lock",
    ]
    proc = subprocess.run(command, cwd=tmp_path, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("UNIQUE-MODEL-BODY") == expected
    assert output.read_text(encoding="utf-8") == "UNIQUE-MODEL-BODY\n"


def test_full_stream_stdout_shortcut_prints_answer_once(tmp_path):
    command = [
        sys.executable, str(PI_BATCH), "-p", "question",
        "--agent-bin", str(_agent(tmp_path)), "--memory-mode", "off",
        "--stream-output", "full", "--no-lock",
    ]
    proc = subprocess.run(command, cwd=tmp_path, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("UNIQUE-MODEL-BODY") == 1
    assert proc.stderr.count("UNIQUE-MODEL-ERR") == 1


def test_nested_log_directory_and_startup_failure_cleanup(tmp_path):
    log_file = tmp_path / "nested" / "logs" / "run.log"
    command = [sys.executable, str(PI_BATCH), "-p", "question", "-o", "out.md",
               "--agent-bin", str(_agent(tmp_path)), "--memory-mode", "off",
               "--log-file", str(log_file), "--stream-output", "none"]
    proc = subprocess.run(command, cwd=tmp_path, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "OK  done" in log_file.read_text(encoding="utf-8")
    invalid = subprocess.run(
        [sys.executable, str(PI_BATCH), "-p", "question",
         "--agent-bin", str(_agent(tmp_path)),
         "--session-mode", "per-stage", "--memory-mode", "off"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert invalid.returncode == 1
    assert "per-stage" in invalid.stderr
    assert not (tmp_path / ".pi-batch.lock").exists()


def test_live_display_stops_at_collection_cap():
    class SizedReader(io.StringIO):
        sizes = []

        def readline(self, size=-1):
            self.sizes.append(size)
            return super().readline(size)

    collector = []
    overflow = [False]
    shown = io.StringIO()
    source = SizedReader("one\ntwo\ntail\n")
    _read_stream(source, "[x] ", collector,
                 cap=8, overflow=overflow, emit=True, target=shown)
    assert collector == ["one\n", "two\n"]
    assert shown.getvalue() == "[x] one\n[x] two\n"
    assert overflow == [True]
    assert set(source.sizes) == {8192}
