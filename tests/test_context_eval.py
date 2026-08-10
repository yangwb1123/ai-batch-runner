"""Tests for Context Engineering (pbatch/context.py) and Evaluation
Engineering (pbatch/eval.py + the evals/ suite)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def test_keyword_can_match_route_that_also_declares_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "p.md").write_text("x", encoding="utf-8")
    routes = [{"when": {"paths": ["src/payment/**"],
                         "task_contains": ["支付", "payment"]},
               "load": ["docs/p.md"]}]
    manifest = ctx.match_context("实现支付逻辑", None, routes)
    assert manifest["files"] == ["docs/p.md"]
    assert manifest["routes_matched"] is True


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
    assert manifest["routes_matched"] is False


def test_context_cli_json(capsys):
    sys.argv = ["pi-batch.py", "context", "--json", "--paths", "src/order/a.ts",
                "订单模块"]
    ctx.context_main(sys.argv[2:])
    payload = json.loads(capsys.readouterr().out)
    assert "files" in payload
    assert "missing" in payload


@pytest.mark.parametrize("payload", [
    {"routes": "not-a-list"},
    {"routes": [], "typo": True},
    {"routes": [{"when": {}, "load": ["docs/x.md"]}]},
    {"routes": [{"when": {"task_contains": ["x"]}, "load": []}]},
    {"routes": [{"when": {"task_contains": ["x"], "typo": []},
                 "load": ["docs/x.md"]}]},
    {"routes": [{"when": {"task_contains": ["x"]},
                 "load": ["../outside.md"]}]},
    {"routes": [{"when": {"task_contains": ["x"]},
                 "load": ["/tmp/outside.md"]}]},
])
def test_explicit_context_map_schema_fails_closed(tmp_path, payload):
    context_map = tmp_path / "context.json"
    context_map.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid context map"):
        ctx.load_context_map(str(context_map))


def test_missing_explicit_context_map_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="context map not found"):
        ctx.load_context_map(str(tmp_path / "missing.yaml"))


def test_context_cli_can_inject_bounded_document_content(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = tmp_path / "docs" / "payments.md"
    document.parent.mkdir()
    document.write_text("0123456789abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    context_map = tmp_path / "context.json"
    context_map.write_text(json.dumps({"routes": [{
        "when": {"task_contains": ["payment"]},
        "load": ["docs/payments.md"],
    }]}), encoding="utf-8")

    assert ctx.context_main([
        "payment", "--map", str(context_map), "--content", "--max-bytes", "8",
        "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"] == ["docs/payments.md"]
    assert '<evidence source="payments">' in payload["content"]
    assert "01234567" in payload["content"]
    assert "evidence truncated" in payload["content"]
    assert "abcdefghijklmnopqrstuvwxyz" not in payload["content"]


def test_context_cli_resolves_default_map_from_project_root(
        tmp_path, capsys):
    context_dir = tmp_path / "docs" / "agent-context"
    context_dir.mkdir(parents=True)
    context_dir.joinpath("context-map.yaml").write_text(
        "routes:\n"
        "  - when: {task_contains: [payment]}\n"
        "    load: [docs/contract.md]\n", encoding="utf-8")
    (tmp_path / "docs" / "contract.md").write_text("contract", encoding="utf-8")
    assert ctx.context_main([
        "payment", "--root", str(tmp_path), "--content", "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "contract" in payload["content"]


def test_context_cli_rejects_negative_content_budget():
    with pytest.raises(SystemExit) as exc:
        ctx.context_main(["--content", "--max-bytes", "-1"])
    assert exc.value.code == 2


def test_context_rejects_symlink_document(tmp_path):
    outside = tmp_path.parent / "outside-context.md"
    outside.write_text("secret", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "linked.md").symlink_to(outside)
    routes = [{"when": {"task_contains": ["x"]},
               "load": ["docs/linked.md"]}]
    with pytest.raises(ValueError, match="symlink"):
        ctx.match_context("x", context_map=routes,
                          project_root=str(tmp_path))


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


def test_eval_expect_schema_checks_system_type_and_rule_sets():
    suite = {"name": "compat", "cases": [
        {"name": "state", "input": "订单审批流，状态流转，审核流程",
         "expect": {"system_type": "state-machine"}},
        {"name": "rules", "input": "生成一个登录页 demo",
         "expect": {"includes": ["visual-core"],
                    "excludes": ["architecture-budgets"]}},
    ]}
    results = ev.run_suite(suite)
    assert [case_id for case_id, _, _ in results] == ["state", "rules"]
    assert all(ok for _, ok, _ in results), results


def test_eval_empty_unknown_and_extra_fields_fail_closed():
    cases = [
        {"id": "empty", "input": "x", "assert": {}},
        {"id": "unknown", "input": "x", "assert": {"mystery": "x"}},
        {"id": "extra", "input": "x", "assert": {"task_type": "unknown"},
         "typo": True},
    ]
    results = ev.run_suite({"name": "bad-schema", "cases": cases})
    assert len(results) == 3
    assert all(not ok and "schema:" in detail for _, ok, detail in results)


def test_eval_loader_rejects_invalid_case_schema(tmp_path, monkeypatch):
    eval_dir = tmp_path / "evals"
    eval_dir.mkdir()
    eval_dir.joinpath("bad.yaml").write_text(
        "name: bad\ncases:\n  - id: empty\n    input: x\n    assert: {}\n",
        encoding="utf-8")
    monkeypatch.setattr(ev, "EVAL_DIR", eval_dir)
    with pytest.raises(SystemExit) as exc:
        ev.load_eval_files()
    assert exc.value.code == 2


def test_eval_filter_with_no_match_fails(monkeypatch, capsys):
    monkeypatch.setattr(ev, "load_eval_files", lambda: [{
        "name": "one", "cases": [
            {"id": "known", "input": "随便写点什么",
             "assert": {"task_type": "unknown"}},
        ],
    }])
    with pytest.raises(SystemExit) as exc:
        ev.eval_main(["--filter", "missing", "--json"])
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 0
    assert payload["failed"] == 1


def test_eval_coverage_does_not_swallow_failure(monkeypatch, capsys):
    monkeypatch.setattr(ev, "load_eval_files", lambda: [{
        "name": "broken", "cases": [
            {"id": "wrong", "input": "随便写点什么",
             "assert": {"task_type": "frontend_ui"}},
        ],
    }])
    with pytest.raises(SystemExit) as exc:
        ev.eval_main(["--coverage", "--json"])
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1
    assert payload["coverage"][0]["suite"] == "broken"


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
