"""The release executable must enforce stdin limits before creating run state."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "smoke-binary.py"
SPEC = importlib.util.spec_from_file_location("stdin_binary_smoke", SCRIPT)
SMOKE = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(SMOKE)
finally:
    sys.path.remove(str(SCRIPT.parent))


def test_stdin_contract_runs_against_source_in_the_binary_isolated_environment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = SMOKE.isolated_environment(tmp_path)
    report = {"checks": []}
    token = SMOKE._REPORT.set(report)
    try:
        SMOKE.check_stdin([sys.executable, str(ROOT / "pi-batch.py")], workspace, environment, 10)
    finally:
        SMOKE._REPORT.reset(token)
    assert len(report["checks"]) == 3
    assert all(check["status"] == "passed" for check in report["checks"])
    assert not list((tmp_path / "stdin-checks").rglob("agent-started"))
    for name in ("oversized", "invalid"):
        assert not (tmp_path / "stdin-checks" / name / "operation.log").exists()
        assert not list((tmp_path / "stdin-checks" / name / "runtime").rglob("*"))


def test_pipe_transport_preserves_invalid_bytes_instead_of_decoding_in_harness(tmp_path):
    payload = b"stdin-private-\xff"
    output = SMOKE._command_output(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"],
        tmp_path, dict(os.environ), 5, "raw pipe", (0,), input_bytes=payload)
    assert output.strip() == payload.hex()


def test_pipe_transport_retains_its_process_deadline(tmp_path):
    started = time.monotonic()
    with pytest.raises(SMOKE.SmokeFailure, match="deadline"):
        SMOKE._command_output([sys.executable, "-c", "import time; time.sleep(30)"],
                              tmp_path, dict(os.environ), 0.1, "hung pipe", (0,), input_bytes=b"x")
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("failure", ["log", "lock", "agent", "diagnostic", "leak"])
def test_exit_two_cannot_mask_input_side_effects_or_false_rejection(tmp_path, monkeypatch, failure):
    fixture = tmp_path / "false-rejection.py"
    _write_false_rejection(fixture, failure)

    def checks(_command, workspace, environment, timeout):
        SMOKE.check_stdin([sys.executable, str(fixture)], workspace, environment, timeout)

    monkeypatch.setattr(SMOKE, "run_checks", checks)
    report_path = tmp_path / "smoke-report.json"
    assert SMOKE.run_smoke(Path(sys.executable).resolve(), 5, report_path) == 1
    report = json.loads(report_path.read_text())
    assert report["checks"][0]["status"] == "passed"
    assert report["checks"][1]["name"] == "stdin oversized whitespace rejected before side effects"
    assert report["checks"][1]["status"] == "failed"
    assert report["checks"][1]["reason"]


def _write_false_rejection(path, failure):
    path.write_text(
        "import pathlib, sys\n"
        "root=pathlib.Path.cwd().parent\n"
        "if '--dry-run' in sys.argv:\n"
        " print('Tasks: 1\\n  [1] 中文🙂 binary-pipe...')\n"
        " raise SystemExit(0)\n"
        f"failure={failure!r}\n"
        "if failure=='log': (root/'operation.log').write_text('started')\n"
        "if failure=='agent': (root/'agent-started').write_text('started')\n"
        "if failure=='lock':\n"
        " (root/'runtime'/'locks').mkdir(parents=True)\n"
        " (root/'runtime'/'locks'/'.pi-batch.lock').write_text('held')\n"
        "message='Input rejected: stdin prompt exceeds 32 bytes'\n"
        "if failure=='diagnostic': message='agent is unavailable'\n"
        "if failure=='leak': message+=' binary-smoke-private'\n"
        "print(message, file=sys.stderr)\n"
        "raise SystemExit(2)\n", encoding="utf-8")


@pytest.mark.parametrize("output", [
    "Tasks: 10\n  [1] 中文🙂 binary-pipe...\n",
    "Tasks: 1\n  [1] changed 中文🙂 binary-pipe...\n",
])
def test_utf8_dry_run_requires_exact_prompt_and_task_count(tmp_path, output):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SMOKE.SmokeFailure, match="exact prompt"):
        SMOKE._check_stdin_result(output, workspace, "")
