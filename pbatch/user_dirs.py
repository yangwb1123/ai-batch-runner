"""用户级目录规范（XDG 兼容）——产品 CLI 的用户工作目录。

分层（对齐 XDG Base Directory）：
- config: ~/.config/pbatch/   （用户配置 settings.yaml）
- cache:  ~/.cache/pbatch/    （可丢弃缓存：SSH 探测、规则缓存）
- data:   ~/.local/share/pbatch/ （设备密钥、设备状态、兼容全局日志）
- runtime: ~/.abr/            （按 workspace 隔离的会话、报告、日志、状态）

兼容回退：XDG 变量未设置时按规范默认；显式环境变量
PBATCH_CONFIG_HOME / PBATCH_CACHE_HOME / PBATCH_DATA_HOME 优先。
另有统一 PBATCH_HOME（如 ~/.pbatch）——设置时三个目录都收拢到其下
（老式单目录习惯）。

原则：运行时状态（会话、memory 索引、被拒诊断、campaign 报告/日志）默认
留在用户级 Codex 目录；只有用户明确指定的交付物/项目知识文档才写回项目。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# 环境变量（统一收拢用；未设置时走 XDG 规范）
PBATCH_HOME_ENV = "PBATCH_HOME"
PBATCH_RUNTIME_HOME_ENV = "PBATCH_RUNTIME_HOME"
CODEX_HOME_ENV = "CODEX_HOME"
XDG_CONFIG_HOME = "XDG_CONFIG_HOME"
XDG_CACHE_HOME = "XDG_CACHE_HOME"
XDG_DATA_HOME = "XDG_DATA_HOME"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _xdg(name: str, fallback_dir: str) -> Path:
    value = os.environ.get(name, "")
    if value:
        return Path(value)
    return _home() / fallback_dir


def pbatch_config_home() -> Path:
    """~/.config/pbatch（或 PBATCH_HOME/.config 统一模式）。"""
    unified = os.environ.get(PBATCH_HOME_ENV, "")
    if unified:
        return Path(unified) / ".config"
    return _xdg(XDG_CONFIG_HOME, ".config") / "pbatch"


def pbatch_cache_home() -> Path:
    """~/.cache/pbatch（探测缓存等可丢弃数据）。"""
    unified = os.environ.get(PBATCH_HOME_ENV, "")
    if unified:
        return Path(unified) / ".cache"
    return _xdg(XDG_CACHE_HOME, ".cache") / "pbatch"


def pbatch_data_home() -> Path:
    """~/.local/share/pbatch（设备密钥/状态/全局日志）。"""
    unified = os.environ.get(PBATCH_HOME_ENV, "")
    if unified:
        return Path(unified) / ".local" / "share"
    return _xdg(XDG_DATA_HOME, ".local/share") / "pbatch"


def ensure_user_dirs() -> dict:
    """创建用户级目录骨架；返回配置、缓存、数据和 runtime 路径。"""
    dirs = {
        "config": pbatch_config_home(),
        "cache": pbatch_cache_home(),
        "data": pbatch_data_home(),
        "runtime": pbatch_runtime_home(),
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def user_settings_path() -> Path:
    """~/.config/pbatch/settings.yaml（用户级默认覆盖）。"""
    return pbatch_config_home() / "settings.yaml"


def keys_dir() -> Path:
    """设备身份密钥目录（用户级，跨项目复用）。"""
    return pbatch_data_home() / "keys"


def global_logs_dir() -> Path:
    """全局运行日志（无项目时的兜底落点）。"""
    return pbatch_data_home() / "logs"


def pbatch_runtime_home() -> Path:
    """Return the private runtime root used by installed/binary releases.

    Runtime artifacts belong in the user's own directory, not in the target
    repository.  ``PBATCH_RUNTIME_HOME`` is the explicit override; when
    ``CODEX_HOME`` is configured we create a separate ``pbatch`` namespace
    below it.  The default is ``~/.abr``.
    """
    configured = os.environ.get(PBATCH_RUNTIME_HOME_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = os.environ.get(CODEX_HOME_ENV, "").strip()
    if codex_home:
        return (Path(codex_home).expanduser() / "pbatch").resolve()
    unified = os.environ.get(PBATCH_HOME_ENV, "").strip()
    if unified:
        return (Path(unified).expanduser() / "runtime").resolve()
    return (_home() / ".abr").resolve()


def project_runtime_dir(cwd: str = "") -> Path:
    """Return a stable, private runtime directory for one workspace.

    Only a bounded basename and a digest of the resolved workspace are used;
    absolute project paths and user input are never copied into the filename.
    """
    root = Path(cwd or os.getcwd()).expanduser().resolve()
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip(".-")[:32]
    label = label or "workspace"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return pbatch_runtime_home() / "projects" / f"{label}-{digest}"
