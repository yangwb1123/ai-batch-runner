"""Tests for 工程知识循环：ADR 决策库 / Change Impact / Project Memory.

adr: 记录/状态机/决策缓存查询/superseded。
impact: 增量路由（受影响面→角色→工作等级→变化成本）、ADR 引用。
project: 记忆骨架初始化/事实登记/索引重建/健康检查。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import adr, impact, project_memory


# ---------------------------------------------------------------------------
# ADR 决策库
# ---------------------------------------------------------------------------

def test_adr_record_and_write(tmp_path, monkeypatch):
    monkeypatch.setattr(adr, "ADR_DIR", tmp_path)
    record = adr.adr_record(
        "文件存储使用 MinIO", "Use MinIO", "大文件/S3 兼容",
        alternatives=["本地磁盘", "OSS"], rejected=["本地磁盘:扩容困难"],
        status="accepted")
    assert record["number"] == 1
    assert record["status"] == "accepted"
    path = adr.write_adr(record, adr_dir=tmp_path)
    assert path.name.startswith("ADR-001-")
    loaded = adr.load_adrs(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["rejected_reasons"] == ["本地磁盘:扩容困难"]


def test_adr_numbering_increments(tmp_path, monkeypatch):
    monkeypatch.setattr(adr, "ADR_DIR", tmp_path)
    first = adr.adr_record("决策一", "A")
    adr.write_adr(first, adr_dir=tmp_path)
    second = adr.adr_record("决策二", "B")
    assert second["number"] == 2


def test_adr_find_decision_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(adr, "ADR_DIR", tmp_path)
    record = adr.adr_record("文件存储使用 MinIO", "Use MinIO",
                            "大文件场景")
    adr.write_adr(record, adr_dir=tmp_path)
    hits = adr.find_adrs("文件存储", tmp_path)
    assert hits and hits[0]["decision"] == "Use MinIO"
    assert adr.find_adrs("无关话题", tmp_path) == []


def test_adr_supersede(tmp_path, monkeypatch):
    monkeypatch.setattr(adr, "ADR_DIR", tmp_path)
    first = adr.adr_record("方案 A", "A")
    adr.write_adr(first, adr_dir=tmp_path)
    second = adr.adr_record("方案 B", "B")
    adr.write_adr(second, adr_dir=tmp_path)
    adr.mark_superseded(1, 2, adr_dir=tmp_path)
    loaded = {r["number"]: r for r in adr.load_adrs(tmp_path)}
    assert loaded[1]["status"] == "superseded"
    assert loaded[1]["superseded_by"] == "ADR-002"


def test_adr_cli_new_and_find(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(adr, "ADR_DIR", tmp_path)
    with pytest.raises(SystemExit):
        adr.adr_main(["new", "--title", "数据库用 PostgreSQL",
                      "--decision", "PostgreSQL", "--status", "accepted"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        adr.adr_main(["find", "数据库"])
    out = capsys.readouterr().out
    assert "PostgreSQL" in out


# ---------------------------------------------------------------------------
# Change Impact Analyzer
# ---------------------------------------------------------------------------

def test_impact_incremental_routing():
    """简单前端修改 → L0/L1 增量路由，不触发架构评审。"""
    report = impact.change_impact("把订单页面按钮对齐")
    assert report["work_level"] in ("L0", "L1")
    assert "architect" not in report["required_agents"]
    assert "Coding → Test" in report["flow"]


def test_impact_high_risk_triggers_review_agents():
    report = impact.change_impact("增加员工绩效导出Excel，生产环境")
    assert report["work_level"] == "L2"
    assert report["change_cost"] in ("high", "critical")
    assert "architect" in report["required_agents"]
    assert "security_engineer" in report["required_agents"]


def test_impact_affected_areas():
    report = impact.change_impact("给员工表增加一个字段并修改接口和页面")
    areas = report["affected_areas"]
    assert "database" in areas and "api" in areas and "frontend" in areas


def test_impact_related_adrs(tmp_path, monkeypatch, capsys):
    """影响分析自动引用决策缓存（不重新讨论已定项）。"""
    monkeypatch.setattr(adr, "ADR_DIR", tmp_path)
    adr.write_adr(adr.adr_record("文件上传使用 MinIO 分片", "Use MinIO"),
                  adr_dir=tmp_path)
    report = impact.change_impact("增加文件上传功能")
    assert report["related_adrs"]
    assert report["related_adrs"][0]["decision"] == "Use MinIO"


def test_impact_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        impact.impact_main(["--task", "增加订单导出", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert "work_level" in report and "change_cost" in report


# ---------------------------------------------------------------------------
# Project Memory
# ---------------------------------------------------------------------------

def test_project_memory_init(tmp_path, monkeypatch):
    memory_dir = tmp_path / ".ai" / "project-memory"
    monkeypatch.setattr(project_memory, "MEMORY_DIR", memory_dir)
    created = project_memory.init_memory(memory_dir)
    assert len(created) == 4  # facts/architecture/decisions/constraints
    # 幂等：再次 init 不创建
    assert project_memory.init_memory(memory_dir) == []


def test_project_facts_with_source(tmp_path, monkeypatch):
    memory_dir = tmp_path / ".ai" / "project-memory"
    monkeypatch.setattr(project_memory, "MEMORY_DIR", memory_dir)
    path = project_memory.add_fact("员工表为 employee_account",
                                   source="schema.sql", memory_dir=memory_dir)
    from pbatch.config import yaml
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assert data["facts"][0]["statement"] == "员工表为 employee_account"
    assert data["facts"][0]["source"] == "schema.sql"  # 事实带来源


def test_project_memory_report_and_index(tmp_path, monkeypatch):
    memory_dir = tmp_path / ".ai" / "project-memory"
    adr_dir = tmp_path / "docs" / "adr"
    monkeypatch.setattr(project_memory, "MEMORY_DIR", memory_dir)
    project_memory.init_memory(memory_dir)
    from pbatch import adr as adr_mod
    adr_mod.write_adr(adr_mod.adr_record("决策 X", "X"), adr_dir=adr_dir)
    # 索引前：stale
    report = project_memory.memory_report(memory_dir, adr_dir)
    assert report["adr_index_stale"] is True
    # 重建索引后：一致
    project_memory.index_adrs(memory_dir, adr_dir)
    report2 = project_memory.memory_report(memory_dir, adr_dir)
    assert report2["adr_index_stale"] is False
    assert report2["adr_count"] == 1


def test_project_verify_cli(tmp_path, monkeypatch, capsys):
    memory_dir = tmp_path / ".ai" / "project-memory"
    monkeypatch.setattr(project_memory, "MEMORY_DIR", memory_dir)
    with pytest.raises(SystemExit) as exc:
        project_memory.project_main(["verify", "--json"])
    assert exc.value.code == 1  # 骨架缺失 → fail closed
    report = json.loads(capsys.readouterr().out)
    assert report["skeleton_complete"] is False
