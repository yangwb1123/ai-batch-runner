"""Tests for Capability Registry（内核冻结第一件）与 Health Score.

capabilities: 注册表完整性（owner 存在/测试关联/依赖预算）、domain 覆盖、
check fail closed、CLI 输出。
health: Health Score 组件与扣分逻辑。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pbatch import capabilities, health


def test_registry_covers_all_subcommands():
    from pbatch.cli import _SUBCOMMANDS
    registry = capabilities.build_registry()
    ids = {c["id"] for c in registry}
    for name in _SUBCOMMANDS:
        assert name in ids


def test_registry_domains_valid():
    for capability in capabilities.build_registry():
        assert capability["domain"] in capabilities.CAPABILITY_DOMAINS
        assert capability["risk"] in ("low", "medium", "high")
        assert capability["entry"].startswith("pi-batch ")


def test_product_capability_is_registered_as_low_risk_planning():
    product = next(item for item in capabilities.build_registry()
                   if item["id"] == "product")
    assert product["domain"] == "planning"
    assert product["risk"] == "low"
    assert product["output"] == "有界产品方向简报"


def test_registry_check_valid():
    """owner 模块存在 + 依赖预算内（自检：0 超限）。"""
    report = capabilities.registry_check()
    assert report["valid"] is True
    assert report["stats"]["total"] >= 39
    assert report["stats"]["deps_over_budget"] == 0


def test_registry_check_is_independent_of_process_cwd(tmp_path, monkeypatch):
    """Installed commands validate their package owners outside a checkout."""
    monkeypatch.chdir(tmp_path)
    report = capabilities.registry_check()
    assert report["valid"] is True
    assert report["stats"]["total"] >= 39


def test_registry_check_detects_missing_owner(tmp_path, monkeypatch):
    registry = [{"id": "ghost", "domain": "ops",
                 "owner": {"module": "no_such_module", "target": "x:y"},
                 "tests": [], "module_dependencies": 0}]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pbatch").mkdir()
    report = capabilities.registry_check(registry)
    assert report["valid"] is False
    assert any("owner 模块" in p for p in report["problems"])


def test_registry_check_detects_dep_budget(tmp_path, monkeypatch):
    registry = [{"id": "big", "domain": "ops",
                 "owner": {"module": "m", "target": "m:x"},
                 "tests": ["t.py"], "module_dependencies": 20}]
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pbatch").mkdir()
    report = capabilities.registry_check(registry)
    assert report["valid"] is False
    assert any("依赖 20 > 预算" in p for p in report["problems"])


def test_capabilities_cli_list_json(capsys):
    with pytest.raises(SystemExit) as exc:
        capabilities.capabilities_main(["list", "--json"])
    assert exc.value.code == 0
    registry = json.loads(capsys.readouterr().out)
    assert registry and registry[0]["id"]


def test_capabilities_cli_get_unknown(capsys):
    with pytest.raises(SystemExit) as exc:
        capabilities.capabilities_main(["get", "--id", "no-such"])
    assert exc.value.code == 2


def test_capabilities_cli_check(capsys):
    with pytest.raises(SystemExit) as exc:
        capabilities.capabilities_main(["check", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is True


def test_capability_grant_binds_identity_scope_and_expiry():
    grant = capabilities.CapabilityGrant(
        agent_id="agent-a", node_id="node-1", actions=["devices.run"],
        resources=["host-*"], environment="trusted-local",
        expires_at="2099-01-01T00:00:00Z", max_side_effect_class="sandboxed",
        capability="devices")
    ok, reasons = capabilities.authorize_grant(
        "devices", grant, 4, "agent-a", "node-1", "devices.run", "host-7",
        "trusted-local", "sandboxed", device_zone="trusted-local",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert ok is True and reasons == []
    denied, reasons = capabilities.authorize_grant(
        "devices", grant, 4, "agent-a", "other-node", "devices.run", "host-7",
        "trusted-local", "sandboxed", device_zone="trusted-local",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert denied is False and any("node_id" in reason for reason in reasons)
    expired, reasons = capabilities.authorize_grant(
        "devices", grant, 4, "agent-a", "node-1", "devices.run", "host-7",
        "trusted-local", "sandboxed", device_zone="trusted-local",
        now=datetime(2100, 1, 1, tzinfo=timezone.utc))
    assert expired is False and any("过期" in reason for reason in reasons)


def test_capabilities_authorize_cli(tmp_path, capsys):
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps({
        "agentId": "agent-a", "nodeId": "node-1", "actions": ["world"],
        "resources": ["repo-1"], "environment": "trusted-local",
        "expiresAt": "2099-01-01T00:00:00Z",
        "maxSideEffectClass": "read_only", "capability": "world",
    }), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        capabilities.capabilities_main([
            "authorize", "--grant", str(grant), "--capability", "world",
            "--agent-level", "2", "--agent-id", "agent-a", "--node-id", "node-1",
            "--action", "world", "--resource", "repo-1",
            "--environment", "trusted-local", "--device-zone", "trusted-local",
            "--json"])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["allowed"] is True


def test_health_score_components():
    score = health._health_score({"designintelligence": 0,
                                  "knowledge": 0, "backendquality": 0},
                                 quality=0, tests=900)
    assert score["score"] >= 95
    assert score["breakdown"]["architecture_cycles"] == 0
    assert score["breakdown"]["budget_overruns"] == 0


def test_health_score_penalties():
    bad = health._health_score({"designintelligence": 2,
                                "knowledge": 1, "backendquality": 3},
                               quality=4, tests=100)
    assert bad["score"] < 60
    assert bad["grade"] in ("C", "D")


def test_health_score_in_report():
    report = health.health_report()
    assert "score" in report
    assert report["score"]["grade"] in ("A", "B", "C", "D")
