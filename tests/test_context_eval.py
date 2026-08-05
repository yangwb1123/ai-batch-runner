"""Tests for Context Engineering (pbatch/context.py) and Evaluation
Engineering (pbatch/eval.py + the evals/ suite)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pbatch import context as ctx
from pbatch import eval as ev


def test_context_map_loads(tmp_path):
    routes = ctx.load_context_map()  # repo template
    assert routes and all(isinstance(r, dict) for r in routes)


def test_path_routing_matches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x", encoding="utf-8")
    routes = [{"when": {"paths": ["src/order/**"]}, "load": ["docs/x.md"]}]
    manifest = ctx.match_context("anything", ["src/order/a.ts"], routes)
    assert manifest["files"] == ["docs/x.md"]


def test_keyword_routing_without_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "p.md").write_text("x", encoding="utf-8")
    routes = [{"when": {"task_contains": ["支付", "payment"]},
               "load": ["docs/p.md"]}]
    manifest = ctx.match_context("实现支付逻辑", None, routes)
    assert manifest["files"] == ["docs/p.md"]


def test_missing_referenced_file_reported(tmp_path):
    routes = [{"when": {"paths": ["src/**"]}, "load": ["docs/nope.md"]}]
    manifest = ctx.match_context("x", ["src/a.ts"], routes)
    assert manifest["files"] == []
    assert manifest["missing"] and "nope.md" in manifest["missing"][0]


def test_no_match_loads_nothing(tmp_path):
    routes = [{"when": {"task_contains": ["登录"]}, "load": ["docs/auth.md"]}]
    manifest = ctx.match_context("做表格页面", None, routes)
    assert manifest["files"] == []
    assert manifest["missing"] == []


def test_context_cli_json(capsys):
    sys.argv = ["pi-batch.py", "context", "--json", "--paths", "src/order/a.ts",
                "订单模块"]
    ctx.context_main(sys.argv[2:])
    payload = json.loads(capsys.readouterr().out)
    assert "files" in payload
    assert "missing" in payload


def test_eval_suite_passes():
    """The committed evals/ suite is the rule-system regression contract."""
    sys.argv = ["pi-batch.py", "eval"]
    try:
        ev.eval_main(sys.argv[2:])
        code = 0
    except SystemExit as exc:
        code = exc.code
    assert code == 0, ("rule-system regression suite failed — a keyword/"
                       "registry change broke behavior")


def test_eval_single_case_filter():
    sys.argv = ["pi-batch.py", "eval", "--filter", "demo-login-loads-visual-core-only"]
    try:
        ev.eval_main(sys.argv[2:])
        code = 0
    except SystemExit as exc:
        code = exc.code
    assert code == 0


def test_eval_failure_detected(tmp_path, monkeypatch):
    suite = {"name": "t", "cases": [
        {"id": "bad", "input": "随便写点什么",
         "assert": {"task_type": "frontend_ui"}}]}
    results = ev.run_suite(suite)
    case_id, ok, detail = results[0]
    assert ok is False
    assert "task_type" in detail


def test_dangerous_ddl_detected(tmp_path):
    import subprocess
    script = "scripts/check-backend-quality.py"
    sql = tmp_path / "q.sql"
    sql.write_text("DROP TABLE orders;\n", encoding="utf-8")
    result = subprocess.run([sys.executable, script, str(sql)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "dangerous DDL" in result.stderr


def test_ddl_allowed_in_migration_files(tmp_path):
    import subprocess
    script = "scripts/check-backend-quality.py"
    mig = tmp_path / "migrations" / "002_down.sql"
    mig.parent.mkdir(parents=True, exist_ok=True)
    mig.write_text("DROP TABLE orders;\n", encoding="utf-8")
    result = subprocess.run([sys.executable, script, str(mig)],
                            capture_output=True, text=True)
    assert result.returncode == 0
