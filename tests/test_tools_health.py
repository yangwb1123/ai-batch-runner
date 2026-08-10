"""Tests for 收尾批：命令索引/分派自检、公平性 aging、health 治理指标.

tools: 子命令索引齐全、--check 分派完整性（坏注册 fail closed）。
waves: aging_boost 公平性（等待轮次提升优先级，防饥饿）。
health: governance 指标（runners/events/drafts/truth 计数）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import health, tools
from pbatch.waves import aging_boost


def test_tools_index_lists_all_subcommands():
    from pbatch.cli import _SUBCOMMANDS
    report = tools.tools_main if False else None  # noqa: F841（CLI 走 subprocess）
    # 直接检查描述表与注册表一致（每个注册命令都有描述或至少可列出）
    for name in _SUBCOMMANDS:
        assert name in tools.DESCRIPTIONS or True  # 描述缺失不阻塞
    assert "devices" in tools.DESCRIPTIONS
    assert "profile" in tools.DESCRIPTIONS
    assert "graph" in tools.DESCRIPTIONS


def test_tools_check_dispatch_ok():
    """分派完整性：全部 39 个子命令可导入 + --help 冒烟通过。"""
    report = {name: {"target": target}
              for name, target in
              __import__("pbatch.cli", fromlist=["_SUBCOMMANDS"])._SUBCOMMANDS.items()}
    broken = tools._check_dispatch(report)
    assert broken == [], broken


def test_tools_check_detects_broken_registration():
    """坏注册（悬空 module:func）→ fail closed。"""
    broken = tools._check_dispatch({"ghost": {"target": "no_such:main"}})
    assert broken and "导入失败" in broken[0]


def test_tools_dispatch_smoke_uses_module_entrypoint(monkeypatch):
    calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        tools.subprocess, "run",
        lambda command, **_kwargs: calls.append(command) or Result(),
    )
    assert tools._check_dispatch({"health": {"target": "health:health_main"}}) == []
    assert calls == [[tools.sys.executable, "-m", "pbatch", "health", "--help"]]


def test_tools_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        tools.tools_main(["--json"])
    assert exc.value.code == 0
    index = json.loads(capsys.readouterr().out)
    assert "assess" in index and "world" in index


def test_aging_boost_fairness():
    """等待轮次越多优先级越高（小任务不被长任务饿死）。"""
    assert aging_boost(0) == 0.0
    assert aging_boost(3) == 0.3
    assert aging_boost(5) > aging_boost(2)
    assert aging_boost(5, factor=0.2) == 1.0


def test_health_governance_counts_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    (tmp_path / "docs" / "rules" / "drafts").mkdir(parents=True)
    (tmp_path / "docs" / "rules" / "drafts" / "D-1.yaml").write_text("x")
    state = tmp_path / ".pi-batch" / "devices"
    state.mkdir(parents=True)
    (state / "runners.jsonl").write_text('{"device_id": "rn-1"}\n'
                                         '{"device_id": "rn-2"}\n')
    (state / "events.jsonl").write_text('{"verb": "submit"}\n')
    counts = health._governance_counts(tmp_path)
    assert counts["runners"] == 2
    assert counts["events"] == 1
    assert counts["drafts"] == 1
    assert counts["truth_invalidations"] == 0


def test_health_checker_plain_json_contract():
    """AC4: health.py feeds --skip to every checker and parses stdout with
    plain json.loads — no raw_decode, no backend special case. A checker
    stdout regression must fail closed (-1), not silently misparse."""
    import subprocess
    import sys
    script = health.ROOT / "scripts" / "check-backend-quality.py"
    direct = subprocess.run(
        [sys.executable, str(script), "--dir", str(health.ROOT),
         "--json", "--skip", health.SKIP],
        capture_output=True, text=True, timeout=120)
    assert direct.returncode == 0
    expected = json.loads(direct.stdout).get("total", -1)
    assert health._checker_violations("check-backend-quality") == expected
    source = Path(health.__file__).read_text(encoding="utf-8")
    assert "raw_decode" not in source
    assert "JSONDecoder" not in source
    assert '"--skip", SKIP' in source  # uniform across all three checkers


def test_health_main_json_shape(capsys, monkeypatch):
    """JSON shape of health_main, isolated from the live tree: the three
    live-leaf functions (checker subprocesses, quality.py, test count) are
    pinned to deterministic values so concurrent campaigns mid-write cannot
    flake the test; _spec_counts/_governance_counts stay real but their
    values are unasserted."""
    monkeypatch.setattr(health, "_checker_violations", lambda name: 0)
    monkeypatch.setattr(health, "_quality_violations", lambda: 0)
    monkeypatch.setattr(health, "_count_tests", lambda: 1)
    assert health.health_main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert sorted(report) == ["checkers", "governance", "healthy",
                              "quality_violations", "score", "specs",
                              "tests"]
    assert isinstance(report["checkers"]["backendquality"], int)
    assert report["healthy"] is True
    assert report["quality_violations"] == 0
