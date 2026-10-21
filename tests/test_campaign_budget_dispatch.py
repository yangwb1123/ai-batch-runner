"""Campaign deadline admission, including queued work and real ledger writes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import campaign as campaign_facade
from pbatch import campaign_budget, campaign_cli, campaign_exec, campaign_pipeline
from pbatch.campaign_models import CampaignSettings, Direction
from pbatch.campaign_pipeline import PipelineOutcome
from pbatch.campaign_state import StateStore
from pbatch.pipeline_status import PipelineStatus


DEADLINE = "2030-01-01T00:00:00+00:00"
CUTOFF = 1893456000.0


def _context(tmp_path: Path, **settings) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        settings=CampaignSettings(state_file="state.jsonl", summary_file="SUMMARY.md",
                                  **settings),
        args=SimpleNamespace(dry_run=False, modules="", model="", provider="", timeout=0,
                             agent_bin="pi", memory_mode="off", stream_output="none"),
        store=StateStore(tmp_path / "state.jsonl"),
        snapshot={},
    )


def _items(tmp_path: Path):
    return [(Direction(module="src", title=title), tmp_path / f"{title}.json", title)
            for title in ("first", "second")]


def _clock(monkeypatch, epoch: float):
    clock = SimpleNamespace(epoch=epoch)
    monkeypatch.setattr(campaign_budget, "time", SimpleNamespace(time=lambda: clock.epoch))
    return clock


def _batch_observers(monkeypatch):
    monkeypatch.setattr(campaign_exec, "_direction_event", lambda *a, **k: None)
    monkeypatch.setattr(campaign_exec, "record_campaign_outcomes", lambda *a, **k: None)


def test_budget_exhaustion_writes_real_event_and_summary(monkeypatch, tmp_path):
    """Keep the real _event signature: a *args mock hid the missing argument."""
    context = _context(tmp_path, deadline_at="2000-01-01T00:00:00+00:00")
    monkeypatch.setattr("pbatch.memory.record_campaign", lambda payload, root: None)

    assert campaign_exec._budget_exhausted(context) is True

    row = context.store.latest_event("__campaign__", "__budget__")
    assert row["status"] == "CAMPAIGN_BUDGET_EXHAUSTED"
    assert row["evidence"] == []
    assert "absolute deadline" in row["reason"]
    assert context.settings.summary_path(tmp_path).is_file()


def test_expired_sequential_direction_never_touches_repository(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF)
    settings = CampaignSettings(deadline_at=DEADLINE)
    calls = []
    monkeypatch.setattr(campaign_pipeline, "_git_text", lambda *a: calls.append(a) or "")
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline",
                        lambda *a: tmp_path / "pipeline.yaml")
    monkeypatch.setattr(campaign_pipeline, "_execute_direction_pipeline",
                        lambda *a: (PipelineStatus.PASSED, "stub", []))
    direction, analysis, _ = _items(tmp_path)[0]

    outcome = campaign_pipeline.run_direction(tmp_path, settings, direction, analysis)

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.evidence == [str(analysis)]
    assert calls == [], "expired admission must happen before Git or materialization"


def test_sequential_batch_checks_cutoff_between_directions(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 1)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _batch_observers(monkeypatch)
    monkeypatch.setattr(campaign_pipeline, "_git_text", lambda *a: "")
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", lambda *a, **k: {
        "head_commit": "", "clean_commit": "", "dirty_evidence": [], "dirty_digest": ""})
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline",
                        lambda *a: tmp_path / "pipeline.yaml")
    ran = []

    def execute(path, model, provider, timeout, reuse, direction):
        ran.append(direction.title)
        clock.epoch = CUTOFF
        return PipelineStatus.PASSED, "checks passed", [str(path)]

    monkeypatch.setattr(campaign_pipeline, "_execute_direction_pipeline", execute)
    outcomes, _elapsed = campaign_exec._execute_pipeline_batch(context, _items(tmp_path), 1)

    assert ran == ["first"]
    assert [item.status for item in outcomes] == [
        PipelineStatus.PASSED, PipelineStatus.BUDGET_BLOCKED]
    assert context.round_progress == 1


def test_expired_isolated_batch_creates_no_worktree(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF)
    context = _context(tmp_path, deadline_at=DEADLINE, pipeline_concurrency=2)
    _batch_observers(monkeypatch)
    calls = []
    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree", lambda *a: calls.append(a))

    outcomes, _elapsed = campaign_exec._execute_pipeline_batch(context, _items(tmp_path), 2)

    assert [item.status for item in outcomes] == [PipelineStatus.BUDGET_BLOCKED] * 2
    assert calls == []
    assert context.round_progress == 0


def test_queued_isolated_direction_rechecks_coordinator_budget(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 1)
    settings = CampaignSettings(deadline_at=DEADLINE, state_file="coordinator.jsonl")
    raw = [(direction, analysis) for direction, analysis, _ in _items(tmp_path)]
    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree", lambda root, settings, d: (
        tmp_path / d.title, f"campaign/{d.title}", "baseline"))
    ran = []

    def run(settings, direction, analysis, worktree, branch, baseline, agent_args):
        ran.append(direction.title)
        clock.epoch = CUTOFF
        return PipelineOutcome(direction, PipelineStatus.PASSED, "", 0, [],
                               branch=branch, baseline_commit=baseline)

    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    outcomes = campaign_pipeline.run_directions_isolated(tmp_path, settings, raw, 1, [])
    by_title = {item.direction.title: item for item in outcomes}

    assert ran == ["first"]
    assert by_title["first"].status == PipelineStatus.PASSED
    blocked = by_title["second"]
    assert blocked.status == PipelineStatus.BUDGET_BLOCKED
    assert blocked.branch == "campaign/second"
    assert blocked.baseline_commit == "baseline"
    assert blocked.evidence == [str(raw[1][1])]


def test_gate_anchors_fallback_once_instead_of_sliding(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF)
    settings = CampaignSettings(budget_seconds=10, state_file="missing.jsonl")
    allowed = campaign_budget.gate(settings, tmp_path)

    assert allowed() is True
    clock.epoch += 10
    assert allowed() is False


def test_gate_uses_original_ledger_not_worktree_or_restart(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF)
    settings = CampaignSettings(budget_seconds=10, state_file="coordinator.jsonl")
    settings.state_path(tmp_path).write_text(
        '{"status":"CAMPAIGN_RUN_STARTED","updated_at":"2030-01-01T00:00:00Z"}\n',
        encoding="utf-8")
    allowed = campaign_budget.gate(settings, tmp_path)
    clock.epoch += 10

    assert allowed() is False
    assert campaign_budget.gate(settings, tmp_path)() is False


def test_disabled_gate_preserves_unlimited_admission(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF)
    allowed = campaign_budget.gate(CampaignSettings(), tmp_path)
    clock.epoch += 86400 * 365
    assert allowed() is True


def test_expired_campaign_leaves_round_loop_without_commit_or_delay(monkeypatch, tmp_path):
    context = _context(tmp_path, deadline_at="2000-01-01T00:00:00+00:00")
    context.args.rounds = 0
    context.args.round_commit = True
    context.args.round_delay = 60
    monkeypatch.setattr("pbatch.memory.record_campaign", lambda payload, root: None)
    monkeypatch.setattr(campaign_exec, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(campaign_exec, "discover_modules", lambda *a: ["src"])
    monkeypatch.setattr(campaign_exec, "_campaign_plan", lambda *a: SimpleNamespace(blocked=False))
    monkeypatch.setattr(campaign_cli, "_refresh_repository_snapshot", lambda *a: None)
    actions = []
    monkeypatch.setattr(campaign_cli, "_round_commit", lambda *a: actions.append("commit"))
    monkeypatch.setattr(campaign_cli, "update_round_recovery", lambda *a: (0, ""))

    def unexpected_sleep(_seconds):
        raise AssertionError("terminal budget stop must not sleep or start another round")

    monkeypatch.setattr(campaign_cli, "time", SimpleNamespace(sleep=unexpected_sleep))
    assert campaign_cli.run_campaign_loop(context) == 3
    assert actions == []
    assert context.store.latest_event("__campaign__", "__budget__")["status"] == (
        "CAMPAIGN_BUDGET_EXHAUSTED")


def test_budget_terminal_code_is_not_hidden_by_an_earlier_round_failure(monkeypatch, tmp_path):
    context = _context(tmp_path)
    context.args.rounds = 0
    context.args.round_commit = False
    context.args.round_delay = 0
    monkeypatch.setattr(campaign_cli, "_refresh_repository_snapshot", lambda *a: None)
    monkeypatch.setattr(campaign_cli, "update_round_recovery", lambda *a: (0, ""))
    results = iter([1, 3])

    def round_result(_context):
        return next(results)

    monkeypatch.setattr(campaign_exec, "run_campaign", round_result)
    assert campaign_cli.run_campaign_loop(context) == 3


@pytest.mark.parametrize("rounds", [0, 1])
def test_late_budget_refusal_is_terminal_before_commit_or_delay(monkeypatch, tmp_path, rounds):
    clock = _clock(monkeypatch, CUTOFF - 1)
    context = _context(tmp_path, deadline_at=DEADLINE)
    context.args.rounds = rounds
    context.args.round_commit = True
    context.args.round_delay = 60
    monkeypatch.setattr("pbatch.memory.record_campaign", lambda *a: None)
    monkeypatch.setattr(campaign_exec, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(campaign_exec, "discover_modules", lambda *a: ["src"])
    monkeypatch.setattr(campaign_exec, "_campaign_plan", lambda *a: SimpleNamespace(
        selected=["src"], blocked=False, reason="test", mapping=lambda: {}))
    monkeypatch.setattr(campaign_exec, "_provider_breaker_admission", lambda *a: ("", []))
    monkeypatch.setattr(campaign_exec, "_preflight_validators", lambda *a: 0)
    monkeypatch.setattr(campaign_exec, "_analyze_modules", lambda *a: ({}, 0))
    monkeypatch.setattr(campaign_exec, "_select", lambda *a: ([], 0))
    monkeypatch.setattr(campaign_exec, "_report_working_tree_residue", lambda *a: None)
    monkeypatch.setattr(campaign_exec, "_record_evolution_trajectory", lambda *a: None)
    monkeypatch.setattr(campaign_cli, "_refresh_repository_snapshot", lambda *a: None)
    monkeypatch.setattr(campaign_cli, "update_round_recovery", lambda *a: (0, ""))
    actions = []
    monkeypatch.setattr(campaign_cli, "_round_commit", lambda *a: actions.append("commit"))
    monkeypatch.setattr(campaign_cli, "time", SimpleNamespace(sleep=lambda *a: actions.append("sleep")))

    def implement(*_args):
        clock.epoch = CUTOFF
        return [PipelineOutcome(d, status, "", 0, [str(a)]) for (d, a, _), status in zip(
            _items(tmp_path), (PipelineStatus.PASSED, PipelineStatus.BUDGET_BLOCKED))]

    monkeypatch.setattr(campaign_exec, "_implement", implement)
    assert campaign_cli.run_campaign_loop(context) == 3
    assert actions == []


@pytest.mark.parametrize("rounds,codes", [(0, [3]), (1, [3]), (0, [1, 3]), (2, [1, 3])])
def test_compatibility_facade_treats_budget_stop_as_terminal(monkeypatch, tmp_path, rounds, codes):
    context = _context(tmp_path)
    context.args.rounds = rounds
    context.args.round_commit = True
    context.args.round_delay = 60
    results = iter(codes)
    monkeypatch.setattr(campaign_facade, "run_campaign", lambda *a: next(results))
    monkeypatch.setattr(campaign_facade, "_refresh_repository_snapshot", lambda *a: None)
    monkeypatch.setattr(campaign_facade, "update_round_recovery", lambda *a: (0, ""))
    actions = []
    monkeypatch.setattr(campaign_facade, "_round_commit", lambda *a: actions.append("commit") or True)
    monkeypatch.setattr(campaign_facade, "time", SimpleNamespace(
        sleep=lambda *a: actions.append("sleep")))

    assert campaign_facade.run_campaign_loop(context) == 3
    assert actions == ["commit", "sleep"] * (len(codes) - 1)
