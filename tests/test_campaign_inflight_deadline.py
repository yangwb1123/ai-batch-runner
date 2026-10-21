"""Offline campaign-cutoff regressions: real owned children, no model calls."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from pbatch import campaign_pipeline, campaign_process
from pbatch.campaign_budget import CampaignDeadlineExpired, parse_deadline
from pbatch.campaign_models import CampaignSettings, Direction


def _run(tmp_path, monkeypatch, settings, source):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    return campaign_process.run_isolated_child(
        [sys.executable, "-c", source], settings, Direction(module="src", title="cutoff"),
        tmp_path, pipeline, tmp_path / "campaign.log")


def test_already_expired_cutoff_never_spawns_child(tmp_path, monkeypatch):
    marker = tmp_path / "spawned"
    settings = CampaignSettings(deadline_at=str(time.time() - 1), pipeline_timeout=3)
    with pytest.raises(RuntimeError, match="campaign deadline"):
        _run(tmp_path, monkeypatch, settings,
             f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')")
    assert not marker.exists()


def test_running_child_cannot_extend_cutoff_with_progress(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    progress = artifacts / "progress.md"
    source = (
        "import pathlib, time\n"
        f"pathlib.Path({str(marker)!r}).write_text('started')\n"
        "while True:\n"
        f"    pathlib.Path({str(progress)!r}).write_text(str(time.time()))\n"
        "    time.sleep(0.02)\n"
    )
    settings = CampaignSettings(deadline_at=str(time.time() + 1), pipeline_timeout=3,
                                pipeline_no_progress_timeout=2)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="campaign deadline") as caught:
        _run(tmp_path, monkeypatch, settings, source)
    assert time.monotonic() - started < 2.8
    assert marker.read_text() == "started"
    before = progress.read_bytes()
    time.sleep(0.1)
    assert progress.read_bytes() == before, "owned child still writes after cutoff cleanup"
    assert any(Path(path).is_file() and path.endswith(".json")
               for path in getattr(caught.value, "evidence", ())), "missing deadline receipt"


@pytest.mark.parametrize("deadline", ["bad-deadline", "nan", "inf", "-inf"])
def test_invalid_cutoff_fails_before_spawn(tmp_path, monkeypatch, deadline):
    marker = tmp_path / "spawned"
    settings = CampaignSettings(deadline_at=deadline, pipeline_timeout=3)
    with pytest.raises(ValueError, match="deadline"):
        _run(tmp_path, monkeypatch, settings,
             f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')")
    assert not marker.exists()


def test_unfrozen_relative_budget_is_not_restarted_in_worktree(tmp_path, monkeypatch):
    settings = CampaignSettings(budget_seconds=10, pipeline_timeout=3)
    with pytest.raises(ValueError, match="coordinator"):
        _run(tmp_path, monkeypatch, settings, "pass")


def test_no_campaign_budget_keeps_child_result_tuple(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, CampaignSettings(pipeline_timeout=3), "pass") == (
        0, False, False, 0.0)


def test_pipeline_timeout_remains_distinct_from_campaign_cutoff(tmp_path, monkeypatch):
    settings = CampaignSettings(deadline_at=str(time.time() + 60), pipeline_timeout=1)
    result = _run(tmp_path, monkeypatch, settings, "import time; time.sleep(10)")
    assert result == (-1, True, False, 0.0)


def test_coordinator_ledger_cutoff_is_carried_without_mutating_settings(tmp_path, monkeypatch):
    origin = time.time()
    state = tmp_path / "coordinator.jsonl"
    state.write_text(json.dumps({"status": "CAMPAIGN_RUN_STARTED", "updated_at": str(origin)})
                     + "\n", encoding="utf-8")
    settings = CampaignSettings(budget_seconds=60, state_file="coordinator.jsonl")
    direction = Direction(module="src", title="carry cutoff")
    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree", lambda *a: (
        tmp_path / "worktree", "branch", "baseline"))
    received = []

    def run(received_settings, d, analysis, worktree, branch, baseline, args):
        received.append(received_settings)
        return campaign_pipeline.PipelineOutcome(d, "PASSED", "", 0, [])

    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    campaign_pipeline.run_directions_isolated(
        tmp_path, settings, [(direction, tmp_path / "analysis.json")], 1, [])
    assert parse_deadline(received[0].deadline_at) == origin + 60
    assert settings.deadline_at == ""
    assert received[0].budget_seconds == 60


def test_inflight_budget_failure_preserves_worktree_and_stops_finalization(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    direction = Direction(module="src", title="preserve partial implementation")
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    repository = {"head_commit": "base", "clean_commit": "", "dirty_evidence": [" M code.py"],
                  "dirty_digest": "dirty"}
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", lambda *a, **k: repository)
    monkeypatch.setattr(campaign_pipeline, "_copy_local_analysis", lambda *a: tmp_path / "analysis.json")
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline", lambda *a: pipeline)
    source = ("import pathlib,time\n"
              f"pathlib.Path({str(marker)!r}).write_text('started')\n"
              "time.sleep(10)\n")
    monkeypatch.setattr(campaign_pipeline, "isolated_command", lambda *a: [sys.executable, "-c", source])
    actions = []
    monkeypatch.setattr(campaign_pipeline, "_finalize_worktree", lambda *a: actions.append("finalize"))
    monkeypatch.setattr(campaign_pipeline, "_recover_isolated_worktree", lambda *a: actions.append("restore"))
    settings = CampaignSettings(deadline_at=str(time.time() + 1), pipeline_timeout=3,
                                recovery_enabled=True, recovery_restore_on_failure=True)
    outcome = campaign_pipeline._safe_worktree_run(
        settings, direction, tmp_path / "analysis.json", tmp_path, "branch", "base", [])
    assert outcome.status == "BUDGET_BLOCKED"
    assert "campaign deadline" in outcome.reason
    assert outcome.recovery_status == "PRESERVED"
    assert outcome.dirty_evidence == [" M code.py"]
    assert outcome.elapsed > 0
    assert actions == []
    assert marker.exists()


def _controlled_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    state = {"head_commit": "base", "clean_commit": "", "dirty_evidence": [" M code.py"],
             "dirty_digest": "dirty"}
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", lambda *a, **k: state)
    monkeypatch.setattr(campaign_pipeline, "_copy_local_analysis", lambda *a: tmp_path / "a.json")
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline", lambda *a: pipeline)
    actions = []
    monkeypatch.setattr(campaign_pipeline, "_finalize_worktree", lambda *a: actions.append("finalize"))
    monkeypatch.setattr(campaign_pipeline, "_recover_isolated_worktree", lambda *a: actions.append("restore"))
    return Direction(module="src", title="controlled flow"), actions


def test_cutoff_crossed_before_parent_finalize_blocks_commit(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pbatch import campaign_budget

    direction, actions = _controlled_flow(tmp_path, monkeypatch)
    clock = SimpleNamespace(wall=100.0)
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))

    def completed(*_args):
        clock.wall = 102.0
        return 0, False, False, 0.0

    monkeypatch.setattr(campaign_pipeline, "_run_isolated_child", completed)
    monkeypatch.setattr(campaign_pipeline, "isolated_command", lambda *a: ["unused"])
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(deadline_at="102"), direction, tmp_path / "a.json", tmp_path,
        "branch", "base", [])
    assert outcome.status == "BUDGET_BLOCKED"
    assert outcome.recovery_status == "PRESERVED"
    assert actions == []


def test_git_evidence_delay_cannot_start_finalize_after_cutoff(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pbatch import campaign_budget

    direction, actions = _controlled_flow(tmp_path, monkeypatch)
    clock = SimpleNamespace(wall=100.0, probes=0)
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))
    state = {"head_commit": "base", "clean_commit": "base", "dirty_evidence": [],
             "dirty_digest": ""}

    def evidence(*_args, **_kwargs):
        clock.probes += 1
        if clock.probes == 2:  # post-child Git work crosses the boundary
            clock.wall = 102.0
        return state

    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", evidence)
    monkeypatch.setattr(campaign_pipeline, "_run_isolated_child", lambda *a: (0, False, False, 0.0))
    monkeypatch.setattr(campaign_pipeline, "isolated_command", lambda *a: ["unused"])
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(deadline_at="102"), direction, tmp_path / "a.json", tmp_path,
        "branch", "base", [])
    assert actions == [], "finalize/recovery started after the Git probe exhausted budget"
    assert outcome.status == "BUDGET_BLOCKED"
    assert outcome.recovery_status == "PRESERVED"


@pytest.mark.parametrize("late_phase", ["finalize", "artifacts", "recovery"])
@pytest.mark.parametrize("raises", [False, True])
def test_late_parent_io_neither_recovers_nor_publishes_pass(monkeypatch, tmp_path, late_phase, raises):
    from types import SimpleNamespace
    from pbatch import campaign_budget
    from pbatch.campaign_recovery import RecoveryResult

    direction, _ = _controlled_flow(tmp_path, monkeypatch)
    clock, calls = SimpleNamespace(wall=100.0), []
    state = {"head_commit": "base", "clean_commit": "base", "dirty_evidence": [], "dirty_digest": ""}
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))

    def touch(phase):
        calls.append(phase)
        if phase == late_phase:
            clock.wall = 102.0
            if raises:
                raise OSError("fixture late parent I/O failure")

    def finalize(*_args):
        touch("finalize")
        return "", state

    def artifacts(*_args):
        touch("artifacts")
        return []

    def recovery(*_args):
        touch("recovery")
        return RecoveryResult(""), state, ""

    monkeypatch.setattr(campaign_pipeline, "_run_isolated_child", lambda *a: (0, False, False, 0.0))
    monkeypatch.setattr(campaign_pipeline, "isolated_command", lambda *a: ["unused"])
    monkeypatch.setattr(campaign_pipeline, "_finalize_worktree", finalize)
    monkeypatch.setattr(campaign_pipeline, "isolated_run_evidence", artifacts)
    monkeypatch.setattr(campaign_pipeline, "_recover_isolated_worktree", recovery)
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(deadline_at="102"), direction, tmp_path / "a.json", tmp_path,
        "branch", "base", [])
    assert outcome.status == "BUDGET_BLOCKED"
    assert outcome.recovery_status == "PRESERVED"
    expected = ["finalize", "artifacts", "recovery"]
    assert calls == expected[:expected.index(late_phase) + 1]


@pytest.mark.parametrize("failure", [CampaignDeadlineExpired("fixture budget", []),
                                    campaign_process.CampaignGuardFailed("fixture guard")])
def test_preserved_stop_survives_failed_git_diagnostic(monkeypatch, tmp_path, failure):
    direction = Direction(module="src", title="diagnostic")

    def stopped(*_args):
        raise failure

    def unavailable(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fixture Git output")

    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", stopped)
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", unavailable)
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(), direction, tmp_path / "a.json", tmp_path, "branch", "base", [])
    assert outcome.status == ("BUDGET_BLOCKED" if isinstance(failure, CampaignDeadlineExpired)
                              else "PIPELINE_FAILED")
    assert outcome.recovery_status == "PRESERVED"
    assert outcome.clean_commit == ""
    assert "Git evidence unavailable" in outcome.reason
    assert "unavailable" in " ".join(outcome.dirty_evidence)


@pytest.mark.parametrize("diagnostic_raises", [False, True])
def test_failure_diagnostics_crossing_cutoff_keep_terminal_budget(monkeypatch, tmp_path, diagnostic_raises):
    from types import SimpleNamespace
    from pbatch import campaign_budget

    clock = SimpleNamespace(wall=100.0)
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))

    def failed(*_args):
        raise RuntimeError("fixture pre-cutoff failure")

    def probe(*_args):
        clock.wall = 102.0
        if diagnostic_raises:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fixture delayed Git output")
        return {"head_commit": "base", "clean_commit": "base", "dirty_evidence": [], "dirty_digest": ""}

    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", failed)
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", probe)
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(deadline_at="102"), Direction("src", "late diagnostic"),
        tmp_path / "a.json", tmp_path, "branch", "base", [])
    assert outcome.status == "BUDGET_BLOCKED"
    assert outcome.recovery_status == "PRESERVED"
    assert "fixture pre-cutoff failure" in outcome.reason
    if diagnostic_raises:
        assert "Git evidence unavailable" in outcome.reason


def test_recovery_limit_early_return_rechecks_publication_deadline(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pbatch import campaign_budget

    direction, actions = _controlled_flow(tmp_path, monkeypatch)
    clock = SimpleNamespace(wall=100.0)
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.wall))
    monkeypatch.setattr(campaign_pipeline, "has_failed_attempt", lambda *a: True)

    def late_history(*_args):
        clock.wall = 102.0
        return 2

    monkeypatch.setattr(campaign_pipeline, "recovery_attempt_count", late_history)
    monkeypatch.setattr(campaign_pipeline, "_run_isolated_child", lambda *a: pytest.fail("early refusal spawned"))
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(deadline_at="102", recovery_enabled=True, recovery_max_attempts=1),
        direction, tmp_path / "a.json", tmp_path, "branch", "base", [])
    assert outcome.status == "BUDGET_BLOCKED"
    assert outcome.recovery_status == "PRESERVED"
    assert outcome.elapsed > 0
    assert "self-repair attempt budget exhausted" in outcome.reason
    assert actions == []


def test_required_monitor_start_failure_is_not_an_automatic_retry(monkeypatch, tmp_path):
    from pbatch import campaign_execution_guard

    direction, actions = _controlled_flow(tmp_path, monkeypatch)
    monkeypatch.setattr(campaign_pipeline, "isolated_command", lambda *a: [
        sys.executable, "-c", "import time; time.sleep(30)"])

    def cannot_start(_thread):
        raise RuntimeError("fixture thread startup refusal")

    monkeypatch.setattr(campaign_execution_guard.threading.Thread, "start", cannot_start)
    outcome = campaign_pipeline._safe_worktree_run(
        CampaignSettings(deadline_at=str(time.time() + 60)), direction,
        tmp_path / "a.json", tmp_path, "branch", "base", [])
    assert outcome.status == "PIPELINE_FAILED"
    assert "monitor could not start" in outcome.reason
    assert outcome.recovery_status == "PRESERVED"
    assert not campaign_process._ACTIVE_PIPELINES
    assert actions == []
