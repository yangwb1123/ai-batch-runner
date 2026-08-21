"""Lightweight prompt/domain policy for progressive message memory."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from . import config

SENSITIVITY_LEVELS = ("public", "internal", "confidential", "secret", "regulated")


def _project_root(cwd: str = "") -> Path:
    """Resolve a stable project identity without exposing the absolute path."""
    start = Path(cwd or os.getcwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / "pi-batch.yaml").is_file():
            return candidate
    return start


def _project_identity(root_text: str) -> Path:
    """Keep the primary worktree's existing identity in linked worktrees.

    A normal checkout hashes its top-level path, preserving the established
    scope id.  A linked worktree has a ``.git`` file; Git validates that file
    and returns the shared common directory, whose ``.git`` parent is the
    primary checkout.  Invalid/non-standard layouts fall back to the current
    root, so an unverified marker can never widen memory visibility.
    """
    root = Path(root_text).resolve()
    marker = root / ".git"
    if marker.is_dir() or not marker.is_file():
        return root
    try:
        stat = marker.stat()
    except OSError:
        return root
    return _linked_project_identity(
        str(root), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=128)
def _linked_project_identity(root_text: str, *_marker_signature: int) -> Path:
    """Resolve one linked-worktree identity; marker metadata invalidates cache."""
    root = Path(root_text).resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel",
             "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return root
    lines = proc.stdout.splitlines() if proc.returncode == 0 else []
    if len(lines) != 3 or Path(lines[0]).resolve() != root:
        return root
    git_dir = _resolved_git_path(root, lines[1])
    common = _resolved_git_path(root, lines[2])
    if (_valid_worktree_admin(root, git_dir, common)
            and common.name == ".git"):
        return common.parent.resolve()
    return root


def _resolved_git_path(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _valid_worktree_admin(root: Path, git_dir: Path, common: Path) -> bool:
    """Require Git's linked-worktree admin directory and exact back-reference."""
    if not git_dir.is_dir() or not common.is_dir():
        return False
    if git_dir.parent.resolve() != (common / "worktrees").resolve():
        return False
    backlink = git_dir / "gitdir"
    try:
        with backlink.open(encoding="utf-8") as handle:
            value = handle.read(4097)
    except (OSError, UnicodeError):
        return False
    if len(value) > 4096 or not value.strip():
        return False
    marker = Path(value.strip())
    marker = marker if marker.is_absolute() else git_dir / marker
    return marker.resolve() == (root / ".git").resolve()


def memory_scope(cwd: str = "") -> dict:
    """Return the explicit tenant/project/sensitivity memory boundary.

    Environment variables are read per call so a long-running coordinator can
    switch an isolated worker scope without re-importing the package.  The
    project identity is a one-way digest; raw paths remain available only in
    the existing trusted read-safety field, never as a scope selector.
    """
    tenant = os.environ.get("PBATCH_MEMORY_TENANT", "").strip() \
        or str(config.MEMORY_TENANT)
    sensitivity = os.environ.get("PBATCH_MEMORY_SENSITIVITY", "").strip().lower() \
        or str(config.MEMORY_SENSITIVITY).lower()
    if not tenant or len(tenant) > 128 or "\n" in tenant:
        raise ValueError("memory tenant must be a non-empty single-line value")
    if sensitivity not in SENSITIVITY_LEVELS:
        raise ValueError(f"memory sensitivity must be one of {SENSITIVITY_LEVELS}")
    root = _project_root(cwd)
    identity = _project_identity(str(root))
    project_id = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:24]
    return {"tenant": tenant, "project_id": project_id,
            "sensitivity": sensitivity}


def scope_matches(event: dict, expected: dict) -> bool:
    """Fail closed for unscoped or differently scoped historical records."""
    value = event.get("scope") if isinstance(event, dict) else None
    return isinstance(value, dict) and value == expected


_DOMAINS = {
    "authentication": ("auth", "login", "logout", "mfa", "identity", "认证", "登录", "身份"),
    "security": ("security", "threat", "attack", "secret", "权限", "安全", "攻击", "漏洞"),
    "protocol": ("protocol", "wire", "packet", "rfc", "协议", "报文", "兼容"),
    "database": ("database", "schema", "sql", "storage", "数据库", "存储", "迁移"),
    "frontend": ("frontend", "ui", "ux", "react", "页面", "界面", "交互"),
    "infrastructure": ("deploy", "docker", "kubernetes", "network", "部署", "运维", "网络"),
    "testing": ("test", "qa", "coverage", "validator", "gate", "测试", "验收", "门禁"),
    "performance": ("performance", "latency", "throughput", "性能", "延迟", "吞吐"),
    "documentation": ("document", "readme", "docs", "文档", "说明"),
    "product": ("requirement", "feature", "product", "需求", "功能", "产品"),
}
_RESUME_WORDS = (
    "continue", "previous", "history", "resume", "继续", "之前", "历史", "上次", "复用")
_STRONG_RESUME_WORDS = ("continue", "resume", "继续", "上次", "复用")
_EXECUTE_WORDS = (
    "implement", "fix", "change", "build", "review", "实现", "修复", "修改", "评审", "重构")


def classify_prompt(prompt: str, mode_hint: str = "") -> dict:
    """Classify a prompt without retrieval or an LLM call."""
    folded = " ".join((prompt or "").casefold().split())
    domains = [name for name, terms in _DOMAINS.items()
               if any(term in folded for term in terms)]
    explicit_execute = any(word in folded for word in _EXECUTE_WORDS)
    short_question = len(folded) < 120 and ("?" in prompt or "？" in prompt)
    resume_hint = any(word in folded for word in _RESUME_WORDS)
    strong_resume = any(word in folded for word in _STRONG_RESUME_WORDS)
    if strong_resume or (resume_hint and not short_question):
        mode = "resume"
    elif explicit_execute:
        mode = "execute"
    elif short_question:
        mode = "direct"
    elif re.search(r"\.[a-z0-9]{1,8}\b", folded):
        mode = "execute"
    else:
        mode = "discover"
    if mode_hint in ("execute", "resume"):
        mode = mode_hint
    return {"mode": mode, "domains": domains}
