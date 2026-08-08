"""Tests for 自治层收尾：retro 自动动作 / Desired-State Controller / 信任矩阵.

retro: 失败模式 → 自动改进动作 + 反思账本聚合。
desired: 声明期望 → 服务端观察 → 补/撤直到 converged；幂等。
trust: 能力信任等级推导、授权矩阵（Agent×设备区）、CLI。
critic: prompts/critic.md 角色存在且输出格式与 reflect 兼容。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pbatch import capabilities, fabric, fabric_runner, fabric_server, retro
from pbatch.fabric_rpc import RpcClient


# ---------------------------------------------------------------------------
# retro：自动改进动作
# ---------------------------------------------------------------------------

def test_generate_actions_from_failures():
    counts = {"agent_rejected": 3, "gate_rejected": 2, "lock_refused": 1}
    actions = retro._generate_actions(counts)
    types = {a["type"] for a in actions}
    assert "agent_rejected" in types
    assert "gate_rejected" in types
    assert any(a["rule_id"] == "GATE-REPEAT" for a in actions)


def test_generate_actions_no_failures():
    assert retro._generate_actions({"task_ok": 5}) == []


def test_reflection_actions_repeat_findings(tmp_path, monkeypatch):
    state_dir = tmp_path / ".pi-batch"
    state_dir.mkdir()
    reflections = state_dir / "reflections.jsonl"
    records = [
        {"findings": [{"type": "missed_requirement",
                       "severity": "warning"}]},
        {"findings": [{"type": "missed_requirement",
                       "severity": "warning"}]},
        {"findings": [{"type": "wrong_assumption",
                       "severity": "critical"}]},
    ]
    reflections.write_text("\n".join(
        json.dumps(r) for r in records) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    actions = retro._reflection_actions()
    assert any(a["type"] == "reflection_critical" for a in actions)
    assert any(a["type"] == "repeat_finding" and "missed_requirement"
               in a.get("finding", "") for a in actions)


def test_retro_cli_actions(tmp_path, capsys, monkeypatch):
    log = tmp_path / "ops.log"
    log.write_text("[ERROR] GATE REJECTED: verdict FAIL\n"
                   "[ERROR] agent output REJECTED: rate_limit_error\n",
                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code = retro.retro_main([str(log), "--actions", "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["actions"]
    assert any(a["type"] == "gate_rejected" for a in report["actions"])


# ---------------------------------------------------------------------------
# Desired-State Controller
# ---------------------------------------------------------------------------

@pytest.fixture()
def control_plane(tmp_path):
    httpd, port = fabric_server.start_server(0, str(tmp_path / "state"),
                                             auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"httpd": httpd, "port": port, "state_dir": tmp_path / "state"}
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _client(control_plane, name="node-d"):
    key = fabric_runner.load_or_create_key(str(control_plane["state_dir"]),
                                           name)
    client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                       key["secret"], key["device_id"])
    assert client.register(name, fabric.local_probe())["approved"] is True
    return client


def test_desired_scale_up_to_replicas(control_plane):
    client = _client(control_plane)
    report = client.desired_reconcile([
        {"name": "svc-a", "replicas": 2, "cmd": ["echo", "x"],
         "effect_class": "read_only"}])
    assert report["converged"] is True
    assert report["results"][0]["desired"] == 2
    assert len(report["results"][0]["actions"]) == 2
    assert all("scale_up" in a for a in report["results"][0]["actions"])
    # 幂等：再 reconcile 不重复补（已经 2 个）
    report2 = client.desired_reconcile([
        {"name": "svc-a", "replicas": 2, "cmd": ["echo", "x"]}])
    assert report2["converged"] is True
    assert report2["results"][0]["actions"] == []


def test_desired_scale_down_and_up(control_plane):
    client = _client(control_plane)
    client.desired_reconcile([
        {"name": "svc-b", "replicas": 3, "cmd": ["echo", "y"]}])
    # 期望降到 1 → 取消 2 个
    report = client.desired_reconcile([
        {"name": "svc-b", "replicas": 1, "cmd": ["echo", "y"]}])
    result = report["results"][0]
    assert result["diff"] == -2
    assert sum("scale_down" in a for a in result["actions"]) == 2
    assert result["actual"] == 1
    # 回升到 2 → 补 1
    report2 = client.desired_reconcile([
        {"name": "svc-b", "replicas": 2, "cmd": ["echo", "y"]}])
    assert report2["results"][0]["diff"] == 1


def test_desired_spec_file_parsing(tmp_path):
    from pbatch.fabric_desired import load_desired_specs
    spec_file = tmp_path / "desired.yaml"
    spec_file.write_text("""
services:
  - name: model-service
    replicas: 3
    cmd: ["python3", "serve.py"]
    effect_class: sandboxed
""", encoding="utf-8")
    specs = load_desired_specs(str(spec_file))
    assert specs[0]["name"] == "model-service"
    assert specs[0]["replicas"] == 3
    assert specs[0]["cmd"] == ["python3", "serve.py"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("services: []", encoding="utf-8")
    with pytest.raises(ValueError):
        load_desired_specs(str(bad))


def test_desired_marker_isolation(control_plane):
    """不同服务互不干扰：marker 前缀隔离归属。"""
    client = _client(control_plane)
    client.desired_reconcile([
        {"name": "svc-x", "replicas": 2, "cmd": ["echo", "x"]},
        {"name": "svc-y", "replicas": 1, "cmd": ["echo", "y"]}])
    # svc-x 降为 1：只影响 svc-x，不影响 svc-y
    report = client.desired_reconcile([
        {"name": "svc-x", "replicas": 1, "cmd": ["echo", "x"]}])
    assert report["results"][0]["diff"] == -1
    y_tasks = [t for t in control_plane["httpd"].control.tasks.values()
               if "__desired__:svc-y" in " ".join(t.get("argv", []))]
    assert len(y_tasks) == 1  # svc-y 未受影响


# ---------------------------------------------------------------------------
# 信任等级授权矩阵
# ---------------------------------------------------------------------------

def test_trust_level_by_domain():
    registry = capabilities.build_registry()
    by_id = {c["id"]: c for c in registry}
    assert by_id["atomize"]["trust_level"] == 1   # reasoning/low
    assert by_id["check"]["trust_level"] == 3     # verification
    assert by_id["devices"]["trust_level"] == 4   # device/medium
    assert by_id["campaign"]["trust_level"] == 3  # planning/medium → 3


def test_authorized_matrix():
    capability = {"domain": "device", "risk": "medium",
                  "trust_level": 4}
    ok, _ = capabilities.authorized(capability, agent_level=4)
    assert ok is True
    ok2, reason = capabilities.authorized(capability, agent_level=2)
    assert ok2 is False and "需要信任等级 4" in reason
    # 不可信设备区拒绝高等级
    ok3, reason3 = capabilities.authorized(capability, agent_level=4,
                                           device_zone="untrusted-external")
    assert ok3 is False and "不可信设备区" in reason3


def test_trust_cli_level1_analysis_only(capsys):
    with pytest.raises(SystemExit) as exc:
        capabilities.capabilities_main(["trust", "--agent-level", "1",
                                        "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["allowed"] >= 20  # 分析/规划/只读能力
    assert report["denied"] > 0     # 执行/设备能力被拒


def test_trust_cli_untrusted_zone(capsys):
    with pytest.raises(SystemExit) as exc:
        capabilities.capabilities_main(["trust", "--agent-level", "4",
                                        "--device-zone",
                                        "untrusted-external", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    denied = {d["id"] for d in report["denied_capabilities"]}
    assert "devices" in denied  # 设备操作在不可信区被拒


# ---------------------------------------------------------------------------
# Critic 角色
# ---------------------------------------------------------------------------

def test_critic_role_exists():
    path = Path("prompts/critic.md")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "反方审查" in text or "Critic" in text
    assert "severity" in text  # 输出格式与 reflect Finding 兼容
    assert "证据" in text
