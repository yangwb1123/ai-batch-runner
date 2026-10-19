"""Historical duration priors are source/profile scoped, backtested, never live ETAs."""
from __future__ import annotations

from pbatch.models import Task
from pbatch.task_duration import build_priors, forecast_for, work_fingerprint


def row(index, duration=10, status="succeeded", digest="a" * 64, stage="implement", spawned=True):
    return {"kind": "agent", "phase": "execution", "stage": stage, "work_fingerprint": digest,
            "spawn_observed": spawned, "status": status, "elapsed": duration, "ts": 100 + index}


def candidates(rows, source="src-one", partial=False):
    return [(record["ts"], str(index), {"source_id": source}, [record], partial)
            for index, record in enumerate(rows)]


def active():
    return row(20, None, "running")


def test_sparse_or_untracked_profiles_stay_unknown():
    priors = build_priors(candidates([row(index) for index in range(7)]))
    result = forecast_for("src-one", active(), priors)
    assert result["duration_range_seconds"] is None and result["sample_count"] == 7
    assert result["reason"] == "insufficient_completed_samples"
    assert forecast_for("src-one", row(0, digest=""), {})["reason"] == "execution_profile_untracked"


def test_backtested_range_has_sample_counts_but_no_live_eta_or_control_grant():
    priors = build_priors(candidates([row(index) for index in range(8)]))
    result = forecast_for("src-one", active(), priors)
    assert result["duration_range_seconds"] == [10, 10]
    assert result["status"] == "historical_prior_only"
    assert result["calibration"]["test_samples"] == 3
    assert result["calibration"]["observed_coverage"] == 1
    assert result["calibration"]["statistical_guarantee"] is False
    assert result["remaining_seconds"] is None and result["eta"] is None
    assert result["grants_execution"] is False and result["quality_ranking"] == "not_performed"


def test_chronological_holdout_withholds_a_prior_after_duration_distribution_changes():
    priors = build_priors(candidates([row(index, 10 if index < 5 else 1000) for index in range(8)]))
    result = forecast_for("src-one", active(), priors)
    assert result["duration_range_seconds"] is None
    assert result["reason"] == "historical_interval_failed_backtest"
    assert result["calibration"]["observed_coverage"] < 0.8


def test_partial_corrupt_source_or_failed_samples_do_not_create_optimistic_forecasts():
    samples = [row(index) for index in range(8)]
    result = forecast_for("src-one", active(), build_priors(candidates(samples, partial=True)))
    assert result["reason"] == "partial_observation_window" and result["duration_range_seconds"] is None
    priors = build_priors(candidates([*samples, row(9, 300, "failed")]))
    result = forecast_for("src-one", active(), priors)
    assert result["reason"] == "failed_or_unmeasured_attempts_prevent_success_only_bias"
    assert result["excluded_terminal_observations"] == 1


def test_projects_stages_profiles_and_nonspawned_attempts_do_not_share_samples():
    groups = candidates([row(index) for index in range(8)], "other-project")
    groups += candidates([row(index, stage="plan") for index in range(8)])
    groups += candidates([row(index, digest="b" * 64) for index in range(8)])
    groups += candidates([row(index, spawned=False) for index in range(8)])
    result = forecast_for("src-one", active(), build_priors(groups))
    assert result["sample_count"] == 0 and result["duration_range_seconds"] is None


def test_closed_attempts_do_not_get_retrospective_predictions_trained_on_themselves():
    priors = build_priors(candidates([row(index) for index in range(8)]))
    result = forecast_for("src-one", row(9), priors)
    assert result["reason"] == "attempt_already_reported_terminal"
    assert result["duration_range_seconds"] is None


def test_instruction_profile_has_no_prompt_truncation_and_changes_with_configuration():
    a = Task(prompt="prefix" + "x" * 2000, cwd="/tmp/project", agent="fake", model="model-a")
    b = Task(prompt=a.prompt + " constraint at the end", cwd=a.cwd, agent=a.agent, model=a.model)
    assert work_fingerprint(a) != work_fingerprint(b)
    b.prompt = a.prompt
    assert work_fingerprint(a) == work_fingerprint(b)
    b.model = "model-b"
    assert work_fingerprint(a) != work_fingerprint(b)
    b.model = a.model
    b.memory["role"] = "independent-reviewer"
    assert work_fingerprint(a) != work_fingerprint(b)


def started_row(index=20, started_at=100.0, digest="a" * 64, status="running", spawned=True):
    value = row(index, None, status, digest, spawned=spawned)
    value["started_at"] = started_at
    return value


def test_prior_interval_is_reported_only_with_recorded_start_and_spawn_evidence():
    priors = build_priors(candidates([row(index) for index in range(8)]))
    result = forecast_for("src-one", started_row(), priors, now=104.0)
    assert result["remaining_seconds"] is None and result["eta"] is None
    assert result["not_a_live_eta"] is True and result["live_context_verified"] is False
    assert result["prior_observed_seconds"] == 4.0
    assert result["prior_remaining_interval_seconds"] == [6.0, 6.0]
    assert result["prior_remaining_basis"] == \
        "backtested_same_fingerprint_interval_minus_observed_elapsed"


def test_prior_interval_clamps_to_zero_and_requires_spawn_evidence():
    priors = build_priors(candidates([row(index) for index in range(8)]))
    late = forecast_for("src-one", started_row(started_at=100.0), priors, now=130.0)
    assert late["prior_remaining_interval_seconds"] == [0.0, 0.0]
    unspawned = forecast_for("src-one", started_row(spawned=False), priors, now=104.0)
    assert unspawned["prior_remaining_interval_seconds"] is None
    assert unspawned["prior_remaining_basis"] == "spawn_observation_missing"


def test_prior_interval_requires_a_recorded_start():
    priors = build_priors(candidates([row(index) for index in range(8)]))
    result = forecast_for("src-one", active(), priors, now=104.0)
    assert result["prior_remaining_interval_seconds"] is None
    assert result["prior_remaining_basis"] == "observation_start_not_recorded"


def test_no_prior_interval_without_a_backtested_range():
    priors = build_priors(candidates([row(index) for index in range(7)]))
    result = forecast_for("src-one", started_row(), priors, now=104.0)
    assert result["prior_remaining_interval_seconds"] is None
    assert result["prior_remaining_basis"] is None
