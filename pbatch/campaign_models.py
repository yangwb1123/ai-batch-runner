"""Data models and configuration defaults for repository campaigns."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_ANALYSIS_PROMPT = """Analyze module '{module}' at {module_path} and its related interfaces in the repository.
Return ONLY a JSON array with at most {candidate_limit} high-value improvement directions, sorted by value.
Each object must contain: title, problem, value (1-10), risk_reduction (1-10), effort (1-10), confidence (1-10), evidence (a JSON array of existing file/symbol paths), and acceptance (a JSON array of testable checks).
Do not modify code. Claims without repository evidence must be identified as proposed, not verified.
"""


def safe_slug(value: str, fallback: str = "campaign", max_length: int = 48) -> str:
    """Return a stable, path-safe identifier that cannot contain traversal."""
    raw = str(value)
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:max_length]
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{base or fallback}-{suffix}"


@dataclass(frozen=True)
class Direction:
    module: str
    title: str
    problem: str = ""
    value: float = 0
    risk_reduction: float = 0
    effort: float = 0
    confidence: float = 0
    evidence: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def score(self) -> float:
        return (self.value * 0.35 + self.risk_reduction * 0.25 +
                self.confidence * 0.20 - self.effort * 0.20)

    @property
    def direction_id(self) -> str:
        base = re.sub(r"[^A-Za-z0-9]+", "-", self.title.lower()).strip("-")[:48] or "direction"
        suffix = hashlib.sha256((self.module + "\0" + self.title).encode("utf-8")).hexdigest()[:8]
        return f"{base}-{suffix}"

    @property
    def implementable(self) -> bool:
        return bool(self.title and self.problem and self.evidence and self.acceptance)

    @classmethod
    def from_mapping(cls, module: str, data: dict) -> "Direction":
        return cls(
            module=module,
            title=str(data.get("title", "")).strip(),
            problem=str(data.get("problem", "")).strip(),
            value=_number(data.get("value", 0)),
            risk_reduction=_number(data.get("risk_reduction", data.get("risk", 0))),
            effort=_number(data.get("effort", 0)),
            confidence=_number(data.get("confidence", 0)),
            evidence=_strings(data.get("evidence", [])),
            acceptance=_strings(data.get("acceptance", [])),
            raw=dict(data),
        )


def _number(value) -> float:
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _strings(value) -> tuple[str, ...]:
    """Normalize a list of strings; tolerate a single string (LLMs
    sometimes emit evidence/acceptance as one string instead of an array)
    by splitting on newlines and semicolons."""
    if isinstance(value, str):
        value = re.split(r"[\n;]+", value)
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


@dataclass
class CampaignSettings:
    name: str = "repository-improvements"
    roots: tuple[str, ...] = ("domains", "interfaces", "infrastructure", "platform", "protocols", "shared")
    excludes: tuple[str, ...] = ("testdata", "gen", "generated", "mock", "defaultimpl",
                                 "tests", "docs", "examples", "prompts", "pipelines", "logs",
                                 "vendor", "node_modules", "target", ".venv", ".git", ".github",
                                 ".pi-batch", "__pycache__")
    max_depth: int = 1
    fallback_top_level: bool = True
    analysis_prompt: str = DEFAULT_ANALYSIS_PROMPT
    analysis_jobs: int = 4
    analysis_retries: int = 2
    analysis_batch_size: int = 0
    analysis_budget_tokens: int = 0
    analysis_token_estimate: int = 8_000
    candidate_limit: int = 3
    max_directions: int = 1
    minimum_score: float = 0
    # Empty values use the private per-workspace runtime root.  Explicit
    # paths remain supported for projects that intentionally version reports.
    output_dir: str = ""
    state_file: str = ""
    summary_file: str = ""
    state_line_max_bytes: int = 256 * 1024
    pipeline_template: str = "examples/repository-campaign-pipeline.yaml"
    requirement_stage: str = "requirements"
    pipeline_concurrency: int = 1
    pipeline_timeout: int = 4 * 60 * 60
    # 0 disables the evidence-based stall watchdog; the hard timeout remains.
    pipeline_no_progress_timeout: int = 0
    worktree_root: str = ""  # 空 = data_dir/worktrees（PBATCH_DATA_DIR 感知）
    analysis_minutes: float = 3
    pipeline_minutes: float = 20

    def effective_worktree_root(self, root: Path) -> Path:
        """worktree 根：显式配置优先；空 = data_dir/worktrees（可移出项目）。"""
        from .config import data_dir
        if self.worktree_root:
            return self.path(root, self.worktree_root)
        return data_dir(str(root)) / "worktrees"

    @classmethod
    def from_mapping(cls, data: dict) -> "CampaignSettings":
        discovery = _section(data, "discovery")
        analysis = _section(data, "analysis")
        selection = _section(data, "selection")
        implementation = _section(data, "implementation")
        state = _section(data, "state")
        estimates = _section(data, "estimates")
        return cls(
            name=_string_value(data, "name", "repository-improvements"),
            roots=_string_list(discovery, "roots", cls.roots),
            excludes=_string_list(discovery, "exclude", cls.excludes),
            max_depth=_int_value(discovery, "max_depth", 1),
            fallback_top_level=_bool_value(discovery, "fallback_top_level", True),
            analysis_prompt=_string_value(analysis, "prompt", DEFAULT_ANALYSIS_PROMPT),
            analysis_jobs=_int_value(analysis, "jobs", 4),
            analysis_retries=_int_value(analysis, "retries", 2),
            analysis_batch_size=_int_value(analysis, "batch_size", 0),
            analysis_budget_tokens=_int_value(analysis, "budget_tokens", 0),
            analysis_token_estimate=_int_value(analysis, "token_estimate", 8_000),
            candidate_limit=_int_value(analysis, "candidate_limit", 3),
            max_directions=_int_value(selection, "max_directions", 1),
            minimum_score=_float_value(selection, "minimum_score", 0),
            output_dir=_string_value(state, "output_dir", ""),
            state_file=_string_value(state, "state_file", ""),
            summary_file=_string_value(state, "summary_file", ""),
            state_line_max_bytes=_int_value(state, "line_max_bytes", 256 * 1024),
            pipeline_template=_string_value(implementation, "pipeline", "examples/repository-campaign-pipeline.yaml"),
            requirement_stage=_string_value(implementation, "requirement_stage", "requirements"),
            pipeline_concurrency=_int_value(implementation, "concurrency", 1),
            pipeline_timeout=_int_value(implementation, "timeout", 4 * 60 * 60),
            pipeline_no_progress_timeout=_int_value(
                implementation, "no_progress_timeout", 0),
            worktree_root=_string_value(implementation, "worktree_root", ""),
            analysis_minutes=_float_value(estimates, "analysis_minutes", 3),
            pipeline_minutes=_float_value(estimates, "pipeline_minutes", 20),
        )

    def validate(self) -> None:
        positive = {
            "discovery.max_depth": self.max_depth,
            "analysis.jobs": self.analysis_jobs,
            "analysis.token_estimate": self.analysis_token_estimate,
            "analysis.candidate_limit": self.candidate_limit,
            "selection.max_directions": self.max_directions,
            "implementation.concurrency": self.pipeline_concurrency,
            "implementation.timeout": self.pipeline_timeout,
            "state.line_max_bytes": self.state_line_max_bytes,
        }
        invalid = [name for name, value in positive.items() if value < 1]
        for name, value in (("analysis.batch_size", self.analysis_batch_size),
                            ("analysis.budget_tokens", self.analysis_budget_tokens)):
            if value < 0:
                invalid.append(name)
        if self.analysis_retries < 0:
            invalid.append("analysis.retries")
        if self.pipeline_no_progress_timeout < 0:
            invalid.append("implementation.no_progress_timeout")
        finite = {"selection.minimum_score": self.minimum_score,
                  "estimates.analysis_minutes": self.analysis_minutes,
                  "estimates.pipeline_minutes": self.pipeline_minutes}
        invalid.extend(name for name, value in finite.items() if not math.isfinite(value))
        if self.analysis_minutes <= 0:
            invalid.append("estimates.analysis_minutes")
        if self.pipeline_minutes <= 0:
            invalid.append("estimates.pipeline_minutes")
        if invalid:
            raise ValueError("invalid campaign values: " + ", ".join(invalid))

    def runtime_path(self, root: Path) -> Path:
        """Private campaign artifact root for this workspace."""
        from .config import data_dir
        return data_dir(str(root)) / "campaign"

    def output_path(self, root: Path) -> Path:
        """Resolve campaign output, defaulting outside the repository."""
        return self.path(root, self.output_dir) if self.output_dir else self.runtime_path(root)

    def state_path(self, root: Path) -> Path:
        """Resolve the append-only campaign state file."""
        return self.path(root, self.state_file) if self.state_file else self.runtime_path(root) / "state.jsonl"

    def summary_path(self, root: Path) -> Path:
        """Resolve the human-readable summary outside the repository by default."""
        return self.path(root, self.summary_file) if self.summary_file else self.runtime_path(root) / "SUMMARY.md"

    def path(self, root: Path, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (root / path).resolve()


def _section(data: dict, name: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("campaign config must be a mapping")
    if name not in data:
        return {}
    value = data[name]
    if not isinstance(value, dict):
        raise ValueError(f"campaign section '{name}' must be a mapping")
    return value


def _value(section: dict, key: str, default):
    return section[key] if key in section else default


def _string_value(section: dict, key: str, default: str) -> str:
    value = _value(section, key, default)
    if not isinstance(value, str):
        raise ValueError(f"campaign {key} must be a string")
    return value


def _string_list(section: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = _value(section, key, default)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"campaign {key} must be a string list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"campaign {key} must contain non-empty strings")
    return tuple(item.strip() for item in value)


def _bool_value(section: dict, key: str, default: bool) -> bool:
    value = _value(section, key, default)
    if not isinstance(value, bool):
        raise ValueError(f"campaign {key} must be boolean")
    return value


def _int_value(section: dict, key: str, default: int) -> int:
    value = _value(section, key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"campaign {key} must be an integer")
    return value


def _float_value(section: dict, key: str, default: float) -> float:
    value = _value(section, key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"campaign {key} must be a number")
    return float(value)
