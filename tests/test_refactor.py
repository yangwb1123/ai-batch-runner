"""Tests for God File 重构分析器（软件工程规范可执行化）.

度量（行数/函数/圈复杂度分级）、职责污染识别（SRP）、模式信号
（Strategy/DI/AOP）、拆分建议与迁移计划、真实文件分析、CLI。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import refactor

GOD_FILE = '''class OrderService:
    def create_order(self, user, stock, payment):
        if user:
            if stock:
                if payment:
                    return "ok"
        return "fail"

    def calculate_price(self, order, type):
        if type == "vip":
            return 80
        elif type == "normal":
            return 100
        elif type == "employee":
            return 90
        elif type == "channel":
            return 85
        return 100

    def send_email(self, order):
        try:
            self._smtp.send(order)
        except Exception:
            pass

    def write_audit(self, order):
        try:
            self._audit.log(order)
        except Exception:
            pass

    def generate_pdf(self, order):
        db = Mysql()
        try:
            rows = db.query(order)
        except Exception:
            return []
        return rows

    def upload_file(self, order):
        storage = Redis()
        return storage.put(order)
'''

CLEAN_FILE = '''def add(a, b):
    return a + b
'''


def _analyze(tmp_path, content, name="svc.py"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return refactor.analyze_file(str(path)), path


def test_metrics_function_count_and_complexity(tmp_path):
    report, _ = _analyze(tmp_path, GOD_FILE)
    m = report["metrics"]
    assert m["functions"] == 6  # 含类方法（修复后）
    assert m["top_complexity"] >= 4  # create_order 三层 if
    complex_func = [f for f in report["functions"]
                    if f["name"] == "create_order"]
    assert complex_func and complex_func[0]["complexity"] >= 4


def test_responsibility_pollution_detected(tmp_path):
    """职责污染：订单/价格/通知/审计/文件 5 域 → SRP 违规。"""
    report, _ = _analyze(tmp_path, GOD_FILE)
    domains = report["responsibilities"]
    assert len(domains) >= 4
    critical = [s for s in report["suggestions"]
                if s["type"] == "responsibility_pollution"]
    assert critical and "拆分为" in critical[0]["action"]
    assert report["risk"] == "HIGH"


def test_strategy_signal_on_branches(tmp_path):
    report, _ = _analyze(tmp_path, GOD_FILE)
    strategy = [s for s in report["signals"] if s["pattern"] == "Strategy"]
    assert strategy and "分支" in strategy[0]["evidence"]


def test_di_signal_on_direct_construction(tmp_path):
    report, _ = _analyze(tmp_path, GOD_FILE)
    di = [s for s in report["signals"] if s["pattern"] == "DI"]
    assert di
    assert "Mysql" in di[0]["evidence"] and "Redis" in di[0]["evidence"]


def test_aop_signal_on_repeated_try(tmp_path):
    report, _ = _analyze(tmp_path, GOD_FILE)
    aop = [s for s in report["signals"] if s["pattern"] == "AOP"]
    assert aop and "try/except" in aop[0]["evidence"]


def test_clean_file_no_suggestions(tmp_path):
    report, _ = _analyze(tmp_path, CLEAN_FILE)
    assert report["risk"] == "LOW"
    assert report["suggestions"] == []
    assert report["migration_plan"] == []
    assert report["metrics"]["functions"] == 1


def test_god_function_must_split(tmp_path):
    """圈复杂度 > 20 → must_split。"""
    body = "\n".join(f"        if cond_{i}:\n            pass"
                      for i in range(25))
    content = f"def monster():\n{body}\n"
    report, _ = _analyze(tmp_path, content, name="monster.py")
    func = report["functions"][0]
    assert func["level"] == "must_split"
    assert any(s["type"] == "god_function"
               and s["severity"] == "critical"
               for s in report["suggestions"])


def test_line_level_thresholds(tmp_path):
    content = "\n".join(f"x{i} = {i}" for i in range(320))
    report, _ = _analyze(tmp_path, content, name="big.py")
    assert report["metrics"]["line_level"] == "warning"
    content2 = "\n".join(f"x{i} = {i}" for i in range(650))
    report2, _ = _analyze(tmp_path, content2, name="huge.py")
    assert report2["metrics"]["line_level"] == "critical"
    assert any(s["type"] == "god_file" for s in report2["suggestions"])


def test_migration_plan_has_test_protection_first(tmp_path):
    report, _ = _analyze(tmp_path, GOD_FILE)
    assert report["migration_plan"]
    assert "测试保护" in report["migration_plan"][0]
    assert any("ADR" in step for step in report["migration_plan"])


def test_non_python_file_fails_closed(tmp_path):
    path = tmp_path / "app.ts"
    path.write_text("const x = 1;", encoding="utf-8")
    report = refactor.analyze_file(str(path))
    assert "error" in report


def test_missing_file_fails_closed(tmp_path):
    report = refactor.analyze_file(str(tmp_path / "nope.py"))
    assert "error" in report


def test_refactor_cli_json(tmp_path, capsys):
    path = tmp_path / "god.py"
    path.write_text(GOD_FILE, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        refactor.refactor_main(["--file", str(path), "--json"])
    assert exc.value.code == 1  # HIGH 风险 → 非零退出
    report = json.loads(capsys.readouterr().out)
    assert report["risk"] == "HIGH"
    assert report["metrics"]["functions"] == 6


def test_refactor_real_repo_file(tmp_path):
    """分析本仓库真实文件（dogfooding）：cli_batch 等大模块。"""
    report = refactor.analyze_file("pbatch/cli_batch.py")
    assert "error" not in report
    assert report["metrics"]["functions"] > 10
    assert report["metrics"]["top_complexity"] >= 1
