#!/usr/bin/env python3
"""Declarative gate configuration from engineering.yaml.

Thresholds and ignore patterns for every check live in engineering.yaml at
the repo root (or cwd). Missing file / missing PyYAML -> built-in defaults
below, so the harness still runs standalone with zero config — copy
cli.py + checks/ + engineering.yaml into another project and edit the YAML
only. Importers must use `from checks.config import get_config` (a fresh
dict per call) or read the module constants AFTER importing this module —
never `from checks.config import MAX_LINES` before the file is parsed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None


def _resolve_config_path() -> Path:
    """engineering.yaml next to checks/ (repo root) wins; cwd is fallback."""
    here = Path(__file__).resolve().parent.parent / "engineering.yaml"
    cwd = Path.cwd() / "engineering.yaml"
    return here if here.exists() else cwd


def _load_yaml() -> dict:
    path = _resolve_config_path()
    if not yaml or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        # PyYAML raises yaml.YAMLError, while the zero-dependency fallback
        # can raise OSError/ValueError. Keep one actionable contract for both.
        raise ValueError(f"{path}: invalid engineering.yaml: {exc}") from exc
    return data if isinstance(data, dict) else {}


@dataclass
class FilesizeGate:
    max_lines: int = 500
    ignore_patterns: list = field(default_factory=list)
    exemptions: list = field(default_factory=list)


@dataclass
class ComplexityGate:
    max_cyclomatic: int = 15
    max_cognitive: int = 20
    ignore_patterns: list = field(default_factory=list)


@dataclass
class RootPolicyGate:
    max_files: int = 15
    allowed_files: list = field(default_factory=list)
    banned_patterns: list = field(default_factory=list)


@dataclass
class StyleGate:
    max_line_length: int = 100
    ignore_patterns: list = field(default_factory=list)


@dataclass
class Config:
    project_name: str = "project"
    language: str = "python"
    filesize: FilesizeGate = field(default_factory=FilesizeGate)
    complexity: ComplexityGate = field(default_factory=ComplexityGate)
    root_policy: RootPolicyGate = field(default_factory=RootPolicyGate)
    style: StyleGate = field(default_factory=StyleGate)


def _section(raw: dict, key: str) -> dict:
    """Return the raw dict for one gate section (empty when absent/malformed)."""
    d = raw.get(key) or {}
    return d if isinstance(d, dict) else {}


def _load_project(cfg: Config, raw: dict) -> None:
    proj = _section(raw, "project")
    cfg.project_name = str(proj.get("name", cfg.project_name))
    cfg.language = str(proj.get("language", cfg.language))


def _load_filesize(cfg: Config, raw: dict) -> None:
    fs = _section(raw, "filesize")
    cfg.filesize.max_lines = _integer(fs, "max_lines", cfg.filesize.max_lines, "filesize")
    cfg.filesize.ignore_patterns = list(fs.get("ignore_patterns") or [])
    cfg.filesize.exemptions = list(fs.get("exemptions") or [])


def _load_complexity(cfg: Config, raw: dict) -> None:
    cx = _section(raw, "complexity")
    cfg.complexity.max_cyclomatic = _integer(
        cx, "max_cyclomatic", cfg.complexity.max_cyclomatic, "complexity")
    cfg.complexity.max_cognitive = _integer(
        cx, "max_cognitive", cfg.complexity.max_cognitive, "complexity")
    cfg.complexity.ignore_patterns = list(cx.get("ignore_patterns") or [])


def _load_root_policy(cfg: Config, raw: dict) -> None:
    rp = _section(raw, "root_policy")
    cfg.root_policy.max_files = _integer(rp, "max_files", cfg.root_policy.max_files, "root_policy")
    cfg.root_policy.allowed_files = list(rp.get("allowed_files") or [])
    cfg.root_policy.banned_patterns = list(rp.get("banned_patterns") or [])


def _load_style(cfg: Config, raw: dict) -> None:
    st = _section(raw, "style")
    cfg.style.max_line_length = _integer(
        st, "max_line_length", cfg.style.max_line_length, "style")
    cfg.style.ignore_patterns = list(st.get("ignore_patterns") or [])


def get_config() -> Config:
    """Parse engineering.yaml into a Config (built-in defaults when absent)."""
    raw = _load_yaml()
    cfg = Config()
    _load_project(cfg, raw)
    _load_filesize(cfg, raw)
    _load_complexity(cfg, raw)
    _load_root_policy(cfg, raw)
    _load_style(cfg, raw)
    return cfg


def _integer(section: dict, key: str, default: int, label: str) -> int:
    value = section.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{label}.{key} must be an integer (got {value!r})") from exc


def repo_root() -> Path:
    """Repo root = directory containing engineering.yaml (else cwd)."""
    return _resolve_config_path().parent


# Directory segments excluded from gate scan discovery (matched at any depth,
# exact segment name — not substring). Mirrors engineering.yaml's declared
# scope: "quality rules apply to the code we author here".
DEFAULT_EXCLUDED_SEGMENTS = ("build", "dist", ".git", "__pycache__", "logs")
DEFAULT_EXCLUDED_SUFFIXES = (".egg-info",)   # matches ai_batch_runner.egg-info


def scan_files(root: Path, suffix: str = ".py",
               exclude_segments: tuple = DEFAULT_EXCLUDED_SEGMENTS,
               exclude_suffixes: tuple = DEFAULT_EXCLUDED_SUFFIXES) -> list:
    """Deterministic, sorted list of absolute Paths under `root` whose name
    ends with `suffix`, never descending into excluded directory segments.

    Replaces today's `sorted(root.rglob(...))` scan discovery for the three
    rglob gates. Prunes excluded directories before descending (cheap walk),
    does not follow symlinked directories, and sorts the result (same
    ordering as today's `sorted(rglob(...))`). Exclusion is decided purely
    by directory segment names relative to `root` (a `build/` outside root
    never matters). Returns absolute Paths, matching rglob-from-absolute-root.
    """
    def _excluded(name: str) -> bool:
        return name in exclude_segments or name.endswith(exclude_suffixes)

    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _excluded(d)]  # prune (topdown)
        for name in filenames:
            if name.endswith(suffix):
                found.append(Path(dirpath) / name)
    return sorted(found)


def is_ignored(rel: str, patterns: list) -> bool:
    """Substring match against ignore patterns (snaplink semantics)."""
    return any(p in rel for p in patterns)


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")
