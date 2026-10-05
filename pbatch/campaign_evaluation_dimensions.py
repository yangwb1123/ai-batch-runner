"""Campaign scorecard dimensions.

Each dimension is a small builder over the derived :class:`Rates`, and
:func:`evaluate` assembles them into the eight-dimension scorecard plus the
threshold flags. Split from ``campaign_evaluation`` so the primitives stay
readable and every file fits the size budget.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .campaign_evaluation import (BREACH, OK, THRESHOLDS, WATCH, Counts,
                                  Flag, GitFacts, Rates, Scorecard, _band,
                                  _flag, _ratio, counts_rows, derive_rates,
                                  metric_rows)


def __getattr__(name: str):
    if name == "DIMENSIONS":
        from .campaign_evaluation import DIMENSIONS
        return DIMENSIONS
    raise AttributeError(name)


def _correctness(git, rates: Rates) -> dict:
    return {
        "merged_directions": git.merges,
        "merge_gate_blocks": rates.gate_blocks,
        "merge_verdict_blocks": rates.merge_verdict_blocks,
        "merge_gate_block_ratio": _ratio(rates.merge_gate_block_ratio, 4),
        "framework_free_violations": len(git.framework_free_violations),
        "forbidden_paths_changed": len(git.forbidden_paths),
        "framework_free_samples": list(git.framework_free_violations[:3]),
        "forbidden_samples": list(git.forbidden_paths[:3]),
    }


def _yield(counts: Counts, rates: Rates, git) -> dict:
    return {
        "analyses": counts.get("ANALYZED"),
        "analysis_failures": counts.get("ANALYSIS_FAILED"),
        "directions_passed": counts.get("PASSED"),
        "directions_rejected": counts.get("DIRECTION_REJECTED"),
        "pipeline_failures": counts.get("PIPELINE_FAILED"),
        "merge_blocks": counts.get("MERGE_BLOCKED"),
        "pass_rate": round(rates.pass_rate, 4),
        "quality_pass_rate": round(rates.quality_pass_rate, 4),
        "rejections_by_class": dict(sorted(counts.rejection_classes.items())),
        "quota_drop_rate": round(rates.quota_drop_rate, 4),
        "merges_per_day": round(rates.merges_per_day, 3),
        "files_changed": git.files_changed,
        "insertions": git.insertions,
        "deletions": git.deletions,
        "analyses_per_merge": _ratio(rates.analyses_per_merge, 2),
        "dedup_ratio": _ratio(rates.dedup_ratio, 4),
    }


def _efficiency(rates: Rates) -> dict:
    return {
        "concurrency_slot_utilization": _ratio(rates.slot_utilization, 4),
        "median_pipeline_seconds": _ratio(rates.median_pipeline_seconds, 1),
        "analysis_wall_seconds": round(rates.analysis_wall_seconds, 1),
        "agent_hours_per_merge": _ratio(rates.agent_hours_per_merge, 3),
        "tokens_per_merge": _ratio(rates.tokens_per_merge, 0),
        "token_telemetry": rates.telemetry,
        "token_note": ("provider reports no usage telemetry; token cost per "
                       "merge is unmeasurable, not zero"),
    }


def _reliability(counts: Counts, supervisor, rates: Rates) -> dict:
    return {
        "campaign_runs_started": counts.get("CAMPAIGN_RUN_STARTED"),
        "segments": supervisor.segments,
        "stall_breaks": supervisor.stall_breaks,
        "stall_segment_ratio": _ratio(rates.stall_segment_ratio, 4),
        "preflight_failures": supervisor.preflight_failures,
        "preflight_failure_ratio": _ratio(rates.preflight_failure_ratio, 4),
        "provider_quota_cooldowns": supervisor.quota_cooldowns,
        "analysis_failure_ratio": round(rates.analysis_failure_ratio, 4),
        "analysis_failures_by_class": dict(sorted(counts.failure_classes.items())),
        "analysis_infra_failure_ratio": round(rates.analysis_infra_failure_ratio, 4),
        "pipeline_failure_ratio": round(rates.pipeline_failure_ratio, 4),
        "retry_ratio": _ratio(rates.retry_ratio, 4),
    }


def _hygiene(supervisor, probe_files: int, probe_tracked: int,
             worktree_count: int, disk_bytes: int) -> dict:
    return {
        "agent_probe_files_removed": supervisor.sweeps_removed,
        "tracked_probe_candidates_preserved": supervisor.preserved_tracked,
        "probe_files_remaining": probe_files,
        "probe_files_tracked": probe_tracked,
        "direction_worktrees": worktree_count,
        "campaign_disk_bytes": disk_bytes,
    }


def _integration(git, upstream: str) -> dict:
    return {
        "upstream": upstream or "(none)",
        "commits_ahead": git.ahead,
        "commits_behind": git.behind,
        "merge_base_age_days": round(git.merge_base_age_days, 2),
        "cross_surface_commits": git.cross_surface_commits,
        "files_outside_declared_roots": list(git.files_outside_roots),
    }


def _observability(rows: list[dict], rates: Rates, supervisor) -> dict:
    return {
        "state_rows": len(rows),
        "metrics_snapshots": len(metric_rows(rows)),
        "supervisor_status_lines": supervisor.status_lines,
        "unmeasurable": [name for name, value in (
            ("tokens_per_merge", rates.tokens_per_merge),
            ("concurrency_slot_utilization", rates.slot_utilization),
            ("retry_ratio", rates.retry_ratio),
            ("merge_gate_block_ratio", rates.merge_gate_block_ratio),
            ("stall_segment_ratio", rates.stall_segment_ratio),
        ) if value is None],
    }


def _sustainability(rates: Rates, git) -> dict:
    return {
        "merge_history_length": len(git.merge_dates),
        "yield_second_half_ratio": _ratio(rates.yield_halving, 3),
        "dedup_ratio": _ratio(rates.dedup_ratio, 4),
        "coverage_note": ("analysis dedup ratio rising means the module "
                          "surface is saturating"),
    }


def _flags(git, rates: Rates, supervisor, probe_files: int,
           upstream: str) -> list[Flag]:
    """One flag per thresholded metric, plus the observability caveats."""
    bands = [
        ("correctness", "framework_free_violations",
         float(len(git.framework_free_violations)), "framework_free_violations_max", False),
        ("correctness", "forbidden_paths", float(len(git.forbidden_paths)),
         "forbidden_paths_max", False),
        ("correctness", "merge_gate_block_ratio", rates.merge_gate_block_ratio,
         "merge_gate_block_ratio_max", False),
        ("yield", "pass_rate", rates.pass_rate, "pass_rate_min", True),
        ("yield", "quality_pass_rate", rates.quality_pass_rate,
         "quality_pass_rate_min", True),
        ("yield", "merges_per_day", rates.merges_per_day, "merges_per_day_min", True),
        ("efficiency", "concurrency_slot_utilization", rates.slot_utilization,
         "slot_utilization_min", True),
        ("reliability", "stall_segment_ratio", rates.stall_segment_ratio,
         "stall_segment_ratio_max", False),
        ("reliability", "preflight_failure_ratio", rates.preflight_failure_ratio,
         "preflight_failure_ratio_max", False),
        ("reliability", "analysis_infra_failure_ratio",
         rates.analysis_infra_failure_ratio,
         "analysis_infra_failure_ratio_max", False),
        ("reliability", "analysis_content_failure_ratio",
         rates.analysis_content_failure_ratio,
         "analysis_content_failure_ratio_max", False),
        ("reliability", "pipeline_failure_ratio", rates.pipeline_failure_ratio,
         "pipeline_failure_ratio_max", False),
        ("hygiene", "probe_files_remaining", float(probe_files),
         "probe_files_max", False),
        ("sustainability", "dedup_ratio", rates.dedup_ratio, "dedup_ratio_watch", False),
        ("sustainability", "yield_second_half_ratio", rates.yield_halving,
         "yield_halving_watch", True),
        ("integration", "merge_base_age_days", git.merge_base_age_days,
         "merge_base_age_days_max", False),
    ]
    flags = []
    for dimension, name, value, threshold, higher in bands:
        status, detail = _band(threshold, value, higher_is_better=higher)
        flags.append(_flag(dimension, name, status, detail, value,
                           THRESHOLDS[threshold][0]))
    if rates.telemetry != "observed":
        flags.append(_flag("observability", "token_telemetry", WATCH,
                           "provider reports no token usage; cost per merge "
                           "is unmeasurable"))
    if not upstream:
        flags.append(_flag("integration", "upstream_unknown", WATCH,
                           "no upstream branch configured; divergence is "
                           "unmeasured"))
    return flags


def evaluate(rows: list[dict], supervisor, git, *, window_hours: float,
             probe_files: int = 0, probe_tracked: int = 0, worktree_count: int = 0,
             disk_bytes: int = 0, upstream: str = "",
             now: datetime | None = None) -> Scorecard:
    """Score one campaign window across every dimension. Pure: no I/O."""
    counts = counts_rows(rows)
    rates = derive_rates(rows, counts, supervisor, git,
                       window_hours=window_hours)
    card = Scorecard(
        generated_at=(now or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"),
        window_hours=max(0.0, window_hours))
    card.dimensions = {
        "correctness": _correctness(git, rates),
        "yield": _yield(counts, rates, git),
        "efficiency": _efficiency(rates),
        "reliability": _reliability(counts, supervisor, rates),
        "hygiene": _hygiene(supervisor, probe_files, probe_tracked,
                            worktree_count, disk_bytes),
        "integration": _integration(git, upstream),
        "observability": _observability(rows, rates, supervisor),
        "sustainability": _sustainability(rates, git),
    }
    card.flags = _flags(git, rates, supervisor, probe_files, upstream)
    return card


