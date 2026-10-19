"""Verified execution mapping: exact identities, explicit unknown, no inference."""
import pytest

from pbatch.task_center import snapshot
from pbatch.task_execution_map import (METHOD, execution_mapping)


def _row(**overrides):
    row = {"kind": "agent", "phase": "execution", "record_ref": "tc-1",
           "run_id": "run-1", "local_task_id": "task-1", "attempt": 1,
           "stage": "implement", "role": "", "status": "succeeded",
           "started_at": 100.0, "ts": 130.0, "spawn_observed": True,
           "legacy_identity": False, "title": "same title"}
    row.update(overrides)
    return row


def test_verified_pair_carries_the_exact_identity():
    result = execution_mapping([_row()])
    assert result["method"] == METHOD
    assert result["verified_spawn_and_outcome"] == 1
    assert result["spawn_only_active_observations"] == 0
    assert result["outcome_without_spawn_observation"] == 0
    ref = result["verified_refs"][0]
    assert (ref["run_id"], ref["task_id"], ref["attempt"], ref["stage"]) == \
        ("run-1", "task-1", 1, "implement")
    assert ref["spawn_observed"] is True and ref["status"] == "succeeded"


def test_outcome_without_spawn_observation_is_counted_not_zero():
    rows = [_row(record_ref="tc-a", run_id="run-a", spawn_observed=None),
            _row(record_ref="tc-b", run_id="run-b", spawn_observed=False)]
    result = execution_mapping(rows)
    assert result["verified_spawn_and_outcome"] == 0
    assert result["outcome_without_spawn_observation"] == 2
    assert len(result["unverified_outcome_refs"]) == 2
    assert result["unknown_not_zero"] is False


def test_spawn_only_is_an_observation_not_liveness_or_outcome():
    result = execution_mapping([_row(status="running"), _row(status="queued", record_ref="tc-2")])
    assert result["spawn_only_active_observations"] == 2
    assert result["verified_spawn_and_outcome"] == 0
    assert result["liveness"] == "not_verified"


def test_missing_spawn_flag_is_treated_as_unverified():
    result = execution_mapping([_row(spawn_observed=None)])
    assert result["outcome_without_spawn_observation"] == 1
    assert result["verified_spawn_and_outcome"] == 0


def test_legacy_identity_observations_stay_separate():
    result = execution_mapping([_row(legacy_identity=True)])
    assert result["legacy_identity_observations"] == 1
    assert result["verified_spawn_and_outcome"] == 0
    assert result["outcome_without_spawn_observation"] == 0


def test_validation_and_non_agent_rows_are_not_executions():
    rows = [_row(), {"kind": "agent", "phase": "validation", "status": "succeeded"},
            {"kind": "pipeline", "phase": "pipeline", "status": "RUNNING"}]
    result = execution_mapping(rows)
    assert result["execution_observations"] == 1
    assert result["other_phase_observations"] == 2


def test_partial_coverage_marks_counts_as_lower_bounds():
    result = execution_mapping([_row()], partial=True)
    assert result["coverage"] == "partial_window"
    assert result["unknown_not_zero"] is True


def test_empty_window_is_zero_and_not_unknown_when_complete():
    result = execution_mapping([])
    assert result["execution_observations"] == 0
    assert result["coverage"] == "observed_rows_only"
    assert result["unknown_not_zero"] is False


def test_bounded_refs_report_omissions():
    rows = [_row(record_ref=f"tc-{index}", run_id=f"run-{index}") for index in range(5)]
    result = execution_mapping(rows, limit=2)
    assert result["verified_spawn_and_outcome"] == 5
    assert len(result["verified_refs"]) == 2
    assert result["omitted_verified_refs"] == 3


def test_identity_not_inferred_from_title_pid_or_timestamp():
    base = _row()
    changed = dict(base, title="other title", ts=999.0, started_at=1.0)
    one = execution_mapping([base])
    two = execution_mapping([changed])
    assert one["verified_refs"][0]["run_id"] == two["verified_refs"][0]["run_id"] == "run-1"
    assert one["verified_refs"][0]["ended_at"] == 130.0
    assert two["verified_refs"][0]["ended_at"] == 999.0


def test_malformed_rows_are_counted_not_classified_or_silently_dropped():
    result = execution_mapping([_row(), "not-a-row", None])
    assert result["execution_observations"] == 1
    assert result["malformed_observations"] == 2
    assert result["verified_spawn_and_outcome"] == 1
    with pytest.raises(ValueError, match="list of observation rows"):
        execution_mapping({"kind": "agent"})


def test_invalid_limit_refuses_before_classification():
    with pytest.raises(ValueError, match="positive integer"):
        execution_mapping([_row()], limit=0)
    with pytest.raises(ValueError, match="positive integer"):
        execution_mapping([_row()], limit=True)


def test_task_center_snapshot_exposes_the_mapping(tmp_path, monkeypatch):
    from pbatch import task_center

    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path))
    monkeypatch.setattr(task_center, "discover_sources", lambda: ([], ["no sources"]))
    payload = snapshot(limit=1)
    mapping = payload["execution_mapping"]
    assert mapping["method"] == METHOD
    assert mapping["execution_observations"] == 0
    assert mapping["coverage"] == "observed_rows_only"


def test_task_center_snapshot_marks_unavailable_discovery_as_partial(tmp_path, monkeypatch):
    from pbatch import task_center

    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path))

    def broken():
        raise OSError("registry unavailable")

    monkeypatch.setattr(task_center, "discover_sources", broken)
    payload = snapshot(limit=1)
    mapping = payload["execution_mapping"]
    assert mapping["coverage"] == "partial_window"
    assert mapping["unknown_not_zero"] is True
