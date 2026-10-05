"""Multi-dimensional campaign evaluation.

"Task finished" is not an outcome: a repository campaign has to be judged on
correctness, yield, efficiency, reliability, hygiene, integration risk,
observability, and sustainability, and every one of those dimensions needs a
metric with an explicit threshold so a bad number fails the window instead of
being discovered in a retro.

The core here is pure - facts in, scorecard out - so the thresholds are
testable without a repository, a provider, or a clock. Reading the artifacts a
campaign actually writes lives in ``campaign_evaluation_facts``.

A missing metric is reported as ``None`` with a reason, never as zero: an
unmeasurable token budget must not look like a free run.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from datetime import datetime, timezone

DIMENSIONS = ("correctness", "yield", "efficiency", "reliability",
              "hygiene", "integration", "observability", "sustainability")

OK, WATCH, BREACH = "ok", "watch", "breach"
_RANK = {OK: 0, WATCH: 1, BREACH: 2}

DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})")
SEGMENT_RE = re.compile(r"segment #(?P<segment>\d+) ended")
STALL_RE = re.compile(r"STALL BREAKER: (?P<rounds>\d+) consecutive empty rounds")
PREFLIGHT_RE = re.compile(
    r"preflight: (?P<reason>unexpected probe reply|provider blocked)")
QUOTA_RE = re.compile(r"provider-blocked \(hits=(?P<hits>\d+)")
SWEEP_RE = re.compile(r"sweep: removed leaked agent probe (?P<path>\S+)")
PRESERVED_RE = re.compile(r"sweep: preserved tracked probe candidate")
STATUS_RE = re.compile(
    r"\[SUP\] status left=(?P<left>\d+)s .*merged_total=(?P<merged>\d+)")

TERMINAL_STATUSES = ("PASSED", "DIRECTION_REJECTED", "PIPELINE_FAILED",
                     "MERGE_BLOCKED")

# Rejection taxonomy: a direction dropped because the per-module selection
# quota is full is not a quality signal, and an analysis that failed because
# the agent binary vanished is not a model signal. Conflating them made one
# healthy 5-day window look like a 32% pass rate and a 21% failure rate.
_QUOTA_REASONS = ("selection quota", "quota already filled")
_MALFORMED_REASONS = ("missing required field", "invalid", "malformed")
_INFRA_REASONS = ("agent binary not found", "no such file or directory",
                  "executable", "timed out", "timeout", "connection error",
                  "broken pipe", "killed")

# Thresholds are review decisions, not tuning knobs: each one is a level a
# maintainer agreed to watch, so the scorecard can gate the *next* window.
THRESHOLDS: dict[str, tuple[float, float]] = {
    "merge_gate_block_ratio_max": (0.25, 0.50),
    "framework_free_violations_max": (0, 0),
    "forbidden_paths_max": (0, 0),
    "pass_rate_min": (0.35, 0.20),
    "quality_pass_rate_min": (0.60, 0.40),
    "merges_per_day_min": (3.0, 1.0),
    "slot_utilization_min": (0.60, 0.35),
    "stall_segment_ratio_max": (0.35, 0.60),
    "preflight_failure_ratio_max": (0.10, 0.25),
    "analysis_failure_ratio_max": (0.10, 0.25),
    "analysis_infra_failure_ratio_max": (0.02, 0.08),
    "analysis_content_failure_ratio_max": (0.05, 0.15),
    "pipeline_failure_ratio_max": (0.20, 0.35),
    "probe_files_max": (0, 0),
    "dedup_ratio_watch": (0.80, 0.95),
    "yield_halving_watch": (0.50, 0.25),
    "merge_base_age_days_max": (3.0, 7.0),
}


def classify_rejection(reason: str) -> str:
    """Return `quota`, `malformed`, or `quality` for a rejected direction."""
    text = str(reason or "").lower()
    if any(marker in text for marker in _QUOTA_REASONS):
        return "quota"
    if any(marker in text for marker in _MALFORMED_REASONS):
        return "malformed"
    return "quality"


def classify_analysis_failure(reason: str) -> str:
    """Return `infra` for provider/host failures, else `content`."""
    text = str(reason or "").lower()
    return "infra" if any(marker in text for marker in _INFRA_REASONS) else "content"


@dataclass
class GitFacts:
    merges: int = 0
    merge_dates: tuple[str, ...] = ()
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    ahead: int = 0
    behind: int = 0
    merge_base_age_days: float = 0.0
    cross_surface_commits: int = 0
    files_outside_roots: tuple[str, ...] = ()
    framework_free_violations: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()


@dataclass
class SupervisorFacts:
    segments: int = 0
    stall_breaks: int = 0
    preflight_failures: int = 0
    quota_cooldowns: int = 0
    sweeps_removed: int = 0
    preserved_tracked: int = 0
    status_lines: int = 0


@dataclass
class Flag:
    dimension: str
    name: str
    status: str
    detail: str
    value: float | None = None
    threshold: float | None = None

    def as_dict(self) -> dict:
        return {"dimension": self.dimension, "name": self.name,
                "status": self.status, "detail": self.detail,
                "value": self.value, "threshold": self.threshold}


@dataclass
class Scorecard:
    generated_at: str
    window_hours: float
    dimensions: dict[str, dict] = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)

    @property
    def status(self) -> str:
        return max((flag.status for flag in self.flags),
                   key=lambda s: _RANK[s], default=OK)

    @property
    def breaches(self) -> list[Flag]:
        return [flag for flag in self.flags if flag.status == BREACH]

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_hours": round(self.window_hours, 3),
            "status": self.status,
            "dimensions": self.dimensions,
            "flags": [flag.as_dict() for flag in self.flags],
        }


def _band(name: str, value: float | None, *,
          higher_is_better: bool) -> tuple[str, str]:
    """Map a value onto ok/watch/breach using the configured band."""
    watch, breach = THRESHOLDS[name]
    if value is None:
        return WATCH, "unavailable"
    if higher_is_better:
        if value < breach:
            return BREACH, f"{value:.3g} < {breach:g}"
        return (WATCH, f"{value:.3g} < {watch:g}") if value < watch else (
            OK, f"{value:.3g} >= {watch:g}")
    if value > breach:
        return BREACH, f"{value:.3g} > {breach:g}"
    return (WATCH, f"{value:.3g} > {watch:g}") if value > watch else (
        OK, f"{value:.3g} <= {watch:g}")


def _flag(dimension: str, name: str, status: str, detail: str,
          value: float | None = None, threshold: float | None = None) -> Flag:
    return Flag(dimension, name, status, detail, value, threshold)

def _ratio(value: float | None, digits: int) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


_TREND_METRICS = ("yield/merges_per_day", "yield/pass_rate",
                  "yield/quality_pass_rate", "yield/dedup_ratio",
                  "efficiency/concurrency_slot_utilization",
                  "efficiency/median_pipeline_seconds",
                  "efficiency/agent_hours_per_merge",
                  "reliability/stall_segment_ratio",
                  "reliability/analysis_infra_failure_ratio",
                  "reliability/pipeline_failure_ratio",
                  "correctness/merge_gate_block_ratio",
                  "integration/commits_behind",
                  "integration/merge_base_age_days")


def compare(before: Scorecard, after: Scorecard) -> dict:
    """Diff two scorecards so a window can be judged against its predecessor."""
    trend: dict[str, dict] = {}
    for name in _TREND_METRICS:
        dimension, _, key = name.partition("/")
        old = before.dimensions.get(dimension, {}).get(key)
        new = after.dimensions.get(dimension, {}).get(key)
        delta = None
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            delta = round(float(new) - float(old), 4)
        trend[name] = {"before": old, "after": new, "delta": delta}
    trend["status"] = {"from": before.status, "to": after.status,
                       "regressed": _RANK[after.status] > _RANK[before.status]}
    trend["new_flags"] = sorted(
        {flag.name for flag in after.flags} - {flag.name for flag in before.flags})
    trend["cleared_flags"] = sorted(
        {flag.name for flag in before.flags} - {flag.name for flag in after.flags})
    return trend


def scorecard_from_dict(payload: dict) -> Scorecard:
    """Rebuild a scorecard from its JSON form (for trend comparisons)."""
    card = Scorecard(generated_at=str(payload.get("generated_at", "")),
                     window_hours=float(payload.get("window_hours", 0.0)),
                     dimensions=payload.get("dimensions", {}))
    for item in payload.get("flags", []):
        card.flags.append(Flag(
            dimension=str(item.get("dimension", "")),
            name=str(item.get("name", "")), status=str(item.get("status", OK)),
            detail=str(item.get("detail", "")), value=item.get("value"),
            threshold=item.get("threshold")))
    return card


def __getattr__(name: str):
    """Re-export the fact collectors so callers have one import site.

    Imported lazily: the facts module imports this one, so a module-level
    import would be circular.
    """
    if name in {"collect_git_facts", "collect_supervisor_facts",
                "evaluate_campaign", "read_rows", "scan_probe_files"}:
        from . import campaign_evaluation_facts as facts
        return getattr(facts, name)
    if name in {"main", "render_markdown", "render_trend"}:
        from . import campaign_evaluation_report as report
        return getattr(report, name)
    if name in {"evaluate", "evaluate_campaign_metrics"}:
        from . import campaign_evaluation_dimensions as dimensions
        return getattr(dimensions, name)
    raise AttributeError(name)


# ── counts and derived ratios ───────────────────────────────────────────────

@dataclass
class Counts:
    """Row-level counts and their quality/infra taxonomy."""
    statuses: Counter
    rejection_classes: Counter
    failure_classes: Counter

    def get(self, status: str) -> int:
        return self.statuses.get(status, 0)


@dataclass
class Rates:
    """Every derived ratio the dimensions and flags share."""
    merge_gate_block_ratio: float | None
    merge_verdict_blocks: int
    gate_blocks: int
    pass_rate: float
    quality_pass_rate: float
    quota_drop_rate: float
    analysis_failure_ratio: float
    analysis_infra_failure_ratio: float
    analysis_content_failure_ratio: float
    pipeline_failure_ratio: float
    dedup_ratio: float | None
    slot_utilization: float | None
    retry_ratio: float | None
    median_pipeline_seconds: float | None
    analysis_wall_seconds: float
    merges_per_day: float
    analyses_per_merge: float | None
    agent_hours_per_merge: float | None
    tokens_per_merge: float | None
    telemetry: str
    stall_segment_ratio: float | None
    preflight_failure_ratio: float | None
    yield_halving: float | None


def counts_rows(rows: list[dict]) -> Counts:
    return Counts(
        statuses=Counter(row.get("status", "") for row in rows),
        rejection_classes=Counter(
            classify_rejection(row.get("reason", ""))
            for row in rows if row.get("status") == "DIRECTION_REJECTED"),
        failure_classes=Counter(
            classify_analysis_failure(row.get("reason", ""))
            for row in rows if row.get("status") == "ANALYSIS_FAILED"))


def _merge_blocks(rows: list[dict]) -> tuple[int, int]:
    gate = sum(1 for row in rows if row.get("status") == "MERGE_BLOCKED"
               and "gate failed" in str(row.get("reason", "")))
    verdict = sum(1 for row in rows if row.get("status") == "MERGE_BLOCKED"
                  and "VERDICT" in str(row.get("reason", "")))
    return gate, verdict


def metric_rows(rows: list[dict]) -> list[dict]:
    return [row["metrics"] for row in rows
            if row.get("status") == "CAMPAIGN_METRICS"
            and isinstance(row.get("metrics"), dict)]


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def _yield_halving(merge_dates: tuple[str, ...]) -> float | None:
    """Second-half over first-half merges, split at the middle of the date range."""
    stamps = sorted(date for date in merge_dates if len(date) == 10)
    if len(stamps) < 4:
        return None
    first_day = datetime.fromisoformat(stamps[0]).date()
    last_day = datetime.fromisoformat(stamps[-1]).date()
    midpoint = first_day + (last_day - first_day) / 2
    early = sum(1 for date in stamps
                if datetime.fromisoformat(date).date() <= midpoint)
    return (len(stamps) - early) / early if early else None


def _telemetry(metrics: list[dict]) -> str:
    value = next((m.get("analysis_token_telemetry") for m in metrics
                  if m.get("analysis_token_telemetry")), None)
    return str(value) if value else "unknown"


def _observed_tokens(metrics: list[dict], telemetry: str) -> int:
    if telemetry != "observed":
        return 0
    return sum(int(m.get("analysis_tokens") or 0) for m in metrics)


def _pipeline_seconds(rows: list[dict]) -> list[float]:
    return [float(row.get("elapsed") or 0.0) for row in rows
            if row.get("status") in TERMINAL_STATUSES]


def _analysis_wall(metrics: list[dict]) -> float:
    return sum(float(m.get("analysis_wall_seconds") or 0.0) for m in metrics)


def _merge_rates(git, gate_blocks: int, verdict_blocks: int) -> tuple[float | None, int, int]:
    attempts = git.merges + gate_blocks + verdict_blocks
    ratio = gate_blocks / attempts if attempts else None
    return ratio, gate_blocks, verdict_blocks


def _pass_rates(counts: Counts) -> tuple[float, float, float, float, float]:
    passed = counts.get("PASSED")
    rejected = counts.get("DIRECTION_REJECTED")
    failed = counts.get("PIPELINE_FAILED")
    blocked = counts.get("MERGE_BLOCKED")
    quality_rejected = (counts.rejection_classes.get("quality", 0)
                        + counts.rejection_classes.get("malformed", 0))
    attempted = max(1, passed + rejected + failed + blocked)
    quality_attempts = max(1, passed + quality_rejected + failed + blocked)
    analysis_total = max(1, counts.get("ANALYZED") + counts.get("ANALYSIS_FAILED"))
    infra = counts.failure_classes.get("infra", 0)
    analysis_ratio = counts.get("ANALYSIS_FAILED") / analysis_total
    return (passed / attempted, passed / quality_attempts,
            counts.rejection_classes.get("quota", 0) / attempted,
            analysis_ratio, infra / analysis_total)


def _utilization_rates(rows: list[dict], supervisor) -> tuple[float | None, float | None, float | None]:
    metrics = metric_rows(rows)
    return (_median_or_none(
                [float(m.get("concurrency_slot_utilization") or 0.0) for m in metrics
                 if "concurrency_slot_utilization" in m]),
            _median_or_none([float(m.get("retry_ratio") or 0.0) for m in metrics
                             if "retry_ratio" in m]),
            supervisor.stall_breaks / supervisor.segments if supervisor.segments else None)


def derive_rates(rows: list[dict], counts: Counts, supervisor, git, *,
                 window_hours: float) -> Rates:
    """Turn counts plus campaign facts into every ratio the scorecard needs."""
    gate_blocks, verdict_blocks = _merge_blocks(rows)
    block_ratio, gate_blocks, verdict_blocks = _merge_rates(
        git, gate_blocks, verdict_blocks)
    (pass_rate, quality_pass_rate, quota_drop_rate,
     analysis_ratio, infra_ratio) = _pass_rates(counts)
    slot_utilization, retry_ratio, stall_ratio = _utilization_rates(rows, supervisor)
    metrics = metric_rows(rows)
    telemetry = _telemetry(metrics)
    tokens = _observed_tokens(metrics, telemetry)
    pipeline_seconds = _pipeline_seconds(rows)
    analysis_wall = _analysis_wall(metrics)
    merges = git.merges
    deduplicated = counts.get("ANALYSIS_DEDUPLICATED")
    analyzed = counts.get("ANALYZED")
    return Rates(
        merge_gate_block_ratio=block_ratio, merge_verdict_blocks=verdict_blocks,
        gate_blocks=gate_blocks, pass_rate=pass_rate,
        quality_pass_rate=quality_pass_rate, quota_drop_rate=quota_drop_rate,
        analysis_failure_ratio=analysis_ratio,
        analysis_infra_failure_ratio=infra_ratio,
        analysis_content_failure_ratio=max(0.0, analysis_ratio - infra_ratio),
        pipeline_failure_ratio=counts.get("PIPELINE_FAILED") / max(
            1, counts.get("PASSED") + counts.get("DIRECTION_REJECTED")
            + counts.get("PIPELINE_FAILED") + counts.get("MERGE_BLOCKED")),
        dedup_ratio=(deduplicated / (analyzed + deduplicated)
                     if (analyzed + deduplicated) else None),
        slot_utilization=slot_utilization, retry_ratio=retry_ratio,
        median_pipeline_seconds=_median_or_none(pipeline_seconds),
        analysis_wall_seconds=analysis_wall,
        merges_per_day=merges / max(0.25, window_hours / 24.0),
        analyses_per_merge=(analyzed / merges) if merges else None,
        agent_hours_per_merge=((analysis_wall + sum(pipeline_seconds)) / 3600.0 / merges
                               if merges else None),
        tokens_per_merge=(tokens / merges) if (telemetry == "observed" and merges) else None,
        telemetry=telemetry,
        stall_segment_ratio=stall_ratio,
        preflight_failure_ratio=(supervisor.preflight_failures / supervisor.segments
                                 if supervisor.segments else None),
        yield_halving=_yield_halving(git.merge_dates),
    )
