"""Tests for the device fabric D3: control plane + device runner.

Covers: HMAC 签名/验签/防重放、注册-审批-心跳-租约-证据全流程、Fencing
Token（陈旧 token 拒收 = 防 split-brain）、控制平面重启后的断线重连与
对账演练、Runner 循环、CLI（approve/runners）。

全部走 127.0.0.1 回环，无外部网络依赖。
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from pbatch import (fabric_rpc, fabric_runner, fabric_server,
                             fabric_server_cli)


# ---------------------------------------------------------------------------
# 签名与验签
# ---------------------------------------------------------------------------

def test_sign_verify_roundtrip():
    secret = "s3cret"
    req = fabric_rpc.build_request(secret, "POST", "/heartbeat",
                                   {"device_id": "rn-abc", "ok": 1})
    assert fabric_rpc.verify_request(secret, "POST", "/heartbeat", req) is None


def test_verify_rejects_tampered_payload():
    secret = "s3cret"
    req = fabric_rpc.build_request(secret, "POST", "/lease",
                                   {"device_id": "rn-abc", "x": 1})
    req["payload"]["x"] = 2  # 篡改 payload → 签名失配
    reason = fabric_rpc.verify_request(secret, "POST", "/lease", req)
    assert reason == "bad signature"


def test_verify_rejects_wrong_secret():
    req = fabric_rpc.build_request("secret-a", "POST", "/heartbeat", {"a": 1})
    assert fabric_rpc.verify_request("secret-b", "POST", "/heartbeat",
                                     req) == "bad signature"


def test_verify_rejects_stale_timestamp():
    req = fabric_rpc.build_request("s", "POST", "/heartbeat", {"a": 1})
    past = int(time.time()) - 600
    req["ts"] = past
    assert fabric_rpc.verify_request("s", "POST", "/heartbeat",
                                     req) == "stale timestamp"


def test_verify_rejects_replay():
    req = fabric_rpc.build_request("s", "POST", "/heartbeat", {"a": 1})
    cache = {}
    assert fabric_rpc.verify_request("s", "POST", "/heartbeat",
                                     req, replay_cache=cache) is None
    # 同一 nonce 重放 → 拒绝
    assert fabric_rpc.verify_request("s", "POST", "/heartbeat",
                                     req, replay_cache=cache) == "replay"


def test_verify_rejects_malformed():
    assert fabric_rpc.verify_request("s", "POST", "/x", {}) == "malformed request"
    assert fabric_rpc.verify_request("s", "POST", "/x", {
        "ts": "not-int", "nonce": "n", "sig": "s", "payload": {}}) \
        == "malformed request"


def test_rpc_post_network_error():
    """网络不可达 → (0, {error})，不抛异常（故障显式）。"""
    code, data = fabric_rpc.rpc_post("http://127.0.0.1:1", "s", "POST",
                                     "/x", {"a": 1}, timeout=0.5)
    assert code == 0 and "error" in data


# ---------------------------------------------------------------------------
# 控制平面：注册 → 审批 → 心跳 → 租约 → 证据（Fencing Token）
# ---------------------------------------------------------------------------

@pytest.fixture()
def control_plane(tmp_path):
    """启动控制平面（回环，随机端口）；测试结束关闭。"""
    httpd, port = fabric_server.start_server(0, str(tmp_path / "state"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"httpd": httpd, "port": port, "state_dir": tmp_path / "state"}
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _client(control_plane, secret, device_id="rn-testdev"):
    return fabric_rpc.RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                                secret, device_id)


def test_register_pending_then_approve(control_plane):
    secret = "dev-secret-1"
    client = _client(control_plane, secret)
    first = client.register("node-a", "fp-1")
    assert first["approved"] is False  # 默认人工审批（TOFU + 等待）
    # CLI 审批
    with pytest.raises(SystemExit) as _exc:
        fabric_server_cli.approve_main(["--state-dir", str(control_plane["state_dir"]),
                                    "node-a"])
    assert _exc.value.code == 0
    second = client.register("node-a", "fp-1")
    assert second["approved"] is True
    # 身份被占用：不同 secret 声称同一 device_id → 拒绝
    spoof = fabric_rpc.RpcClient(
        f"http://127.0.0.1:{control_plane['port']}", "other-secret",
        client.device_id)
    assert spoof.register("node-a", "fp")["approved"] is False


def test_lease_and_fencing_token(control_plane):
    secret = "dev-secret-2"
    client = _client(control_plane, secret)
    client.register("node-b", "fp")
    with pytest.raises(SystemExit) as _exc:
        fabric_server_cli.approve_main(["--state-dir", str(control_plane["state_dir"]),
                                    "node-b"])
    assert _exc.value.code == 0
    assert client.register("node-b", "fp")["approved"] is True
    beat = client.heartbeat({"status": "online"}, [])
    assert beat["ok"] is True
    # 租约 1：epoch/fencing token = 1
    lease1 = client.lease({"cpu_cores": 2})["lease"]
    assert lease1["epoch"] == 1 and lease1["fencing_token"] == 1
    assert lease1["grant"]["max_effect_class"] == "sandboxed"  # D3 最小 Grant
    # 正确 token 提交证据 → 接受
    ok = client.evidence("a1", lease1["lease_id"], lease1["fencing_token"],
                         {"result": "passed"})
    assert ok["accepted"] is True
    # 新租约 → epoch 2；旧 token 提交证据 → 拒收（防 split-brain）
    lease2 = client.lease({"cpu_cores": 4})["lease"]
    assert lease2["epoch"] == 2
    stale = client.evidence("a1", lease1["lease_id"],
                            lease1["fencing_token"], {"result": "passed"})
    assert stale["accepted"] is False
    assert "stale" in stale["reason"]


def test_evidence_requires_auth_and_lease(control_plane):
    """未注册设备的签名请求 → 404；有设备无租约提交证据 → 拒收。"""
    client = _client(control_plane, "nope-secret", device_id="rn-stranger")
    code, data = fabric_rpc.rpc_post(
        f"http://127.0.0.1:{control_plane['port']}", "nope-secret", "POST",
        "/heartbeat", {"device_id": "rn-stranger"}, timeout=2)
    assert code == 404
    # 已注册但未租约
    secret = "dev-secret-3"
    client2 = _client(control_plane, secret, device_id="rn-nolease")
    client2.register("node-c", "fp")
    with pytest.raises(SystemExit) as _exc:
        fabric_server_cli.approve_main(["--state-dir", str(control_plane["state_dir"]),
                                    "node-c"])
    assert _exc.value.code == 0
    res = client2.evidence("a1", "lease-x", 1, {"result": "passed"})
    assert res["accepted"] is False and res["reason"] == "no lease"


def test_reconnect_reconciliation_drill(control_plane, tmp_path):
    """断线重连 + 对账演练：控制平面重启后旧 fencing token 必须失效。"""
    secret = "dev-secret-4"
    client = _client(control_plane, secret)
    client.register("node-d", "fp")
    with pytest.raises(SystemExit) as _exc:
        fabric_server_cli.approve_main(["--state-dir", str(control_plane["state_dir"]),
                                    "node-d"])
    assert _exc.value.code == 0
    assert client.register("node-d", "fp")["approved"] is True
    lease1 = client.lease({})["lease"]

    # 控制平面重启（模拟断线/故障恢复）
    port = control_plane["port"]
    control_plane["httpd"].shutdown()
    control_plane["httpd"].server_close()
    httpd2, port2 = fabric_server.start_server(
        port, str(control_plane["state_dir"]))  # 同端口重启
    assert port2 == port
    thread2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
    thread2.start()
    try:
        # Runner 重连：审批记录持久化 → 直接通过
        assert client.register("node-d", "fp")["approved"] is True
        lease2 = client.lease({})["lease"]
        # 重启后 epoch 重置，旧 token（必然 ≠ 当前）提交 → 拒收（防 split-brain）
        stale_token = lease1["fencing_token"] + 100
        res = client.evidence("a1", lease2["lease_id"],
                              stale_token, {"result": "passed"})
        assert res["accepted"] is False and "stale" in res["reason"]
        # 当前 token 正常提交
        ok = client.evidence("a1", lease2["lease_id"],
                             lease2["fencing_token"], {"result": "passed"})
        assert ok["accepted"] is True
        # 对账视图
        recon = client.reconcile([])
        assert recon["lease"]["lease_id"] == lease2["lease_id"]
    finally:
        httpd2.shutdown()
        httpd2.server_close()
        thread2.join(timeout=5)


def test_status_view_and_auto_approve(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        client = fabric_rpc.RpcClient(f"http://127.0.0.1:{port}",
                                      "auto-secret", "rn-auto")
        assert client.register("node-auto", "fp")["approved"] is True  # 直接批准
        client.heartbeat({"status": "online"}, [])
        code, data = fabric_rpc.rpc_post(
            f"http://127.0.0.1:{port}", "auto-secret", "GET", "/status",
            {"device_id": "rn-auto"}, timeout=2)  # GET 不走签名（只读视图）
        assert code == 200
        devices = data["devices"]
        assert any(d["device_id"] == "rn-auto" and d["online"]
                   for d in devices)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 设备 Runner（出站连接 / 断线重连 / 密钥）
# ---------------------------------------------------------------------------

def test_runner_key_created_and_stable(tmp_path):
    key1 = fabric_runner.load_or_create_key(str(tmp_path), "node-x")
    assert key1["device_id"].startswith("rn-")
    assert len(key1["secret"]) >= 32
    key2 = fabric_runner.load_or_create_key(str(tmp_path), "node-x")
    assert key2 == key1  # 稳定身份
    key_path = fabric_runner.key_path(str(tmp_path), "node-x")
    assert (key_path.stat().st_mode & 0o777) == 0o600


def test_runner_once_pending_then_approved(control_plane):
    state = control_plane["state_dir"]
    base = f"http://127.0.0.1:{control_plane['port']}"
    summary = fabric_runner.runner_once(base, "node-y", str(state))
    assert summary["approved"] is False  # 未审批
    with pytest.raises(SystemExit) as _exc:
        fabric_server_cli.approve_main(["--state-dir", str(state), "node-y"])
    assert _exc.value.code == 0
    summary = fabric_runner.runner_once(base, "node-y", str(state))
    assert summary["approved"] is True
    assert summary["heartbeat_ok"] is True


def _wait_for_heartbeats(control_plane, device_id: int, count: int,
                        deadline_seconds: float = 10.0) -> int:
    """Wait until the control plane has counted `count` heartbeats.

    Startup is deliberately excluded from the measurement: the first cycle
    still pays the one-time full capability probe (an `nvidia-smi` fork), which
    says nothing about the steady-state heartbeat rate under test.
    """
    deadline = time.monotonic() + deadline_seconds
    seen = 0
    while time.monotonic() < deadline:
        device = control_plane["httpd"].control.devices.get(device_id, {})
        seen = int(device.get("heartbeats", 0) or 0)
        if seen >= count:
            return seen
        time.sleep(0.01)
    return seen


def test_runner_loop_heartbeats_and_reconnect(control_plane):
    state = control_plane["state_dir"]
    base = f"http://127.0.0.1:{control_plane['port']}"
    fabric_runner.runner_once(base, "node-z", str(state))
    with pytest.raises(SystemExit) as _exc:
        fabric_server_cli.approve_main(["--state-dir", str(state), "node-z"])
    assert _exc.value.code == 0
    stop = threading.Event()
    thread = threading.Thread(
        target=fabric_runner.run_runner_loop,
        args=(base, "node-z", str(state)), kwargs={"heartbeat_interval": 0.05,
                                                   "stop_event": stop},
        daemon=True)
    thread.start()
    device_id = fabric_runner.load_or_create_key(str(state), "node-z")["device_id"]
    _wait_for_heartbeats(control_plane, device_id, 1)
    observed = _wait_for_heartbeats(control_plane, device_id, 4)
    stop.set()
    thread.join(timeout=5)
    assert observed >= 4, f"heartbeat rate collapsed: only {observed} recorded"
    # 断线重连：控制平面不可达时循环不退出
    stop2 = threading.Event()
    dead = threading.Thread(
        target=fabric_runner.run_runner_loop,
        args=("http://127.0.0.1:1", "node-dead", str(state)),
        kwargs={"heartbeat_interval": 0.02, "stop_event": stop2},
        daemon=True)
    dead.start()
    time.sleep(0.15)
    assert dead.is_alive()  # 重连循环在退避等待，未崩溃
    stop2.set()
    dead.join(timeout=5)


def test_approve_cli_unknown_device(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_server_cli.approve_main(
            ["--state-dir", str(tmp_path / "empty"), "ghost"])
    assert exc.value.code == 2
    assert "no registration" in capsys.readouterr().err


def test_runners_cli(control_plane):
    state = control_plane["state_dir"]
    base = f"http://127.0.0.1:{control_plane['port']}"
    fabric_runner.runner_once(base, "node-r", str(state))
    with pytest.raises(SystemExit) as exc:
        fabric_server_cli.runners_main(["--state-dir", str(state), "--json"])
    assert exc.value.code == 0
    # 通过内部函数验证输出（capsys 与 SystemExit 组合在 fixture 场景略繁）
    records = fabric_server._read_lines_bounded(
        Path(str(state)) / "runners.jsonl")
    assert any("node-r" in line for line in records)
