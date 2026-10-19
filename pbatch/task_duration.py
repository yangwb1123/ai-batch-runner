"""Backtested empirical CLI-duration priors, never a live ETA or quality ranking."""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

from .task_center_metrics import number

MIN_TRAIN = 5
MIN_TEST = 3
MAX_SAMPLES = 64


def work_fingerprint(task) -> str:
    value = {"prompt": task.prompt, "argv": task.to_cmd(), "env": task.env,
             "cwd": task.workdir(), "stage": task.memory.get("stage", ""),
             "role": task.memory.get("role", ""), "timeout": task.timeout}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def _empty(reason: str) -> dict:
    return {"status": "untracked", "reason": reason, "duration_range_seconds": None,
        "sample_count": 0, "sample_limit": MAX_SAMPLES, "calibration": None,
        "remaining_seconds": None, "eta": None, "confidence": "untracked",
        "prior_remaining_interval_seconds": None, "prior_observed_seconds": None,
        "prior_remaining_basis": None, "not_a_live_eta": True,
        "basis": "same_source_stage_declared_prompt_and_config",
        "limitations": "repository, session state, load and semantic difficulty are not controlled",
        "grants_execution": False, "quality_ranking": "not_performed"}


def _profile(row: dict):
    digest = row.get("work_fingerprint")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    return row["stage"], digest


def build_priors(candidates: list) -> dict:
    groups = defaultdict(list)
    partial_sources = {source["source_id"] for _, _, source, _, partial in candidates if partial}
    for _, _, source, rows, _ in candidates:
        for row in rows:
            profile = _profile(row) if row["kind"] == "agent" else None
            if profile and row["phase"] == "execution" and row.get("spawn_observed") is True:
                groups[(source["source_id"], *profile)].append(row)
    return {key: _prior(rows, key[0] in partial_sources) for key, rows in groups.items()}


def _prior(rows: list, partial: bool) -> dict:
    result = _empty("partial_observation_window" if partial else "insufficient_completed_samples")
    completed = [row for row in rows if row["status"] not in {"running", "queued", "retrying"}]
    samples = [row for row in completed if row["status"] == "succeeded" and row["ts"] is not None
               and number(row["elapsed"]) is not None and row["elapsed"] > 0]
    result["excluded_terminal_observations"] = len(completed) - len(samples)
    result["omitted_older_samples"] = max(0, len(samples) - MAX_SAMPLES)
    samples.sort(key=lambda row: row["ts"])
    samples = samples[-MAX_SAMPLES:]
    result["sample_count"] = len(samples)
    if partial:
        return result
    if result["excluded_terminal_observations"]:
        result["reason"] = "failed_or_unmeasured_attempts_prevent_success_only_bias"
        return result
    if len(samples) < MIN_TRAIN + MIN_TEST:
        return result
    durations = [row["elapsed"] for row in samples]
    tests = [_contains(durations[:index], durations[index]) for index in range(MIN_TRAIN, len(durations))]
    coverage = sum(tests) / len(tests)
    result["calibration"] = {"method": "chronological_rolling_holdout", "test_samples": len(tests),
                             "covered_samples": sum(tests), "observed_coverage": coverage,
                             "coverage_target": 0.8, "statistical_guarantee": False}
    if coverage < 0.8:
        result["reason"] = "historical_interval_failed_backtest"
        return result
    result.update(status="historical_prior_only", reason="live_context_and_liveness_not_verified",
                  duration_range_seconds=_interval(durations), confidence="limited_backtest_only")
    return result


def _interval(values: list) -> list:
    ordered = sorted(values)
    return [ordered[math.floor((len(ordered) - 1) * 0.1)],
            ordered[math.ceil((len(ordered) - 1) * 0.9)]]


def _contains(train: list, actual: float) -> bool:
    lower, upper = _interval(train)
    return lower <= actual <= upper


def forecast_for(source_id: str, row: dict, priors: dict, *, now=None) -> dict:
    profile = _profile(row)
    if row["kind"] != "agent" or profile is None:
        return _empty("execution_profile_untracked")
    result = dict(priors.get((source_id, *profile), _empty("insufficient_completed_samples")))
    # Completed calls get no retrospective "prediction" trained on themselves.
    if row["status"] not in {"running", "queued", "retrying"}:
        result = _empty("attempt_already_reported_terminal")
    else:
        _prior_remaining(result, row, now)
    return result


def _prior_remaining(result: dict, row: dict, now) -> None:
    """Bounded prior interval minus observed elapsed; never a live ETA.

    ``remaining_seconds``/``eta`` stay null: only an explicitly labelled
    backtested fingerprint interval is reported, and only when the start of the
    current observation and its spawn evidence are actually recorded.
    """
    if result.get("status") != "historical_prior_only" or not result.get("duration_range_seconds"):
        return
    started, moment = number(row.get("started_at")), number(now)
    if row.get("spawn_observed") is not True:
        result["prior_remaining_basis"] = "spawn_observation_missing"
        return
    if started is None or moment is None or moment < started:
        result["prior_remaining_basis"] = "observation_start_not_recorded"
        return
    elapsed = float(moment) - float(started)
    low, high = result["duration_range_seconds"]
    result["prior_observed_seconds"] = round(elapsed, 3)
    result["prior_remaining_interval_seconds"] = [round(max(0.0, low - elapsed), 3),
                                                  round(max(0.0, high - elapsed), 3)]
    result["prior_remaining_basis"] = "backtested_same_fingerprint_interval_minus_observed_elapsed"
    result["live_context_verified"] = False
