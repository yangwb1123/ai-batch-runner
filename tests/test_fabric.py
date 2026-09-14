"""Tests for the device-aware execution fabric — D0 + D1.

Covers: config defaults (OFF), the ExecutionTarget abstraction,
LocalExecutionTarget bounded execution (ok/fail/timeout/spawn-crash/overflow),
P0 local probe stability, SSH inventory parsing, remote read-only probe
(ok/unreachable/timeout), probe-state cache, Placement Dry-run, and the
`pi-batch devices` subcommand.

Modules: pbatch.fabric (core) / pbatch.fabric_devices (inventory/probe/
placement) / pbatch.fabric_cli (CLI).
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from pbatch import config, fabric, fabric_cli, fabric_devices, fabric_exec, fabric_ssh


def test_fabric_disabled_by_default():
    """D0 承诺：device_fabric 默认关闭，行为与单机完全一致。"""
    assert config.DEVICE_FABRIC_ENABLED is False
    assert config.DEVICE_FABRIC_MODE == "off"
    assert config.DEVICE_FABRIC_LAN_SCAN is False
    assert config.DEVICE_FABRIC_AUTO_INSTALL is False
    assert config.DEVICE_FABRIC_APPROVAL_REQUIRED is True


def test_bool_setting_validation():
    """坏值回退默认（fail closed），字符串 true/false 归一化。"""
    assert config._bool_setting({"enabled": True}, "enabled", False) is True
    assert config._bool_setting({"enabled": "yes"}, "enabled", False) is True
    assert config._bool_setting({"enabled": "0"}, "enabled", True) is False
    assert config._bool_setting({"enabled": 42}, "enabled", False) is False
    assert config._bool_setting({"enabled": "maybe"}, "enabled", True) is True


def test_artifact_ref_content_addressed(tmp_path):
    """ArtifactRef：哈希才是身份（AADM-D §24）。"""
    p = tmp_path / "a.txt"
    p.write_text("hello")
    ref = fabric.artifact_ref(p)
    assert ref.sha256 == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1"
                          "b161e5c1fa7425e73043362938b9824")
    assert ref.size == 5
    assert ref.path == str(p)


def test_environment_digest_deterministic():
    assert fabric._environment_digest() == fabric._environment_digest()
    assert len(fabric._environment_digest()) == 16


def test_local_execute_ok(tmp_path):
    target = fabric.LocalExecutionTarget()
    spec = fabric.ExecutionSpec(
        argv=[sys.executable, "-c", "print('hi')"],
        cwd=str(tmp_path), label="unit-ok")
    ev = target.execute(spec)
    assert ev.result == "passed"
    assert ev.exit_code == 0
    assert "hi" in ev.stdout
    assert not ev.timed_out and not ev.output_overflow
    assert ev.target_id == "local"
    assert ev.runner_version == fabric.FABRIC_VERSION
    assert ev.environment_digest


def test_local_execute_failure():
    ev = fabric.LocalExecutionTarget().execute(fabric.ExecutionSpec(
        argv=[sys.executable, "-c", "import sys; sys.exit(3)"], cwd="."))
    assert ev.result == "failed"
    assert ev.exit_code == 3
    assert not ev.timed_out


def test_local_execute_timeout_kills_process_group(tmp_path):
    """硬超时：SIGKILL 整个进程组，不留下孤儿进程。"""
    spec = fabric.ExecutionSpec(
        argv=[sys.executable, "-c",
              "import time; time.sleep(30)"],
        cwd=str(tmp_path), timeout=0.3)
    start = time.monotonic()
    ev = fabric.LocalExecutionTarget().execute(spec)
    elapsed = time.monotonic() - start
    assert ev.timed_out is True
    assert ev.result == "failed"
    assert ev.exit_code == -1
    assert elapsed < 10  # 没有被 30s 的 sleep 拖住


def test_local_execute_spawn_crash():
    """spawn 失败 fail closed，不抛异常（故障显式）。"""
    ev = fabric.LocalExecutionTarget().execute(fabric.ExecutionSpec(
        argv=["/nonexistent/bin/definitely-not-a-command"], cwd="."))
    assert ev.result == "failed"
    assert ev.exit_code == -1
    assert ev.stderr


def test_local_execute_output_cap(tmp_path):
    """输出超限：继续排空防阻塞，但证据标记 overflow。"""
    spec = fabric.ExecutionSpec(
        argv=[sys.executable, "-c",
              "print('x' * 10000)"],
        cwd=str(tmp_path), output_cap=1024, timeout=10)
    ev = fabric.LocalExecutionTarget().execute(spec)
    assert ev.output_overflow is True
    assert ev.result == "failed"
    assert len(ev.stdout) < 2000


def test_evidence_serializable():
    ev = fabric.LocalExecutionTarget().execute(fabric.ExecutionSpec(
        argv=[sys.executable, "-c", "print(1)"], cwd="."))
    data = ev.to_dict()
    json.dumps(data)  # 必须可 JSON 序列化（后续进证据账本/决策日志）
    assert data["result"] == "passed"


def test_local_probe_shape_and_stability():
    probe = fabric.local_probe()
    assert probe["device_id"]
    assert probe["kind"] == "physical_device"
    caps = probe["static_capabilities"]
    assert caps["os"] and caps["architecture"]
    assert caps["cpu_cores"] >= 1
    assert probe["capability_fingerprint"]
    assert probe["capability_valid_until"]
    assert probe["protocol_version"] == fabric.FABRIC_PROTOCOL
    # 设备身份与能力指纹稳定（同一台机器两次探测一致）
    assert fabric.local_probe()["device_id"] == probe["device_id"]
    assert (fabric.local_probe()["capability_fingerprint"]
            == probe["capability_fingerprint"])


def test_local_probe_full_includes_runtimes():
    probe = fabric.local_probe(depth="full")
    runtimes = probe["runtimes"]
    assert "docker" in runtimes and "git" in runtimes
    assert isinstance(runtimes["git"], bool)


def test_current_target_is_local():
    assert isinstance(fabric.current_target(), fabric.LocalExecutionTarget)
    assert fabric.current_target().target_id == "local"


def test_device_inventory_static_entries():
    static = [{"name": "rtx-node", "host": "rtx-node",
               "labels": {"ai.backend.cuda": "true"}},
              "not-a-dict"]  # 坏条目被跳过
    nodes = fabric_devices.device_inventory(static=static, use_cache=False)
    assert nodes[0]["kind"] == "physical_device"
    remote = [n for n in nodes if n["device_id"].startswith("ssh-")]
    assert len(remote) == 1
    assert remote[0]["name"] == "rtx-node"
    assert remote[0]["dynamic_state"]["status"] == "unknown"
    assert remote[0]["trust"]["zone"] == "untrusted-external"
    assert remote[0]["labels"]["ai.backend.cuda"] == "true"


# ---------------------------------------------------------------------------
# D1: SSH inventory / 远程只读探测 / 状态缓存 / Placement Dry-run
# ---------------------------------------------------------------------------

def test_parse_ssh_config(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("""
Host rtx-node
    HostName 192.168.1.10
    User ubuntu
    Port 2222
    ProxyJump bastion
    IdentityFile ~/.ssh/id_ed25519

Host mac-mini

Host *.example.com
    User deploy
""")
    hosts = fabric_devices.parse_ssh_config(str(cfg))
    assert [h["alias"] for h in hosts] == ["rtx-node", "mac-mini"]  # 通配符跳过
    rtx = hosts[0]
    assert rtx["host_name"] == "192.168.1.10"
    assert rtx["user"] == "ubuntu"
    assert rtx["port"] == "2222"
    assert rtx["proxyjump"] == "bastion"
    assert rtx["identityfile"] == "~/.ssh/id_ed25519"
    assert hosts[1]["host_name"] == "mac-mini"  # 无 HostName 时默认别名
    # 缺失文件 → 空列表
    assert fabric_devices.parse_ssh_config(str(tmp_path / "nope")) == []


def test_valid_ssh_host():
    assert fabric_devices._valid_ssh_host("node-1")
    assert fabric_devices._valid_ssh_host("a.b_c")
    assert not fabric_devices._valid_ssh_host("-x")
    assert not fabric_devices._valid_ssh_host("a b")
    assert not fabric_devices._valid_ssh_host("a;rm -rf")
    assert not fabric_devices._valid_ssh_host("")


def test_ssh_probe_argv_options():
    target = {"name": "rtx", "host_name": "192.168.1.10",
              "user": "ubuntu", "port": "2222", "config_path": ""}
    argv = fabric_devices._ssh_probe_argv(target)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "-l" in argv and argv[argv.index("-l") + 1] == "ubuntu"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"
    assert argv[-2] == "192.168.1.10"
    assert argv[-1] == fabric_devices.REMOTE_PROBE_CMD
    with pytest.raises(ValueError):
        fabric_devices._ssh_probe_argv(
            {"name": "x", "host_name": "-o ProxyCommand=evil"})


def test_parse_remote_probe():
    out = ("os=Linux\nos_release=6.8.0\narchitecture=aarch64\n"
           "cpu_cores=8\nhostname=mac-mini\nMemTotal: 16384 kB\n"
           "runtime_docker=1\nruntime_python3=0\nruntime_go=1\n"
           "garbage line\n")
    fields = fabric_devices._parse_remote_probe(out)
    assert fields["os"] == "Linux"
    assert fields["architecture"] == "aarch64"
    assert fields["cpu_cores"] == 8
    assert fields["total_memory_bytes"] == 16384 * 1024
    assert fields["runtimes"] == {"docker": True, "python3": False, "go": True}


def test_remote_probe_ok(monkeypatch):
    out = "os=Linux\narchitecture=x86_64\ncpu_cores=4\n" \
          "hostname=node\nruntime_git=1\n"
    fake_proc = type("P", (), {"returncode": 0})()
    monkeypatch.setattr(fabric_devices, "_spawn_bounded",
                        lambda spec, env: (fake_proc, out, "", False, False))
    report = fabric_devices.remote_probe({"name": "rtx", "host_name": "rtx"})
    assert report["dynamic_state"]["status"] == "online"
    assert report["static_capabilities"]["cpu_cores"] == 4
    assert report["runtimes"]["git"] is True
    assert report["capability_fingerprint"]
    assert report["capability_valid_until"]
    assert report["probe_error"] == ""
    assert report["trust"]["max_effect_class"] == "read_only"


def test_remote_probe_unreachable(monkeypatch):
    """不可达也返回报告（status=unreachable + 错误摘录），不抛异常。"""
    fake_proc = type("P", (), {"returncode": 255})()
    monkeypatch.setattr(fabric_devices, "_spawn_bounded",
                        lambda spec, env: (fake_proc, "", "ssh: Connection refused",
                                           False, False))
    report = fabric_devices.remote_probe({"name": "offline", "host_name": "offline"})
    assert report["dynamic_state"]["status"] == "unreachable"
    assert report["static_capabilities"] == {}
    assert "Connection refused" in report["probe_error"]
    assert report["capability_valid_until"] == ""


def test_remote_probe_timeout(monkeypatch):
    fake_proc = type("P", (), {"returncode": -1})()
    monkeypatch.setattr(fabric_devices, "_spawn_bounded",
                        lambda spec, env: (fake_proc, "", "", True, False))
    report = fabric_devices.remote_probe({"name": "slow", "host_name": "slow"})
    assert report["dynamic_state"]["status"] == "unreachable"


def test_resolve_device_precedence(tmp_path):
    static = [{"name": "rtx", "host": "10.0.0.5", "user": "alice"}]
    cfg = tmp_path / "config"
    cfg.write_text("Host rtx\n    HostName 10.0.0.9\n")
    # 静态配置优先于 ssh config 别名
    target = fabric_devices.resolve_device("rtx", static=static,
                                           ssh_config_path=str(cfg))
    assert target["host_name"] == "10.0.0.5"
    assert target["user"] == "alice"
    # 仅 ssh alias
    target2 = fabric_devices.resolve_device("mac", static=[],
                                            ssh_config_path=str(cfg))
    assert target2 is None
    cfg2 = tmp_path / "config2"
    cfg2.write_text("Host mac\n    HostName 10.0.0.9\n")
    target3 = fabric_devices.resolve_device("mac", static=[],
                                            ssh_config_path=str(cfg2))
    assert target3["host_name"] == "10.0.0.9"
    # 未知设备
    assert fabric_devices.resolve_device("nope", static=static,
                                         ssh_config_path=str(cfg2)) is None


def test_save_load_probe_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    report = {"device_id": "ssh-abc", "name": "rtx",
              "capability_valid_until": "2099-01-01T00:00:00",
              "static_capabilities": {"os": "Linux"}}
    path = fabric_devices.save_probe_state(report)
    assert path.endswith("state.jsonl")
    # 同一设备二次保存：load 返回最新一条
    fabric_devices.save_probe_state({**report, "name": "rtx-v2"})
    states = fabric_devices.load_probe_states()
    assert states["ssh-abc"]["report"]["name"] == "rtx-v2"
    # 坏行被跳过（不崩溃）
    with open(tmp_path / "state.jsonl", "a", encoding="utf-8") as f:
        f.write("not-json\n")
    assert fabric_devices.load_probe_states()["ssh-abc"]["report"]["name"] == "rtx-v2"


def test_evaluate_placement_local_device():
    """本机（Linux x86_64）的放置判定：自身必须可行。"""
    placement = fabric_devices.TaskPlacement(
        required_os=["Linux"], required_architecture=["x86_64"],
        min_memory_bytes=8 * 1024 ** 3, min_cpu_cores=4)
    candidates = fabric_devices.evaluate_placement(placement)
    own_id = fabric.local_probe()["device_id"]
    own = [c for c in candidates if c["device_id"] == own_id]
    assert own and own[0]["feasible"]


def test_evaluate_placement_reasons():
    """不满足约束时必须给出原因（淘汰可解释）。"""
    placement = fabric_devices.TaskPlacement(
        required_os=["Darwin"], min_memory_bytes=1 * 1024 ** 6)
    candidates = fabric_devices.evaluate_placement(placement)
    assert all(not c["feasible"] for c in candidates)
    assert any("OS" in r for c in candidates for r in c["reasons"])


def test_evaluate_placement_effect_class():
    """不可信设备（max read_only）不能执行不可逆副作用。"""
    placement = fabric_devices.TaskPlacement(max_effect_class="irreversible")
    candidates = fabric_devices.evaluate_placement(placement)
    remote = [c for c in candidates if c["device_id"].startswith("ssh-")]
    assert all("副作用等级" in r for c in remote for r in c["reasons"])


def test_evaluate_placement_target_required():
    placement = fabric_devices.TaskPlacement(target_required="no-such-device")
    candidates = fabric_devices.evaluate_placement(placement)
    assert all(not c["feasible"] for c in candidates)
    assert all("不是目标设备" in r for c in candidates for r in c["reasons"])


def test_evaluate_placement_stale_warning():
    """过期能力信息给出警告而不是静默使用。"""
    device = fabric.local_probe()
    device["capability_valid_until"] = "2000-01-01T00:00:00"
    candidates = fabric_devices.evaluate_placement(
        fabric_devices.TaskPlacement(), devices=[device])
    assert candidates[0]["feasible"]
    assert any("过期" in w for w in candidates[0]["warnings"])


def test_devices_ssh_hosts_json(tmp_path, capsys):
    cfg = tmp_path / "config"
    cfg.write_text("Host rtx\n    HostName 10.0.0.5\n")
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["ssh-hosts", "--ssh-config", str(cfg), "--json"])
    assert exc.value.code == 0
    hosts = json.loads(capsys.readouterr().out)
    assert hosts[0]["alias"] == "rtx"


def test_devices_placement_json(capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["placement", "--os", "linux", "--json"])
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert "candidate_devices" in data
    assert any(c["feasible"] for c in data["candidate_devices"])


def test_devices_placement_text(capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["placement", "--os", "darwin"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Placement Dry-run" in out
    assert "无可行设备" in out


def test_devices_probe_unknown_device(capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["probe", "no-such-device", "--json"])
    assert exc.value.code == 2
    assert "unknown device" in capsys.readouterr().err


def test_devices_list_merges_cached_state(tmp_path, monkeypatch, capsys):
    """probe 成功后 list 展示缓存的能力状态（静态条目被探测结果覆盖）。"""
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    report = {
        "device_id": fabric_devices._remote_device_id(
            {"name": "rtx", "host_name": "rtx"}),
        "name": "rtx", "kind": "physical_device",
        "labels": {}, "transports": [],
        "static_capabilities": {"os": "Linux", "architecture": "x86_64",
                                "cpu_cores": 8, "total_memory_bytes": 0},
        "runtimes": {"docker": True}, "dynamic_state": {"status": "online"},
        "trust": {}, "protocol_version": "",
        "capability_fingerprint": "f",
        "capability_valid_until": "2099-01-01T00:00:00",
    }
    fabric_devices.save_probe_state(report)
    static = [{"name": "rtx", "host": "rtx"}]
    nodes = fabric_devices.device_inventory(static=static)
    rtx = [n for n in nodes if n["name"] == "rtx"][0]
    assert rtx["dynamic_state"]["status"] == "online"
    assert rtx["static_capabilities"]["os"] == "Linux"


def test_device_inventory_includes_probed_ssh_hosts(tmp_path, monkeypatch):
    """探测过的 ssh-config 主机（非静态条目）也出现在清单里；过期标 stale。"""
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    report = {
        "device_id": "ssh-abcdef1234567890", "name": "mac-mini",
        "kind": "physical_device", "labels": {}, "transports": [],
        "static_capabilities": {"os": "Darwin", "architecture": "arm64"},
        "runtimes": {}, "dynamic_state": {"status": "online"},
        "trust": {}, "protocol_version": "",
        "capability_fingerprint": "f",
        "capability_valid_until": "2099-01-01T00:00:00",
    }
    fabric_devices.save_probe_state(report)
    nodes = fabric_devices.device_inventory(static=[])
    names = [n["name"] for n in nodes]
    assert "mac-mini" in names
    mac = [n for n in nodes if n["name"] == "mac-mini"][0]
    assert mac["dynamic_state"]["status"] == "online"
    # 过期条目标 stale
    stale = dict(report)
    stale["device_id"] = "ssh-ffffffffffffffff"
    stale["name"] = "old-node"
    stale["capability_valid_until"] = "2000-01-01T00:00:00"
    fabric_devices.save_probe_state(stale)
    nodes2 = fabric_devices.device_inventory(static=[])
    old = [n for n in nodes2 if n["name"] == "old-node"][0]
    assert old["dynamic_state"]["status"] == "stale"


def test_devices_list_json(capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["list", "--json"])
    assert exc.value.code == 0
    nodes = json.loads(capsys.readouterr().out)
    assert nodes and nodes[0]["kind"] == "physical_device"
    assert nodes[0]["device_id"]


def test_devices_probe_json(capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["probe", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert "static_capabilities" in report
    assert "capability_fingerprint" in report


def test_devices_list_text(capsys):
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["list"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Device Inventory" in out
    assert "INVENTORY mode" in out


# ---------------------------------------------------------------------------
# D2: SSH 受控远程执行（结构化 argv / 门禁 / 同一证据格式 / 传输 / 取消）
# ---------------------------------------------------------------------------

def test_remote_argv_command_roundtrip():
    """shlex.quote 保护：远程命令还原后与原 argv 完全一致（防注入）。"""
    argv = ["python3", "-c", "print('a b'); import os; os.system('echo hi')",
            "$HOME/x", "--flag=1"]
    cmd = fabric_exec._remote_argv_command(argv)
    assert cmd.startswith("exec ")
    import shlex
    assert shlex.split(cmd[len("exec "):]) == argv


def test_remote_argv_command_cwd():
    cmd = fabric_exec._remote_argv_command(["ls", "-la"], cwd="/tmp/a b")
    assert cmd.startswith("cd '/tmp/a b' && exec ls -la")


def test_remote_argv_command_rejects_empty():
    with pytest.raises(ValueError):
        fabric_exec._remote_argv_command([])
    with pytest.raises(ValueError):
        fabric_exec._remote_argv_command(["ls", 42])


def test_scp_argv_port_and_user():
    """scp 端口必须是大写 -P；用户写进目标串。"""
    target = {"name": "rtx", "host_name": "10.0.0.5",
              "user": "alice", "port": "2222", "config_path": ""}
    up = fabric_exec._scp_argv(target, "/local/f", "/remote/f")
    assert up[0] == "scp"
    assert "-P" in up and up[up.index("-P") + 1] == "2222"
    assert up[-1] == "alice@10.0.0.5:/remote/f"
    down = fabric_exec._scp_argv(target, "/local/f", "/remote/f",
                                    download=True)
    assert down[-2] == "alice@10.0.0.5:/remote/f"
    assert down[-1] == "/local/f"


def test_build_ssh_argv_rejects_bad_host():
    with pytest.raises(ValueError):
        fabric_ssh._build_ssh_argv(
            {"name": "x", "host_name": "-oProxyCommand=evil"}, "ls")


def _fake_target():
    return {"name": "rtx", "host_name": "rtx", "user": "", "port": "",
            "config_path": ""}


def test_ssh_execute_ok(monkeypatch):
    out = "hello remote\n"
    fake_proc = type("P", (), {"returncode": 0})()
    monkeypatch.setattr(fabric_exec, "_spawn_bounded",
                        lambda spec, env, on_spawn=None:
                        (fake_proc, out, "", False, False))
    target = fabric_exec.SshExecutionTarget(_fake_target())
    ev = target.execute(fabric.ExecutionSpec(
        argv=["echo", "hello remote"], label="unit", effect_class="read_only"))
    assert ev.result == "passed"
    assert ev.exit_code == 0
    assert ev.stdout == out
    assert ev.target_id == "ssh:rtx"
    assert ev.environment_digest  # 含 ssh + 指纹（无缓存时为 unknown）
    assert ev.attempt_id.startswith("ssh-")


def test_ssh_execute_failure(monkeypatch):
    fake_proc = type("P", (), {"returncode": 127})()
    monkeypatch.setattr(fabric_exec, "_spawn_bounded",
                        lambda spec, env, on_spawn=None:
                        (fake_proc, "", "command not found", False, False))
    ev = fabric_exec.SshExecutionTarget(_fake_target()).execute(
        fabric.ExecutionSpec(argv=["nope"], label="unit"))
    assert ev.result == "failed"
    assert ev.exit_code == 127
    assert "command not found" in ev.stderr


def test_ssh_execute_timeout(monkeypatch):
    fake_proc = type("P", (), {"returncode": -1})()
    monkeypatch.setattr(fabric_exec, "_spawn_bounded",
                        lambda spec, env, on_spawn=None:
                        (fake_proc, "", "", True, False))
    ev = fabric_exec.SshExecutionTarget(_fake_target()).execute(
        fabric.ExecutionSpec(argv=["sleep", "99"], timeout=0.1, label="unit"))
    assert ev.timed_out is True
    assert ev.result == "failed"
    assert "timed out" in ev.stderr


def test_ssh_execute_rejects_high_effect():
    """D2 副作用等级上限 sandboxed：可补偿/不可逆直接拒绝（fail closed）。"""
    target = fabric_exec.SshExecutionTarget(_fake_target())
    for effect in ("reversible", "compensatable", "irreversible"):
        with pytest.raises(ValueError):
            target.execute(fabric.ExecutionSpec(
                argv=["rm", "-rf", "/"], effect_class=effect))


def test_ssh_execute_evidence_same_shape_as_local():
    """远程证据与本地同一格式（to_dict 键一致）。"""
    local = fabric.LocalExecutionTarget().execute(
        fabric.ExecutionSpec(argv=[sys.executable, "-c", "print(1)"], cwd="."))
    remote = fabric_exec._execute_remote(
        _fake_target(), fabric.ExecutionSpec(argv=["true"], label="unit"))
    assert set(local.to_dict()) == set(remote.to_dict())


def test_ssh_cancel_registry(monkeypatch):
    """执行期间登记的进程可被 cancel 杀掉进程组。"""
    killed = []
    fake_proc = type("P", (), {"returncode": 0})()
    monkeypatch.setattr(fabric_exec, "_kill_group",
                        lambda proc: killed.append(proc))
    monkeypatch.setattr(fabric_exec, "_spawn_bounded",
                        lambda spec, env, on_spawn=None:
                        (fake_proc, "", "", False, False))
    ev = fabric_exec.SshExecutionTarget(_fake_target()).execute(
        fabric.ExecutionSpec(argv=["true"], label="unit"))
    # 执行结束后登记已清理；cancel 对未知 attempt 是 no-op
    assert killed == []
    fabric_exec._cancel_remote("ssh-nonexistent")
    assert killed == []
    # 模拟执行中：登记后 cancel 生效
    fabric_exec._register_ssh_proc("ssh-live", fake_proc)
    try:
        fabric_exec._cancel_remote("ssh-live")
        assert killed == [fake_proc]
    finally:
        fabric_exec._unregister_ssh_proc("ssh-live")


def test_probe_required_error(tmp_path, monkeypatch):
    """执行前门禁：无有效探测证据的设备拒绝执行。"""
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    assert fabric_exec.probe_required_error(_fake_target())
    report = fabric_devices.remote_probe(_fake_target())
    report["capability_fingerprint"] = "f"
    report["capability_valid_until"] = "2099-01-01T00:00:00"
    fabric_devices.save_probe_state(report)
    assert fabric_exec.probe_required_error(_fake_target()) is None


def test_placement_gate_error():
    """执行前门禁：设备不在已知设备池则拒绝。"""
    gate = fabric_exec.placement_gate_error("no-such-device", "read_only")
    assert gate and "不在已知设备池" in gate
    # 目标设备存在但 Placement 不通过（不可逆副作用超出 untrusted 上限）；
    # rtx 不在池中时同样拒绝（设备池门禁）
    gate2 = fabric_exec.placement_gate_error("rtx", "irreversible")
    assert gate2 is not None


def test_target_for_unknown():
    assert fabric_exec.target_for("no-such-device") is None


def test_run_cli_mode_gate(capsys):
    """mode 未达 execute 时拒绝 run（渐进模式门禁）。"""
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["run", "rtx", "--", "ls"])
    assert exc.value.code == 2
    assert "EXECUTE 模式" in capsys.readouterr().err


def test_run_cli_requires_probe(tmp_path, monkeypatch, capsys):
    """未探测的设备拒绝执行，提示先 probe。"""
    monkeypatch.setattr(config, "DEVICE_FABRIC_MODE", "execute")
    monkeypatch.setattr(config, "DEVICE_FABRIC_STATIC",
                        ({"name": "rtx", "host": "rtx"},))
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["run", "rtx", "--", "ls"])
    assert exc.value.code == 2
    assert "探测" in capsys.readouterr().err


def test_run_cli_ok(tmp_path, monkeypatch, capsys):
    """全链路：探测缓存有效 + Placement 通过 + 执行成功 → json 证据。"""
    monkeypatch.setattr(config, "DEVICE_FABRIC_MODE", "execute")
    monkeypatch.setattr(config, "DEVICE_FABRIC_STATIC",
                        ({"name": "rtx", "host": "rtx"},))
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    target = fabric_devices.resolve_device("rtx", static=(
        {"name": "rtx", "host": "rtx"},))
    report = fabric_devices.remote_probe(target)
    report["capability_fingerprint"] = "f"
    report["capability_valid_until"] = "2099-01-01T00:00:00"
    fabric_devices.save_probe_state(report)
    fake_proc = type("P", (), {"returncode": 0})()
    monkeypatch.setattr(fabric_exec, "_spawn_bounded",
                        lambda spec, env, on_spawn=None:
                        (fake_proc, "ok\n", "", False, False))
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["run", "rtx", "--json", "--", "echo", "ok"])
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["result"] == "passed"
    assert data["target_id"] == "ssh:rtx"


def test_run_cli_requires_separator(capsys):
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(config, "DEVICE_FABRIC_MODE", "execute")
    try:
        with pytest.raises(SystemExit) as exc:
            fabric_cli.devices_main(["run", "rtx", "ls"])
        assert exc.value.code == 2
        assert "-- 分隔" in capsys.readouterr().err
    finally:
        monkeypatch.undo()


def test_run_cli_upload_missing_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DEVICE_FABRIC_MODE", "execute")
    monkeypatch.setattr(config, "DEVICE_FABRIC_STATIC",
                        ({"name": "rtx", "host": "rtx"},))
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_DIR", tmp_path)
    monkeypatch.setattr(fabric_devices, "DEVICE_STATE_FILE",
                        tmp_path / "state.jsonl")
    target = fabric_devices.resolve_device("rtx", static=(
        {"name": "rtx", "host": "rtx"},))
    report = fabric_devices.remote_probe(target)
    report["capability_fingerprint"] = "f"
    report["capability_valid_until"] = "2099-01-01T00:00:00"
    fabric_devices.save_probe_state(report)
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["run", "rtx", "--upload",
                                 "/no/such/file:x", "--", "ls"])
    assert exc.value.code == 2
    assert "upload source missing" in capsys.readouterr().err
