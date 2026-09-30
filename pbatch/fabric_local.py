"""本机只读观测（设备身份、运行时存在性、内存/负载/GPU 快照）。

从 `pbatch/fabric.py` 拆出：本模块只回答"这台机器现在是什么样"，不依赖
fabric 的抽象与常量，因此可以被心跳路径单独导入，而不必拉起整个执行抽象。
读取成本是不对称的——`/proc` 与 `shutil.which` 便宜，`nvidia-smi` 是一次
fork + exec——所以 GPU 观测与心跳状态被分成两个函数，由调用方决定刷新节奏
（见 `local_dynamic_state` 的说明）。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from .fabric_gpu import probe_nvidia_gpus
from .fabric_workspace import RUNNER_CAPABILITY, workspace_filesystem_supported
from .version import VERSION

FABRIC_VERSION = VERSION


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


def _dynamic_state(gpu_dynamic: dict, total: int, available: int) -> dict:
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "status": "online",
        "load": round(load[0], 2),
        "available_memory_bytes": available,
        "running_tasks": 0,
        "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **gpu_dynamic,
    }


def gpu_facts() -> dict:
    """One GPU observation. Costs an ``nvidia-smi`` fork, so it is not free.

    Callers on a liveness path must amortize this (see
    ``local_dynamic_state``) instead of re-probing every cycle.
    """
    return _local_gpu_facts()[1]


def local_dynamic_state(gpu: Optional[dict] = None) -> dict:
    """Cheap dynamic state for heartbeats.

    ``gpu`` is a previously observed :func:`gpu_facts` snapshot. Without one
    the GPU keys are simply absent rather than fabricated: a heartbeat that
    skips the probe reports less, and says so, instead of claiming a fresh
    reading it never took.
    """
    _total, available = _memory_kb()
    return _dynamic_state(dict(gpu or {}), _total, available)
