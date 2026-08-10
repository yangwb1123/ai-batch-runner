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


def test_quoted_color_literals_are_not_hardcoded(tmp_path):
    dart = tmp_path / "i18n.dart"
    dart.write_text("hintText: '#2563EB'.localized,\n", encoding="utf-8")
    result = _run("--dir", str(tmp_path), "--all")
    assert result.returncode == 0
    real = tmp_path / "real.dart"
    real.write_text("final c = Color(0xFF6366F1);\n", encoding="utf-8")
    assert _run("--dir", str(tmp_path), "--all").returncode == 1


def test_sizedbox_big_value_is_component_dimension(tmp_path):
    dart = tmp_path / "dialog.dart"
    dart.write_text("SizedBox(width: 520)\n", encoding="utf-8")
    assert _run("--dir", str(tmp_path), "--all").returncode == 0


def test_token_definition_file_exempt_from_color(tmp_path):
    colors = tmp_path / "theme" / "app_colors.dart"
    colors.parent.mkdir(parents=True)
    colors.write_text("static const Color primary = Color(0xFF6366F1);\n",
                      encoding="utf-8")
    assert _run("--dir", str(tmp_path), "--all").returncode == 0


def test_json_clean_path_stdout_pure(tmp_path):
    """AC5: ui-spec's --json contract is the family reference — a clean
    path emits exactly one JSON document on stdout, summary on stderr."""
    _make_project(tmp_path, {
        "src/pages/Login.tsx": "SizedBox(height: 24)\nEdgeInsets.all(16)\n",
    })
    result = _run("--dir", str(tmp_path), "--all", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert "UI-SPEC" in result.stderr
    assert "UI-SPEC" not in result.stdout


def test_validator_cmd_uses_ui_scan_dir_placeholder():
    """T1.1: the three project-scan validator commands use the
    {ui_scan_dir} placeholder (not a literal {cwd}/src); existing
    --dir/--all script anchor substrings survive."""
    from pbatch.config import _resolve_validators
    for name, script, has_all in (("uicheck", "check-ui-spec.py", True),
                                  ("uigeometry", "check-ui-geometry.py", True),
                                  ("uiquality", "check-frontend-quality.py", False)):
        cmds = _resolve_validators(name)
        assert cmds, name
        assert "--dir {ui_scan_dir}" in cmds[0], name
        assert "{cwd}/src" not in cmds[0], name
        assert script in cmds[0], name
        assert (" --all " in cmds[0]) == has_all, name


def test_ui_scan_dir_expansion_default_and_configured(monkeypatch):
    """T1.2/T1.3: expansion helper — unset -> {cwd}/src (byte-compatible
    default), relative -> {cwd}/<value>, absolute -> as-is, whitespace ->
    unset (fail-safe); {output} untouched when output is None (cli_batch
    semantics), quoted when given."""
    from pbatch import config
    from pbatch.cmd_expand import expand_cmd
    monkeypatch.setattr(config, "_BATCH_CFG", {})
    assert expand_cmd("--dir {ui_scan_dir}", None, "/proj") == "--dir /proj/src"
    assert expand_cmd("--dir {ui_scan_dir} -o {output}", None, "/proj") == \
        "--dir /proj/src -o {output}"
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "lib"})
    assert expand_cmd("--dir {ui_scan_dir}", None, "/proj") == "--dir /proj/lib"
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "/abs/frontend"})
    assert expand_cmd("--dir {ui_scan_dir}", None, "/proj") == "--dir /abs/frontend"
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "my lib"})
    assert expand_cmd("--dir {ui_scan_dir}", None, "/proj") == "--dir /proj/'my lib'"
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "   "})
    assert expand_cmd("--dir {ui_scan_dir}", None, "/proj") == "--dir /proj/src"
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "lib"})
    assert expand_cmd("--dir {ui_scan_dir} -o {output}", "out.json", "/proj") == \
        "--dir /proj/lib -o out.json"


def test_expand_cmd_defensive_branch_logs_unresolved(monkeypatch, caplog):
    """FM5: a ui_scan_dir value containing the placeholder itself must
    never reach the shell silently — the defensive branch fires a
    log.error pinning the unresolved placeholder (design failure-mode
    table row 5; testing-reviewer F1)."""
    from pbatch import config
    from pbatch.cmd_expand import expand_cmd
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "{ui_scan_dir}"})
    with caplog.at_level("ERROR", logger="pi-batch"):
        out = expand_cmd("--dir {ui_scan_dir}", None, "/proj")
    assert "{ui_scan_dir}" in out  # literal leak is flagged, not sanitized
    assert "unresolved {ui_scan_dir}" in caplog.text


def test_dir_scan_zero_files_warns_on_stderr(tmp_path):
    """T2.1: empty project scan -> exit 0, explicit warning on stderr,
    stdout OK summary unchanged (regression anchor preserved). The word
    'warning' must never appear on stdout (F-7 negative assertion)."""
    _make_project(tmp_path, {"README.md": "no frontend here\n"})
    result = _run("--dir", str(tmp_path), "--all")
    assert result.returncode == 0
    assert "warning" in result.stderr
    assert "0 file(s) scanned" in result.stderr
    assert "0 file(s) scanned" in result.stdout
    assert "warning" not in result.stdout


def test_json_zero_files_warning_stderr_only(tmp_path):
    """T2.2: --json keeps stdout a pure JSON payload; the warning goes to
    stderr only."""
    _make_project(tmp_path, {"README.md": "no frontend here\n"})
    result = _run("--dir", str(tmp_path), "--all", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 0
    assert "UI-SPEC: warning:" not in result.stdout
    assert "UI-SPEC: warning:" in result.stderr


def test_no_warning_single_file_mode(tmp_path):
    """T2.3: single-file mode (no --dir) never warns about zero scans."""
    good = tmp_path / "ok.tsx"
    good.write_text("SizedBox(height: 24)\n", encoding="utf-8")
    result = _run("--mode", "spacing", str(good))
    assert result.returncode == 0
    assert "warning" not in result.stderr


def test_dir_scan_lib_layout_finds_files(tmp_path, monkeypatch):
    """T2.4/T3.1/T3.2 (script level) + T3.3 (wiring): a lib/ layout is
    scanned for real — violations found, clean tree passes with a non-zero
    scan count, and ui_scan_dir config drives the executed command."""
    from pbatch import config
    from pbatch.cmd_expand import expand_cmd
    # T3.1 violation branch: lib/ files are scanned, violations found
    bad = tmp_path / "viol"
    _make_project(bad, {"lib/components/Button.tsx": "SizedBox(height: 3)\n"})
    result = _run("--dir", str(bad / "lib"), "--all", "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 1
    assert payload["total"] == 1
    # T3.2/T2.4 compliant branch: exit 0, non-zero scan count, no warning
    good = tmp_path / "ok"
    _make_project(good, {"lib/components/Good.tsx": "SizedBox(height: 24)\n"})
    result = _run("--dir", str(good / "lib"), "--all", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] > 0
    assert "warning" not in result.stderr
    # T3.3 wiring: ui_scan_dir config -> expand_cmd -> executed shell line
    monkeypatch.setattr(config, "_BATCH_CFG", {"ui_scan_dir": "lib"})
    cmd = expand_cmd(
        "python {tool_root}/scripts/check-ui-spec.py --dir {ui_scan_dir} --all --json",
        None, str(good))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            cwd=good, timeout=120)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] > 0
