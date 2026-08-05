"""Tests for the self-driving iteration engine (pbatch/advance.py)."""

from __future__ import annotations

import json
import sys

from pbatch.advance import (classify_violation, format_plan, record_round,
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
    assert classify_violation("single-implementation interface") == "fe_complexity"


def test_scan_project_on_self(tmp_path, monkeypatch):
    """Scanning the tool's own repo: no lib/ frontend code, so UI/fe dims
    stay empty; any Python issues land in the backend dimensions."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pbatch.advance.STATE_DIR", tmp_path / "docs" / "advance")
    scan = scan_project(".")
    assert scan["target"] == "."
    dims = scan["dimensions"]
    for dim_id in ("ui_spacing", "ui_color", "ui_style", "fe_errors", "fe_n1",
                   "be_errors", "be_architecture"):
        assert dim_id in dims
        assert "count" in dims[dim_id]
        assert "priority" in dims[dim_id]


def test_scan_real_project_ui_clean(tmp_path, monkeypatch):
    """The snaplink-console maintenance state: UI spec dimensions are zero
    (spacing/colors were tokenized), engineering dims carry the debt."""
    target = "/home/u1/workspace/demo/snaplink-console"
    if not __import__("pathlib").Path(target).exists():
        return  # environment without the demo project: skip
    scan = scan_project(target)
    dims = scan["dimensions"]
    assert dims["ui_spacing"]["count"] == 0
    assert dims["ui_color"]["count"] == 0
    assert dims["fe_complexity"]["count"] > 0  # god-files/nesting debt


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
    from pbatch.advance import advance_main
    advance_main(sys.argv[2:])
    payload = json.loads(capsys.readouterr().out)
    assert "target" in payload and "batches" in payload
    assert "total" in payload
