"""Device-Aware Execution Fabric — core (ExecutionTarget abstraction).

AADM-D (`docs/AADM_DEVICE.md`) 的 D0 落地核心：把"在哪里执行"抽象为
ExecutionTarget / ExecutionAttempt / ArtifactRef / ExecutionEvidence，
本机是第一个适配器（LocalExecutionTarget）。

设计原则（AADM-D §0）：
- 位置透明：上层只面对 ExecutionTarget，不关心路径在哪台机器
- 故障显式：超时/输出超限/进程崩溃都显式反映在 ExecutionEvidence
- 现在完成抽象隔离，之后再逐步实现远程能力（SSH/Runner/调度）

模块划分：
- `pbatch/fabric.py`：核心抽象 + 本机探测 + LocalExecutionTarget
- `pbatch/fabric_devices.py`：SSH inventory / 远程只读探测 / 状态缓存 /
  Placement Dry-run（D1）
- `pbatch/fabric_cli.py`：`pi-batch devices` 子命令

runner.py 现有执行路径有意保持不动（零回归）；远程执行落地时再路由。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from .config import log
from .version import VERSION
from .fabric_gpu import cuda_visible_devices, probe_nvidia_gpus
from .fabric_workspace import RUNNER_CAPABILITY, workspace_filesystem_supported

FABRIC_VERSION = VERSION
FABRIC_PROTOCOL = "0.1"

EFFECT_CLASSES = ("pure", "read_only", "sandboxed", "reversible",
                  "compensatable", "irreversible")
MOBILITY_CLASSES = ("stateless", "restartable", "checkpointable", "pinned")
ATTEMPT_STATUSES = ("created", "reserved", "running", "verified",
                    "failed", "lost")
EVIDENCE_RESULTS = ("passed", "failed", "inconclusive")

# 能力上报有效期（AADM-D §8）：三天前上报的 CUDA 可用不能永远当真。
CAPABILITY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# 数据模型（AADM-D §36：现在预留，将来远程执行直接复用）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactRef:
    """内容寻址产物引用：哈希才是身份，路径只是定位（AADM-D §24）。"""
    path: str
    sha256: str
    size: int


@dataclass
class ExecutionSpec:
    """一次结构化执行请求。禁止随意拼接 Shell —— 只接受 argv。"""
    argv: list
    cwd: str = "."
    env: Optional[dict] = None
    timeout: float = 600.0
    output_cap: int = 262144
    label: str = "execution"
    effect_class: str = "pure"
    mobility: str = "stateless"
    inputs: list = field(default_factory=list)   # list[ArtifactRef]
    code_digest: str = ""
    cancel_event: Optional[threading.Event] = None
    gpu_assignment: Optional[dict] = None
    restrict_gpu_visibility: bool = False
    cancelled: bool = field(default=False, init=False)


@dataclass
class ExecutionEvidence:
    """执行证据：结果 + digest 绑定，防止"远程测试通过"实为旧代码/旧环境
    （AADM-D §23）。"""
    attempt_id: str
    target_id: str
    label: str
    result: str
    exit_code: int
    timed_out: bool
    output_overflow: bool
    stdout: str
    stderr: str
    code_digest: str
    environment_digest: str
    input_digest: str
    output_digest: str
    runner_version: str
    elapsed: float
    input_bytes: int = 0
    network_bytes: int = 0
    cancelled: bool = False
    gpu_assignment: Optional[dict] = None

    def to_dict(self) -> dict:
        result = {
            "attempt_id": self.attempt_id,
            "target_id": self.target_id,
            "label": self.label,
            "result": self.result,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "output_overflow": self.output_overflow,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "code_digest": self.code_digest,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "environment_digest": self.environment_digest,
            "runner_version": self.runner_version,
            "stdout_bytes": len(self.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(self.stderr.encode("utf-8", errors="replace")),
            "elapsed_seconds": round(self.elapsed, 3),
            "input_bytes": max(0, int(self.input_bytes or 0)),
            "network_bytes": max(0, int(self.network_bytes or 0)),
            "cancelled": self.cancelled,
        }
        if self.gpu_assignment:
            result["gpu_assignment"] = dict(self.gpu_assignment)
        return result


@dataclass
class ExecutionAttempt:
    """一次具体执行尝试（AADM-D §2：Agent/节点/尝试/目标四概念分离）。"""
    attempt_id: str
    task_node_id: str
    target_id: str
    spec: ExecutionSpec
    status: str = "created"
    evidence: Optional[ExecutionEvidence] = None


class ExecutionTarget(Protocol):
    """执行目标抽象（AADM-D §37）：本机只是第一个 Adapter。"""
    target_id: str

    def probe(self) -> dict:
        """目标能力快照（静态 + 动态，带有效期）。"""

    def execute(self, spec: ExecutionSpec) -> ExecutionEvidence:
        """执行一次结构化请求并返回证据。"""

    def cancel(self, attempt_id: str) -> None:
        """取消一个执行尝试（尽力而为）。"""


# ---------------------------------------------------------------------------
# 本机探测（P0 注册信息 + P1 运行时存在性，只读）
# ---------------------------------------------------------------------------

def _machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            continue
    return platform.node() or "unknown-host"


def _device_id() -> str:
    """稳定设备身份：machine-id + 平台指纹派生。注意：这不是密码学身份
    （AADM-D §7），D0 仅本地识别；登记/证书在后续阶段引入。"""
    raw = f"{_machine_id()}|{platform.system()}|{platform.machine()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _memory_kb() -> tuple:
    """(total_bytes, available_bytes)；非 Linux 返回 (0, 0)。"""
    if not sys.platform.startswith("linux"):
        return (0, 0)
    total = available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8",
                                                    errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
    except OSError:
    # best-effort I/O：失败不阻塞主流程（已验证有意）
        pass
    return (total, available)


def _runtime_presence() -> dict:
    """P1：常见运行时是否存在（只读 PATH 检查，不安装、不修改）。"""
    return {tool: bool(shutil.which(tool))
            for tool in ("docker", "node", "npm", "python3", "go",
                         "java", "git", "psql")}


def _environment_digest() -> str:
    """环境指纹：平台 + 解释器 + fabric 版本（AADM-D §23 环境可复现）。"""
    raw = "|".join((platform.system(), platform.release(),
                    platform.machine(), sys.version.split()[0],
                    FABRIC_VERSION))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_gpu_facts() -> tuple[list, dict]:
    probe = probe_nvidia_gpus()
    static = [{key: row[key] for key in (
        "uuid", "name", "vendor", "total_memory_bytes", "mig_mode")}
        for row in probe["devices"]]
    return static, {"gpu_status": probe["status"], "gpus": probe["devices"]}


def _capability_fingerprint(static: dict, runtimes: dict) -> str:
    raw = json.dumps({"static": static, "runtimes": runtimes}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _runner_capabilities() -> list:
    capabilities = ["task.v1", "grant.v1", "checkpoint.v1", "task.gpu.v1"]
    if workspace_filesystem_supported():
        capabilities.append(RUNNER_CAPABILITY)
    return capabilities


def local_probe(depth: str = "basic") -> dict:
    """本机 P0/P1 探测（只读、无副作用、无网络扫描）。"""
    if depth not in ("basic", "full"):
        depth = "basic"
    total, available = _memory_kb()
    runtimes = _runtime_presence() if depth == "full" else {}
    gpu_static, gpu_dynamic = _local_gpu_facts()
    static = {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "cpu_cores": os.cpu_count() or 0,
        "total_memory_bytes": total,
        "python": sys.version.split()[0],
        "hostname": platform.node(),
        "gpus": gpu_static,
    }
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    dynamic = {
        "status": "online",
        "load": round(load[0], 2),
        "available_memory_bytes": available,
        "running_tasks": 0,
        "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **gpu_dynamic,
    }
    fingerprint = _capability_fingerprint(static, runtimes)
    capabilities = _runner_capabilities()
    return {
        "device_id": _device_id(),
        "kind": "physical_device",
        "name": f"local-{platform.node()}",
        "labels": {"execution.fabric": "true", "trust.zone": "trusted-local"},
        "transports": [{"type": "local"}],
        "static_capabilities": static,
        "runtimes": runtimes,
        "dynamic_state": dynamic,
        "trust": {"level": 3, "zone": "trusted-local",
                  "max_effect_class": "irreversible",
                  "allowed_data_classes": ["public", "internal",
                                            "confidential", "secret"]},
        "capabilities": capabilities,
        "protocol_version": FABRIC_PROTOCOL,
        "capability_fingerprint": fingerprint,
        "environment_digest": _environment_digest(),
        "capability_valid_until": time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() + CAPABILITY_TTL_HOURS * 3600)),
    }


# ---------------------------------------------------------------------------
# LocalExecutionTarget：本机有界进程组执行（第一个适配器）
# ---------------------------------------------------------------------------

def _kill_group(proc: Optional[subprocess.Popen]) -> None:
    """杀掉整个子进程组（与 runner 同一语义：硬超时不留下孤儿进程）。"""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass  # 进程已退出/无权：尽力而为的清理路径


def _drain_into(stream, collector: list, cap: int, overflow: list) -> None:
    """排空一条子进程管道到有界收集器；超限后继续排空但停止收集
    （阻塞的管道会挂死子进程）。溢出标记写入 overflow[0]。"""
    total = 0
    try:
        for line in iter(lambda: stream.readline(8192), ""):
            total += len(line.encode("utf-8", errors="replace"))
            if total <= cap:
                collector.append(line)
            else:
                overflow[0] = True
    except ValueError:
        pass  # stream closed
    finally:
        try:
            stream.close()
        except OSError:
        # best-effort I/O：失败不阻塞主流程（已验证有意）
            pass


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def artifact_ref(path) -> ArtifactRef:
    """把一个本地文件转成内容寻址引用（D0 仅本机；远程传输后续阶段）。"""
    p = Path(path)
    data = p.read_bytes()
    return ArtifactRef(path=str(p), sha256=hashlib.sha256(data).hexdigest(),
                       size=len(data))


class LocalExecutionTarget:
    """本机执行适配器：进程组 + 硬超时 + 输出上限 + digest 证据。"""

    target_id = "local"

    def probe(self) -> dict:
        return local_probe()

    def execute(self, spec: ExecutionSpec) -> ExecutionEvidence:
        return _execute_local(spec)

    def cancel(self, attempt_id: str) -> None:
        log.warning("LocalExecutionTarget.cancel(%s): 本机执行在超时/信号时自然终止",
                    attempt_id)


def _spawn_bounded(spec: ExecutionSpec, env: dict, on_spawn=None):
    """Spawn + 排空 + 硬截止。返回 (proc, stdout, stderr, timed_out,
    overflow)；spawn 失败时 proc 为 None、stderr 为异常文本。
    on_spawn(proc) 在成功 spawn 后立即回调（供调用方登记可取消进程）。"""
    spec.cancelled = bool(spec.cancel_event is not None
                          and spec.cancel_event.is_set())
    if spec.cancelled:
        return None, "", "cancelled before process spawn", False, False
    try:
        proc = subprocess.Popen(
            list(spec.argv), cwd=spec.cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
    except Exception as exc:  # spawn 崩溃：fail closed
        return None, "", str(exc), False, False
    if on_spawn is not None:
        on_spawn(proc)

    out, err = [], []
    out_overflow, err_overflow = [False], [False]
    threads = [
        threading.Thread(target=_drain_into,
                         args=(proc.stdout, out, spec.output_cap, out_overflow),
                         daemon=True),
        threading.Thread(target=_drain_into,
                         args=(proc.stderr, err, spec.output_cap, err_overflow),
                         daemon=True),
    ]
    for t in threads:
        t.start()
    timed_out = _wait_bounded_process(proc, spec)
    for t in threads:
        t.join(timeout=5)
    return (proc, "".join(out), "".join(err),
            timed_out, out_overflow[0] or err_overflow[0])


def _wait_bounded_process(proc, spec: ExecutionSpec) -> bool:
    """Wait for exit, requested cancellation, or the absolute deadline."""
    timed_out = False
    deadline = time.monotonic() + max(0.0, float(spec.timeout))
    while True:
        if spec.cancel_event is not None and spec.cancel_event.is_set():
            spec.cancelled = True
            _kill_group(proc)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_group(proc)
            timed_out = True
            break
        try:
            proc.wait(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    if spec.cancelled or timed_out:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
    return timed_out


def _execute_local(spec: ExecutionSpec) -> ExecutionEvidence:
    """有界执行：spawn 失败、超时、输出超限、非零退出都显式反映在证据里，
    不抛异常（故障显式，AADM-D §0）。"""
    start = time.monotonic()
    attempt = f"local-{uuid.uuid4().hex[:12]}"
    env = dict(os.environ)
    if spec.env:
        env.update(spec.env)
    try:
        _configure_gpu_visibility(spec, env)
    except ValueError as exc:
        return _failed_execution_evidence(spec, attempt, start, str(exc))
    proc, stdout, stderr, timed_out, overflow = _spawn_bounded(spec, env)
    if proc is None:
        cancelled = spec.cancelled
        return ExecutionEvidence(
            attempt_id=attempt, target_id="local", label=spec.label,
            result="inconclusive" if cancelled else "failed",
            exit_code=-1, timed_out=False,
            output_overflow=False, stdout="", stderr=stderr,
            code_digest=spec.code_digest,
            environment_digest=_environment_digest(),
            input_digest="", output_digest=_digest(stderr),
            runner_version=FABRIC_VERSION, elapsed=0.0,
            input_bytes=sum(item.size for item in spec.inputs),
            cancelled=cancelled,
            gpu_assignment=spec.gpu_assignment)
    exit_code = -1 if timed_out else proc.returncode
    failed = spec.cancelled or timed_out or overflow or exit_code != 0
    if timed_out and not stderr:
        stderr = f"timed out after {spec.timeout}s"
    return ExecutionEvidence(
        attempt_id=attempt, target_id="local", label=spec.label,
        result=("inconclusive" if spec.cancelled else
                "failed" if failed else "passed"),
        exit_code=exit_code,
        timed_out=timed_out, output_overflow=overflow,
        stdout=stdout, stderr=stderr,
        code_digest=spec.code_digest,
        environment_digest=_environment_digest(),
        input_digest=_digest(
            json.dumps([a.sha256 for a in spec.inputs], sort_keys=True)),
        output_digest=_digest(stdout + stderr),
        runner_version=FABRIC_VERSION,
        elapsed=time.monotonic() - start,
        input_bytes=sum(item.size for item in spec.inputs),
        cancelled=spec.cancelled,
        gpu_assignment=spec.gpu_assignment)


def _failed_execution_evidence(spec: ExecutionSpec, attempt: str,
                               start: float, reason: str) -> ExecutionEvidence:
    text = str(reason)
    return ExecutionEvidence(
        attempt_id=attempt, target_id="local", label=spec.label,
        result="failed", exit_code=-1, timed_out=False,
        output_overflow=False, stdout="", stderr=text,
        code_digest=spec.code_digest,
        environment_digest=_environment_digest(), input_digest="",
        output_digest=_digest(text), runner_version=FABRIC_VERSION,
        elapsed=time.monotonic() - start,
        input_bytes=sum(item.size for item in spec.inputs),
        gpu_assignment=spec.gpu_assignment)


def _configure_gpu_visibility(spec: ExecutionSpec, env: dict) -> None:
    """Apply task-scoped GPU visibility after caller-provided environment."""
    if spec.gpu_assignment:
        env["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices(
            spec.gpu_assignment)
    elif spec.restrict_gpu_visibility:
        env["CUDA_VISIBLE_DEVICES"] = ""


def current_target() -> ExecutionTarget:
    """当前执行目标：D0 固定为本机。远程 ExecutionTarget 在此处接入。"""
    return LocalExecutionTarget()
