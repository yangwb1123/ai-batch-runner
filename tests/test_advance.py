"""Tests for the self-driving iteration engine (pbatch/advance.py)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from pbatch.advance import (CheckerError, _run_checker, advance_main,
                            classify_violation, format_plan, record_round,
                            scan_project)


def test_classify_violation_dimensions():
    assert classify_violation("spacing: SizedBox(height: 10)") == "ui_spacing"
    assert classify_violation("color: Color(0xFF6366F1)") == "ui_color"
    assert classify_violation("style: style={{") == "ui_style"
    assert classify_violation("swallowed exception (empty catch)") == "fe_errors"
    assert classify_violation("N+1 request risk") == "fe_n1"
    assert classify_violation("nesting depth 20 > 8") == "fe_complexity"
    assert classify_violation("test file without assertions") == "be_errors"
    assert classify_violation("dangerous DDL") == "be_errors"
    assert classify_violation("single-implementation interface") == "be_architecture"
    assert classify_violation(
        "single-implementation interface",
        "check-frontend-quality.py") == "fe_complexity"
    assert classify_violation(
        "single-implementation interface",
        "check-backend-quality.py") == "be_architecture"


def test_scan_project_on_self(tmp_path, monkeypatch):
    """Scanning the tool's own repo: no lib/ frontend code, so UI/fe dims
    stay empty; any Python issues land in the backend dimensions."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pbatch.advance.STATE_DIR", tmp_path / "docs" / "advance")
    scan = scan_project(".")
    assert scan["target"] == "."
    dims = scan["dimensions"]
    for dim_id in ("ui_spacing", "ui_color", "ui_style", "fe_errors", "fe_n1",
                   "be_errors", "be_architecture", "knowledge",
                   "design_intelligence", "backend_experience"):
        assert dim_id in dims
        assert "count" in dims[dim_id]
        assert "priority" in dims[dim_id]


def test_scan_real_project_ui_clean(tmp_path, monkeypatch):
    """The scanner must work on a real external tree and report every
    dimension with countable numbers. Exact violation counts are EXTERNAL
    state (the console repo evolves independently — retro lesson D4:
    never assert external drift), so this test asserts structure, not
    cleanliness."""
    target = "/home/u1/workspace/demo/snaplink-console"
    if not __import__("pathlib").Path(target).exists():
        return  # environment without the demo project: skip
    scan = scan_project(target)
    dims = scan["dimensions"]
    for dim_id in ("ui_spacing", "ui_color", "ui_style", "fe_errors",
                   "fe_complexity", "fe_n1", "be_errors", "be_architecture"):
        assert dim_id in dims, dim_id
        assert isinstance(dims[dim_id]["count"], int), dim_id
        assert "files" in dims[dim_id], dim_id


def test_record_round_append(tmp_path, monkeypatch):
    monkeypatch.setattr("pbatch.advance.STATE_DIR", tmp_path)
    scan = {"target": "x", "dimensions": {"ui_spacing": {"count": 3}}}
    record_round(scan, 1)
    record_round(scan, 2)
    lines = (tmp_path / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["round"] == 1
    assert json.loads(lines[1])["round"] == 2


def test_format_plan_empty_and_batches():
    empty = format_plan([], 0)
    assert "无需推进" in empty
    plan = format_plan([{"priority": "P0", "id": "fe_errors", "count": 2,
                         "label": "x", "fix": "y", "files": [], "samples": []}], 2)
    assert "[P0]" in plan and "x（2 项）" in plan


def test_advance_cli_json(capsys):
    sys.argv = ["pi-batch.py", "advance", "--dir", ".", "--json"]
    assert advance_main(sys.argv[2:]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "target" in payload and "batches" in payload
    assert "total" in payload


def test_scan_runs_every_registered_checker_and_preserves_source(monkeypatch):
    calls = []

    def fake_run(script, _target):
        name = __import__("pathlib").Path(script).name
        calls.append(name)
        detail = {
            "check-ui-spec.py": "spacing: 10px",
            "check-frontend-quality.py": "single-implementation interface",
            "check-backend-quality.py": "single-implementation interface",
            "check-knowledge-freshness.py": "unknown knowledge finding",
            "check-design-intelligence.py": "unknown design finding",
            "check-backend-experience.py": "unknown lifecycle finding",
        }[name]
        return {"files_scanned": 1,
                "violations": [{"file": "x", "detail": detail}]}

    monkeypatch.setattr("pbatch.advance._run_checker", fake_run)
    scan = scan_project(".")
    assert len(calls) == 6
    assert scan["dimensions"]["ui_spacing"]["count"] == 1
    assert scan["dimensions"]["fe_complexity"]["count"] == 1
    assert scan["dimensions"]["be_architecture"]["count"] == 1
    assert scan["dimensions"]["knowledge"]["count"] == 1
    assert scan["dimensions"]["design_intelligence"]["count"] == 1
    assert scan["dimensions"]["backend_experience"]["count"] == 1


def test_run_checker_fails_closed_on_execution_or_bad_json(tmp_path, monkeypatch):
    checker = tmp_path / "checker.py"
    checker.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2, stdout="", stderr="usage error"))
    with pytest.raises(CheckerError, match="exited 2"):
        _run_checker(str(checker), ".")

    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="not-json", stderr=""))
    with pytest.raises(CheckerError, match="invalid JSON"):
        _run_checker(str(checker), ".")


def test_recorded_rounds_rescan_and_stop_when_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pbatch.advance.STATE_DIR", tmp_path)
    scans = [
        {"target": ".", "files_scanned": 1,
         "dimensions": {"x": {"count": 1, "priority": 0, "label": "x",
                               "fix": "x", "files": [], "samples": []}}},
        {"target": ".", "files_scanned": 1,
         "dimensions": {"x": {"count": 0, "priority": 0, "label": "x",
                               "fix": "x", "files": [], "samples": []}}},
    ]
    monkeypatch.setattr("pbatch.advance.scan_project", lambda _target: scans.pop(0))
    assert advance_main(["--max-rounds", "5", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rounds_run"] == 2
    assert payload["total"] == 0
    assert len((tmp_path / "state.jsonl").read_text().splitlines()) == 2
