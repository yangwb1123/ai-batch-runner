"""Smoke tests for the CLI entry points: the installed `pi-batch` console
script (pyproject [project.scripts]) and `python -m pbatch` (__main__.py).
The repo-root pi-batch.py shim remains the zero-install fallback."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest


def _run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def test_python_m_pbatch_help():
    result = _run([sys.executable, "-m", "pbatch", "--help"])
    assert result.returncode == 0
    assert "--classify" in result.stdout
    assert "campaign" in result.stdout


def test_python_m_pbatch_classify_subcommand():
    result = _run([sys.executable, "-m", "pbatch", "classify", "生成登录页面 tsx"])
    assert result.returncode == 0
    assert "frontend_ui" in result.stdout
    assert "route to frontend pipeline: True" in result.stdout


def test_python_m_pbatch_classify_json():
    import json
    result = _run([sys.executable, "-m", "pbatch", "classify", "--json", "写一个后端接口"])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["task_type"] == "backend"
    assert payload["route_frontend"] is False


@pytest.mark.skipif(shutil.which("pi-batch") is None,
                    reason="console script not installed (pip install .)")
def test_installed_console_script():
    result = _run(["pi-batch", "--help"])
    assert result.returncode == 0
    assert "usage: pi-batch" in result.stdout
    result = _run(["pi-batch", "classify", "做一个 ERP 排程页面"])
    assert result.returncode == 0
    assert "frontend_ui" in result.stdout


def test_eval_quick_mode_runs_core_domains():
    """`eval --quick` runs only rules + classifier domains."""
    import subprocess
    import sys as _sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [_sys.executable, "-m", "pbatch", "eval", "--quick", "--json"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    import json as _json
    start = result.stdout.index("{")
    payload = _json.loads(result.stdout[start:])
    assert payload["failed"] == 0
    assert payload["total"] >= 20  # core suites (rules + classifier)


def test_health_command_reports_self():
    """`pi-batch health` reports the tool's own health (six-layer ops)."""
    import subprocess
    import sys as _sys
    import json as _json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [_sys.executable, "-m", "pbatch", "health", "--json"],
        cwd=root, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode in (0, 1)
    start = result.stdout.index("{")
    payload = _json.loads(result.stdout[start:])
    assert payload["tests"] > 0
    assert "designintelligence" in payload["checkers"]
    assert "knowledge" in payload["checkers"]
    assert payload["specs"]["ui_specs"] > 0
