"""Tests for project-wide UI spec scanning (scripts/check-ui-spec.py --dir,
--all, --json) and the ui-refactor pipeline that uses it as a gate."""

from __future__ import annotations

import json
import subprocess
import sys

SCRIPT = "scripts/check-ui-spec.py"


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, cwd=cwd, timeout=120)


def _make_project(root, files: dict, extra_dirs: tuple = ()):
    for d in extra_dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_dir_scan_finds_violations_and_skips_vendor(tmp_path):
    _make_project(tmp_path, {
        "src/pages/Login.tsx": "SizedBox(height: 3)\nmargin: 11\n",
        "src/lib/util.dart": "EdgeInsets.only(left: 13)\n",
        "src/pages/Dash.vue": "color: #1296db\n",
        "node_modules/pkg/bad.tsx": "SizedBox(height: 3)\n",
        "src/pages/Good.tsx": "SizedBox(height: 24)\n",
    })
    result = _run("--dir", str(tmp_path), "--all")
    assert result.returncode == 1
    assert "Login.tsx" in result.stderr
    assert "util.dart" in result.stderr
    assert "Dash.vue" in result.stderr
    # vendor and compliant files are not flagged
    assert "node_modules" not in result.stderr
    assert "Good.tsx" not in result.stderr
    # violations go to stderr (the runner's retry-feedback channel)
    assert result.stdout == ""  # failure: nothing on stdout without --json
    assert "violation(s)" in result.stderr  # summary on stderr too


def test_dir_scan_clean_project_passes(tmp_path):
    _make_project(tmp_path, {
        "src/pages/Login.tsx": "SizedBox(height: 24)\nEdgeInsets.all(16)\n",
    })
    result = _run("--dir", str(tmp_path), "--all")
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_dir_scan_no_frontend_files_passes(tmp_path):
    _make_project(tmp_path, {"README.md": "no frontend here\n"})
    result = _run("--dir", str(tmp_path), "--all")
    assert result.returncode == 0
    assert "0 file(s) scanned" in result.stdout


def test_exclude_pattern(tmp_path):
    _make_project(tmp_path, {
        "src/a.tsx": "margin: 11\n",
        "generated/b.tsx": "margin: 11\n",
    })
    result = _run("--dir", str(tmp_path), "--all", "--exclude", "generated")
    assert result.returncode == 1
    assert "src/a.tsx" in result.stderr
    assert "generated" not in result.stderr


def test_json_report_and_output_file(tmp_path):
    _make_project(tmp_path, {"src/a.tsx": "SizedBox(height: 3)\n"})
    report = tmp_path / "out" / "report.json"
    result = _run("--dir", str(tmp_path), "--all", "--json", "-o", str(report))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["mode"] == "all"
    assert payload["files_scanned"] == 1
    assert payload["total"] == 1
    assert "a.tsx" in payload["violations"][0]["file"]
    assert report.exists()
    assert json.loads(report.read_text(encoding="utf-8"))["total"] == 1


def test_single_file_mode_still_works(tmp_path):
    bad = tmp_path / "bad.dart"
    bad.write_text("EdgeInsets.only(left: 13)\n", encoding="utf-8")
    result = _run("--mode", "spacing", str(bad))
    assert result.returncode == 1
    assert "13" in result.stderr


def test_unknown_mode_exits_2():
    result = _run("--mode", "bogus", "x.tsx")
    assert result.returncode == 2


def test_ui_refactor_pipeline_schema_and_gate():
    from pbatch.config import _resolve_validators
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/ui-refactor-pipeline.yaml")
    assert [s.name for s in pipeline.stages] == ["refactor", "review", "gate"]
    assert pipeline.stages[0].validate_cmd == "uicheck"
    assert pipeline.stages[2].gate is True
    commands = _resolve_validators("uicheck")
    assert commands and "check-ui-spec.py --dir" in commands[0]
    assert "--all" in commands[0]
