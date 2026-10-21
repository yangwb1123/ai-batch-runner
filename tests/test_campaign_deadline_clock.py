"""Deterministic cutoff ordering, fixed monotonic cap, and setup accounting."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import campaign_budget, campaign_watchdog
from pbatch.campaign_budget import execution_deadline
from pbatch.campaign_models import CampaignSettings


class _Child:
    def __init__(self, code=None):
        self.returncode = code
        self.polls = 0
        self.stops = 0

    def poll(self):
        self.polls += 1
        return self.returncode

    def stop(self):
        self.stops += 1
        self.returncode = -9


def _clock(monkeypatch, motion="normal"):
    clock = SimpleNamespace(mono=10.0, wall=100.0, sleeps=[])

    def sleep(delay):
        clock.sleeps.append(delay)
        clock.mono += delay
        clock.wall += delay if motion == "normal" else -100 if motion == "rollback" else 10

    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))
    monkeypatch.setattr(campaign_watchdog, "time", SimpleNamespace(
        monotonic=lambda: clock.mono, sleep=sleep))
    return clock


def _wait(tmp_path, child, *, deadline, started=10.0, timeout=60, progress_timeout=0):
    return campaign_watchdog.wait_for_pipeline(
        child, pipeline=tmp_path / "pipeline.yaml", logfile=tmp_path / "log",
        lease_path=tmp_path / "lease.json", lease={}, timeout=timeout,
        no_progress_timeout=progress_timeout, deadline=deadline, started_at=started, stop=child.stop)


@pytest.mark.parametrize("motion,expected_mono", [("normal", 12.0), ("rollback", 12.0),
                                                 ("forward", 11.0)])
def test_running_attempt_deadline_never_slides(monkeypatch, tmp_path, motion, expected_mono):
    clock = _clock(monkeypatch, motion)
    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0)
    child = _Child()
    result = _wait(tmp_path, child, deadline=deadline)
    assert result.budget_exhausted is True
    assert result.timed_out is False
    assert child.stops == 1
    assert clock.mono == expected_mono
    assert sum(clock.sleeps) <= 2


def test_late_setup_is_charged_before_poll_or_artifact_read(monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0)
    clock.mono, clock.wall = 13.0, 103.0
    monkeypatch.setattr(campaign_watchdog, "_artifact_marker",
                        lambda *a: pytest.fail("expired attempt scanned artifacts"))
    child = _Child()
    result = _wait(tmp_path, child, deadline=deadline)
    assert result.budget_exhausted
    assert child.polls == 0
    assert child.stops == 1
    assert clock.sleeps == []


def test_preparation_time_also_consumes_pipeline_timeout(monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    clock.mono = 13.0
    child = _Child()
    result = _wait(tmp_path, child, deadline=None, timeout=2)
    assert result.timed_out is True
    assert result.budget_exhausted is False
    assert clock.sleeps == []


def test_budget_crossed_during_scan_stops_before_lease_write(monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0)

    def scan(_path):
        clock.mono, clock.wall = 30.0, 120.0
        return ()

    monkeypatch.setattr(campaign_watchdog, "_artifact_marker", scan)
    monkeypatch.setattr(campaign_watchdog, "_touch_lease",
                        lambda *a, **k: pytest.fail("expired attempt wrote a heartbeat"))
    child = _Child()
    assert _wait(tmp_path, child, deadline=deadline).budget_exhausted
    assert child.stops == 1


def test_timely_normal_exit_is_not_relabelled_as_budget(monkeypatch, tmp_path):
    _clock(monkeypatch)
    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0)
    child = _Child(0)
    result = _wait(tmp_path, child, deadline=deadline)
    assert result.returncode == 0
    assert not result.budget_exhausted
    assert child.stops == 0


def test_expired_observation_does_not_publish_old_completion_as_pass(monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0)
    clock.mono, clock.wall = 12.0, 102.0
    child = _Child(0)
    result = _wait(tmp_path, child, deadline=deadline)
    assert result.budget_exhausted
    assert child.polls == 0


def test_observed_wall_expiry_stays_latched_after_rollback(monkeypatch):
    clock = _clock(monkeypatch)
    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0)
    clock.wall = 103.0
    assert deadline.expired(10.5)
    clock.wall = 90.0
    assert deadline.expired(10.5)
    assert deadline.remaining(10.5) == 0


def test_runtime_clock_does_not_mutate_original_serialized_settings(monkeypatch, tmp_path):
    import json
    from pbatch.campaign_budget import with_frozen_deadline

    clock = _clock(monkeypatch)
    monkeypatch.setattr(campaign_budget, "monotonic", lambda: clock.mono)
    original = CampaignSettings(deadline_at="102")
    snapshot = json.dumps(original.__dict__, sort_keys=True)
    frozen = with_frozen_deadline(original, tmp_path)
    assert "_execution_deadline" not in original.__dict__
    assert json.dumps(original.__dict__, sort_keys=True) == snapshot
    assert with_frozen_deadline(frozen, tmp_path) is frozen
    assert execution_deadline(frozen, 999) is frozen._execution_deadline
    assert frozen._execution_deadline.monotonic == 12.0


def test_parent_retains_original_batch_cap_during_wall_rollback(monkeypatch, tmp_path):
    from pbatch import campaign_deadline
    from pbatch.campaign_budget import CampaignDeadlineExpired, with_frozen_deadline

    clock = _clock(monkeypatch)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(campaign_budget, "monotonic", lambda: clock.mono, raising=False)
    monkeypatch.setattr(campaign_deadline, "time", SimpleNamespace(monotonic=lambda: clock.mono))
    settings = with_frozen_deadline(CampaignSettings(deadline_at="102"), tmp_path)
    clock.mono, clock.wall = 13.0, 90.0
    from pbatch.campaign_models import Direction
    with pytest.raises(CampaignDeadlineExpired):
        campaign_deadline.check_deadline(settings, Direction("src", "parent"), tmp_path,
                                         tmp_path / "p.yaml", tmp_path / "log", phase="publication")


def test_queued_batch_does_not_reauthorize_after_wall_rollback(monkeypatch, tmp_path):
    import time
    from pbatch import campaign_pipeline
    from pbatch.campaign_budget import CampaignDeadlineExpired
    from pbatch.campaign_models import Direction
    from pbatch.campaign_pipeline import PipelineOutcome

    clock, calls = SimpleNamespace(mono=time.monotonic(), wall=100.0), []
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))
    monkeypatch.setattr(campaign_budget, "monotonic", lambda: clock.mono, raising=False)
    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree",
                        lambda r, s, d: (tmp_path, "branch", "base"))
    state = {"head_commit": "base", "clean_commit": "base", "dirty_evidence": [], "dirty_digest": ""}
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", lambda *a: state)

    def run(settings, direction, *_args):
        calls.append(direction.title)
        if direction.title == "first":
            deadline = execution_deadline(settings, clock.mono)
            clock.mono += 3
            clock.wall = 90.0
            assert deadline.expired(clock.mono)
            raise CampaignDeadlineExpired("fixture original monotonic cap expired", [])
        return PipelineOutcome(direction, "PASSED", "", 0, [])

    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    items = [(Direction("src", title), tmp_path / "a.json") for title in ("first", "queued")]
    outcomes = campaign_pipeline.run_directions_isolated(
        tmp_path, CampaignSettings(deadline_at="102"), items, 1, [])
    assert calls == ["first"], "rollback admitted a fresh queued budget after refusal"
    assert {item.status for item in outcomes} == {"BUDGET_BLOCKED"}


def test_preparation_error_after_cutoff_is_terminal_budget(monkeypatch, tmp_path):
    import time
    from pbatch import campaign_pipeline
    from pbatch.campaign_models import Direction

    clock = SimpleNamespace(mono=time.monotonic(), wall=100.0)
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))
    monkeypatch.setattr(campaign_budget, "monotonic", lambda: clock.mono)

    def late_failure(*_args):
        clock.wall = 102.0
        raise OSError("fixture preparation I/O failed after cutoff")

    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree", late_failure)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", lambda *a: pytest.fail("expired prep started child"))
    outcomes = campaign_pipeline.run_directions_isolated(
        tmp_path, CampaignSettings(deadline_at="102"), [(Direction("src", "prep"), tmp_path / "a.json")], 1, [])
    assert outcomes[0].status == "BUDGET_BLOCKED"
    assert outcomes[0].recovery_status == "PRESERVED"
    assert outcomes[0].elapsed > 0
    assert outcomes[0].commit == "" and outcomes[0].baseline_commit == ""


def test_expired_pipeline_timeout_cannot_accept_completed_child(monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    clock.mono = 13.0
    child = _Child(0)
    result = _wait(tmp_path, child, deadline=None, timeout=2)
    assert result.timed_out
    assert result.returncode == -1
    assert child.polls == 0


@pytest.mark.parametrize("budgeted", [False, True])
def test_completion_observation_rechecks_time_after_poll(monkeypatch, tmp_path, budgeted):
    clock = _clock(monkeypatch)

    class LateCompletion(_Child):
        def poll(self):
            self.polls += 1
            clock.mono, clock.wall = 13.0, 103.0
            return 0

    deadline = execution_deadline(CampaignSettings(deadline_at="102"), 10.0) if budgeted else None
    result = _wait(tmp_path, LateCompletion(), deadline=deadline, timeout=2)
    assert result.budget_exhausted if budgeted else result.timed_out
    assert result.returncode == -1


@pytest.mark.parametrize("late_phase", ["setup", "first_scan"])
def test_first_artifact_observation_is_not_unobserved_stall(monkeypatch, tmp_path, late_phase):
    clock = _clock(monkeypatch)
    scans = []
    if late_phase == "setup":
        clock.mono = 13.0

    def scan(_path):
        if not scans:
            clock.mono = 13.0
        scans.append(clock.mono)
        return (("fresh-fixture", len(scans), 1),)

    class Producing(_Child):
        def poll(self):
            self.polls += 1
            return 0 if len(scans) >= 2 else None

    monkeypatch.setattr(campaign_watchdog, "_artifact_marker", scan)
    child = Producing()
    result = _wait(tmp_path, child, deadline=None, progress_timeout=1)
    assert result.returncode == 0 and not result.stalled
    assert child.stops == 0
    assert len(scans) == 2


def test_short_remaining_budget_bounds_the_next_sleep(monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    deadline = execution_deadline(CampaignSettings(deadline_at="100.125"), 10.0)
    child = _Child()
    assert _wait(tmp_path, child, deadline=deadline).budget_exhausted
    assert clock.sleeps == [0.125]
