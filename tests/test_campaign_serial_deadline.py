"""Serial in-tree campaign cutoff: owned guard, cancel plumbing, mapping."""

from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pbatch import (campaign_budget, campaign_execution_guard, campaign_pipeline,
                    campaign_deadline, campaign_isolated_outcome, campaign_serial,
                    pipeline, pipeline_native, runner)
from pbatch.campaign_models import CampaignSettings, Direction
from pbatch.models import Task, TaskResult
from pbatch.pipeline_status import PipelineStatus


DEADLINE = "2030-01-01T00:00:00+00:00"
CUTOFF = 1893456000.0


def _clock(monkeypatch, epoch, mono=10_000.0):
    clock = SimpleNamespace(epoch=epoch, mono=mono)
    # Keep actual test waits/polls on stdlib time; all deadline consumers share this clock.
    namespace = SimpleNamespace(**vars(time))
    namespace.time, namespace.monotonic = lambda: clock.epoch, lambda: clock.mono
    for module in (campaign_budget, campaign_execution_guard, campaign_pipeline,
                   campaign_serial, campaign_deadline, campaign_isolated_outcome):
        monkeypatch.setattr(module, "time", namespace)
    monkeypatch.setattr(campaign_budget, "monotonic", namespace.monotonic)
    return clock


def _frozen(tmp_path, window=60.0):
    return campaign_budget.with_frozen_deadline(
        CampaignSettings(deadline_at=DEADLINE), tmp_path)


def _stub_serial_surface(monkeypatch, tmp_path, execute):
    state = {"head_commit": "", "clean_commit": "", "dirty_evidence": [], "dirty_digest": ""}
    monkeypatch.setattr(campaign_pipeline, "_git_text", lambda *a: "")
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", lambda *a, **k: state)
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline",
                        lambda *a: tmp_path / "pipeline.yaml")
    monkeypatch.setattr(campaign_pipeline, "_execute_direction_pipeline", execute)
    return state


def test_serial_deadline_guard_sets_event_and_stop(monkeypatch):
    clock = _clock(monkeypatch, CUTOFF - 1)
    deadline = campaign_budget.ExecutionDeadline(CUTOFF, clock.mono + 1)
    event = threading.Event()
    stops = []

    with campaign_execution_guard.SerialDeadlineGuard(
            deadline, event, stop=lambda: stops.append(1)):
        assert not event.is_set()
        clock.epoch = CUTOFF
        end = time.monotonic() + 5
        while not event.is_set() and time.monotonic() < end:
            time.sleep(0.02)

    assert event.is_set()
    assert stops


def test_serial_direction_maps_late_completion_to_budget_blocked(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 5)
    settings = _frozen(tmp_path)

    def execute(*args, **kwargs):
        clock.epoch = CUTOFF  # the serial pipeline crossed the cutoff
        return PipelineStatus.PASSED, "checks passed", []

    _stub_serial_surface(monkeypatch, tmp_path, execute)
    monkeypatch.setattr(campaign_pipeline, "_recover_direction",
                        lambda *a, **k: pytest.fail("recovery ran after the cutoff"))

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="serial"),
        tmp_path / "analysis.json")

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.recovery_status == "PRESERVED"
    assert "deadline" in outcome.reason


def test_serial_direction_passes_cancel_event_into_the_pipeline(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)
    seen = {}

    def execute(*args, **kwargs):
        seen["cancel_event"] = args[6] if len(args) > 6 else kwargs.get("cancel_event")
        return PipelineStatus.PASSED, "checks passed", []

    _stub_serial_surface(monkeypatch, tmp_path, execute)
    monkeypatch.setattr(campaign_pipeline, "_recover_direction",
                        lambda *a, **k: SimpleNamespace(status="", reason="", evidence=[]))

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="serial"),
        tmp_path / "analysis.json")

    assert outcome.status == PipelineStatus.PASSED
    assert isinstance(seen["cancel_event"], threading.Event)


def test_execute_stage_tasks_forwards_cancel_event_to_serial():
    event = threading.Event()
    captured = {}
    stage = SimpleNamespace(or_tasks=[], mode="serial", workers=1, name="s")

    def fake_serial(tasks, **kwargs):
        captured.update(kwargs)
        return []

    def fake_parallel(tasks, workers, **kwargs):
        pytest.fail("parallel path used for a serial stage")

    results, outputs = pipeline_native.execute_stage_tasks(
        stage, [Task(prompt="p", output="o", cwd=".")], [], "", "new", "sess",
        "sess-1", 0, 10.0, 2.0, set(), None, None, fake_parallel, fake_serial,
        cancel_event=event)

    assert results == [] and outputs == []
    assert captured.get("cancel_event") is event


def test_meta_stage_receives_pipeline_cancel_event(monkeypatch):
    event = threading.Event()
    captured = {}

    def fake_meta(stage, stage_outputs, model_override, timeout_override, validate_cmd,
                  session_name, reuse, reuse_legacy, fingerprint_mode, original_prompt,
                  cancel_event=None):
        captured["cancel_event"] = cancel_event
        return [], True

    monkeypatch.setattr(pipeline, "run_meta_stage", fake_meta)
    stage = SimpleNamespace(meta=True, name="review", cwd=None)

    tasks, reused, early = pipeline._build_stage_tasks(
        stage, {}, reuse=False, model_override="", timeout_override=0,
        validate_cmd="", cancel_event=event)

    assert captured["cancel_event"] is event
    assert tasks == [] and reused == [] and early == ([], True)


def test_serial_runner_does_not_spawn_after_cancel(monkeypatch, tmp_path):
    event = threading.Event()
    event.set()
    task = Task(prompt="p", output=str(tmp_path / "o.md"), cwd=str(tmp_path), timeout=5)

    monkeypatch.setattr(runner, "_spawn_agent",
                        lambda *a, **k: pytest.fail("spawned after cancellation"))

    results = runner.run_serial([task], cancel_event=event)

    assert results and results[0].success is False


def test_serial_publication_check_blocks_late_pass(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)
    state = {"head_commit": "", "clean_commit": "", "dirty_evidence": [], "dirty_digest": ""}
    calls = {"count": 0}

    def probe(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:  # final evidence collection crosses the cutoff
            clock.epoch = CUTOFF
        return state

    monkeypatch.setattr(campaign_pipeline, "_git_text", lambda *a: "")
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", probe)
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline",
                        lambda *a: tmp_path / "pipeline.yaml")
    monkeypatch.setattr(campaign_pipeline, "_execute_direction_pipeline",
                        lambda *a, **k: (PipelineStatus.PASSED, "checks passed", []))
    monkeypatch.setattr(campaign_pipeline, "_recover_direction",
                        lambda *a, **k: SimpleNamespace(status="", reason="", evidence=[]))

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="late"), tmp_path / "a.json")

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.recovery_status == "PRESERVED"


def test_serial_ordinary_exception_after_cutoff_is_preserved_budget(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)

    def execute(*args, **kwargs):
        clock.epoch = CUTOFF
        raise RuntimeError("fixture I/O failure after cutoff")

    _stub_serial_surface(monkeypatch, tmp_path, execute)
    monkeypatch.setattr(campaign_pipeline, "_recover_direction",
                        lambda *a, **k: pytest.fail("recovery ran after cutoff"))

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="boom"), tmp_path / "a.json")

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.recovery_status == "PRESERVED"


def test_serial_ordinary_exception_before_cutoff_still_propagates(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)

    def execute(*args, **kwargs):
        raise RuntimeError("fixture ordinary failure")

    _stub_serial_surface(monkeypatch, tmp_path, execute)
    with pytest.raises(RuntimeError, match="fixture ordinary failure"):
        campaign_pipeline.run_direction(
            tmp_path, settings, Direction(module="src", title="boom"),
            tmp_path / "a.json")


def test_finalize_stage_cancelled_event_fails_closed(monkeypatch):
    from pbatch.models import Stage

    event = threading.Event()
    event.set()
    monkeypatch.setattr(pipeline, "_run_stage_commands",
                        lambda *a, **k: pytest.fail("commands ran after cancellation"))
    monkeypatch.setattr(pipeline, "_git_commit_stage",
                        lambda *a, **k: pytest.fail("commit ran after cancellation"))
    monkeypatch.setattr("pbatch.memory.record_stage", lambda *a, **k: None)
    stage = Stage(name="s", from_prompt="x", output="o", cwd=".")

    results, ok = pipeline._finalize_stage(stage, [], [], True, "", cancel_event=event)

    assert results == [] and ok is False


def test_archive_skipped_after_cancel(monkeypatch, tmp_path):
    from pbatch.models import Pipeline

    event = threading.Event()
    event.set()
    monkeypatch.setattr(pipeline, "_archive_pipeline_results",
                        lambda *a, **k: pytest.fail("archived after cancellation"))

    results, failed = pipeline.run_pipeline(
        Pipeline(name="p", stages=[]), cancel_event=event,
        archive_dir=str(tmp_path / "archive"))

    assert results == [] and failed == []


def test_gate_fix_save_receives_cancel_event(monkeypatch, tmp_path):
    from pbatch import gate_fix, runner
    from pbatch.models import Stage

    event = threading.Event()
    captured = {}
    task = Task(prompt="fix", output=str(tmp_path / "gate-fix.md"), cwd=str(tmp_path))
    ok_result = TaskResult(task=task, success=True, stdout="fixed", returncode=0)
    monkeypatch.setattr(runner, "run_task", lambda *a, **k: ok_result)
    monkeypatch.setattr(
        runner, "_save_validated",
        lambda task, result, cmd, cancel_event=None:
        captured.setdefault("cancel_event", cancel_event) or True)
    monkeypatch.setattr("pbatch.memory.record_task", lambda *a, **k: None)
    gate_file = tmp_path / "gate.md"
    gate_file.write_text("VERDICT: FAIL - x\n", encoding="utf-8")
    stage = Stage(name="gate", from_outputs="plan", cwd=str(tmp_path))

    gate_fix.run_gate_fix(stage, [str(gate_file)], 1, "", "sess", cancel_event=event)

    assert captured["cancel_event"] is event


def test_composed_cancel_keeps_native_and_campaign_sources(monkeypatch):
    from pbatch import runner_cancel

    campaign, native = threading.Event(), threading.Event()
    captured = {}
    stage = SimpleNamespace(or_tasks=[], mode="serial", workers=1, name="s")
    monkeypatch.setattr(pipeline_native, "_cancel_event", lambda observer: native)

    def fake_serial(tasks, **kwargs):
        captured.update(kwargs)
        return []

    pipeline_native.execute_stage_tasks(
        stage, [Task(prompt="p", output="o", cwd=".")], [], "", "new", "sess",
        "sess-1", 0, 10.0, 2.0, set(), None, None,
        lambda *a, **k: pytest.fail("parallel path used"), fake_serial,
        cancel_event=campaign)

    combined = captured["cancel_event"]
    assert not combined.is_set()
    native.set()
    assert combined.is_set()
    assert runner_cancel.compose_cancel(None, None) is None


def test_finalize_stage_rechecks_cancel_before_validators(monkeypatch):
    from pbatch.models import Stage

    event = threading.Event()
    monkeypatch.setattr("pbatch.memory.record_stage", lambda *a, **k: None)

    def commands(stage, cancel_event=None):
        event.set()  # the cutoff lands while post-stage commands run
        return True

    monkeypatch.setattr(pipeline, "_run_stage_commands", commands)
    monkeypatch.setattr(pipeline, "run_stage_repo_validators",
                        lambda *a, **k: pytest.fail("validators ran after cancellation"))
    monkeypatch.setattr(pipeline, "_git_commit_stage",
                        lambda *a, **k: pytest.fail("commit ran after cancellation"))
    stage = Stage(name="s", from_prompt="x", output="o", cwd=".")

    results, ok = pipeline._finalize_stage(stage, [], [], True, "", cancel_event=event)

    assert ok is False


def test_run_cmd_set_stops_before_next_command_after_cancel(monkeypatch):
    from pbatch.models import Stage

    event = threading.Event()
    calls = []

    def run_one(cmd, *args):
        calls.append(cmd)
        event.set()
        return True

    monkeypatch.setattr(pipeline, "_run_single_cmd", run_one)
    stage = Stage(name="s", from_prompt="x", output="o", cwd=".")

    ok = pipeline._run_cmd_set(stage, ["a", "b", "c"], "commands", False, ".", event)

    assert calls == ["a"] and ok is False


def test_reuse_decision_refuses_after_cancel_without_deleting(monkeypatch, tmp_path):
    from pbatch import reuse, runner

    event = threading.Event()
    event.set()
    artifact = tmp_path / "out.md"
    artifact.write_text("x", encoding="utf-8")
    monkeypatch.setattr("pbatch.runner.run_file_validators",
                        lambda *a, **k: pytest.fail("validators ran after cancellation"))

    with pytest.raises(runner.RevalidationCancelled):
        reuse.reuse_decision(str(artifact), "true", str(tmp_path), cancel_event=event)
    assert artifact.exists(), "cancellation deleted a previously valid artifact"


def test_serial_preparation_exception_after_cutoff_is_preserved_budget(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)
    state = {"head_commit": "", "clean_commit": "", "dirty_evidence": [], "dirty_digest": ""}

    def late_setup(*args, **kwargs):
        clock.epoch = CUTOFF
        raise RuntimeError("fixture preparation failure after cutoff")

    monkeypatch.setattr(campaign_pipeline, "_git_text", lambda *a: "")
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", lambda *a, **k: state)
    monkeypatch.setattr(campaign_pipeline, "materialize_pipeline", late_setup)

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="prep"), tmp_path / "a.json")

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.recovery_status == "PRESERVED"


def test_serial_recovery_exception_after_cutoff_is_preserved_budget(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)

    def execute(*args, **kwargs):
        return PipelineStatus.PASSED, "checks passed", []

    _stub_serial_surface(monkeypatch, tmp_path, execute)

    def late_recovery(*args, **kwargs):
        clock.epoch = CUTOFF
        raise RuntimeError("fixture recovery failure after cutoff")

    monkeypatch.setattr(campaign_pipeline, "_recover_direction", late_recovery)

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="recover"), tmp_path / "a.json")

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.recovery_status == "PRESERVED"


def test_serial_child_killer_cleans_each_handle_once(monkeypatch):
    from pbatch import campaign_serial

    class FakeProc:
        def __init__(self, code=None):
            self.returncode = code
            self.kills = 0

    alive, reaped = FakeProc(), FakeProc(0)

    def fake_kill(proc):
        proc.kills += 1
        proc.returncode = -9
        return True

    monkeypatch.setattr("pbatch.campaign_serial.proc_snapshot", lambda: (alive, reaped))
    monkeypatch.setattr("pbatch.process_exec._kill_group_locked", fake_kill)
    killer = campaign_serial.SerialChildKiller()

    killer()
    killer()
    killer()

    assert alive.kills == 1
    assert reaped.kills == 1  # an exited leader still gets its first descendant cleanup


def test_repo_validators_stop_between_specs_after_cancel(monkeypatch, tmp_path):
    from pbatch import repo_gates
    from pbatch.models import Stage

    event = threading.Event()
    calls = []
    monkeypatch.setattr(repo_gates, "_collect_repo_specs",
                        lambda raw: [SimpleNamespace(cmd="one"),
                                     SimpleNamespace(cmd="two")])
    monkeypatch.setattr(repo_gates, "_applicable_repo_specs",
                        lambda specs, wd, name: specs)

    def run_spec(spec, cmd, wd, name):
        calls.append(cmd)
        event.set()
        return True, ""

    monkeypatch.setattr(repo_gates, "_run_stage_repo_spec", run_spec)
    stage = Stage(name="s", from_prompt="x", output="o", cwd=str(tmp_path))

    ok, detail = repo_gates.run_stage_repo_validators(
        stage, [], [], "true", cancel_event=event)

    assert calls == ["one"] and ok is False
    assert "cancelled" in detail


def test_finalize_stage_late_gate_commit_still_fails(monkeypatch, tmp_path):
    from pbatch.models import Stage

    event = threading.Event()
    monkeypatch.setattr("pbatch.memory.record_stage", lambda *a, **k: None)

    def commit(*args, **kwargs):
        event.set()  # the commit was admitted before the cutoff but lands after it
        return True

    monkeypatch.setattr(pipeline, "_git_commit_stage", commit)
    stage = Stage(name="s", from_prompt="x", output="o", cwd=str(tmp_path))

    results, ok = pipeline._finalize_stage(
        stage, [], [], True, "", cancel_event=event)

    assert ok is False


def test_dirty_worktree_return_observes_cutoff(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    settings = _frozen(tmp_path)
    state = {"head_commit": "abc", "clean_commit": "other",
             "dirty_evidence": ["x"], "dirty_digest": "d"}

    def probe(*args, **kwargs):
        clock.epoch = CUTOFF
        return state

    monkeypatch.setattr(campaign_pipeline, "_git_text", lambda *a: "abc")
    monkeypatch.setattr(campaign_pipeline, "git_worktree_evidence", probe)

    outcome = campaign_pipeline.run_direction(
        tmp_path, settings, Direction(module="src", title="dirty"), tmp_path / "a.json")

    assert outcome.status == PipelineStatus.BUDGET_BLOCKED
    assert outcome.recovery_status == "PRESERVED"


def test_shared_cleanup_registry_prevents_stale_signal(monkeypatch):
    from pbatch import process_exec

    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.kills = 0

    proc = FakeProc()

    def fake_kill(target):
        target.kills += 1
        target.returncode = -9
        return True

    monkeypatch.setattr(process_exec, "_kill_group_locked", fake_kill)

    process_exec.kill_owned_process(proc)  # guard-first cleanup
    assert process_exec.cleanup_completed(proc)
    process_exec._kill_group(proc)  # runner cleanup afterwards must not re-signal
    assert proc.kills == 1


def test_gate_stage_late_commit_cannot_record_pass(monkeypatch, tmp_path):
    from pbatch.models import Stage

    event = threading.Event()
    stage = Stage(name="gate", from_prompt="x", output="o", cwd=str(tmp_path), gate=True)
    recorded = []
    monkeypatch.setattr(pipeline, "execute_stage", lambda *a, **k: ([], True))
    monkeypatch.setattr(pipeline, "_read_only_stage_ok", lambda *a, **k: True)
    monkeypatch.setattr(pipeline, "_include_reused_results", lambda results, outputs: results)
    monkeypatch.setattr(pipeline, "_handle_gate", lambda *a, **k: "PASS")
    monkeypatch.setattr(pipeline, "_stage_git_baseline", lambda *a, **k: None)

    def late_commit(*args, **kwargs):
        event.set()  # commit admitted before the cutoff but completes after it
        return True

    monkeypatch.setattr(pipeline, "_gate_commit_ok", late_commit)
    monkeypatch.setattr(pipeline, "_record_pipeline_stage",
                        lambda stage, results, ok, verdict, log: recorded.append(ok))
    monkeypatch.setattr("pbatch.memory.record_stage", lambda *a, **k: None)

    stop, _results = pipeline._run_pipeline_stage(
        stage, {}, [], "", False, 0, "new", "sess", "", False, False, "", "", "",
        cancel_event=event)

    assert stop is True
    assert recorded == [False]


def test_unobserved_cleanup_retains_ownership(monkeypatch):
    from pbatch import process_exec

    class FakeProc:
        pass

    proc = FakeProc()
    calls = []
    monkeypatch.setattr(process_exec, "_kill_group_locked",
                        lambda target: calls.append(1) or False)

    assert process_exec.kill_owned_process(proc) is False
    assert not process_exec.cleanup_completed(proc)
    assert process_exec.kill_owned_process(proc) is False
    assert len(calls) == 2, "an unobserved handle must stay retryable"


@pytest.mark.skipif(not hasattr(os, "waitid") or not getattr(os, "WNOWAIT", 0),
                    reason="requires Linux waitid/WNOWAIT")
def test_exited_unreaped_observes_exit_without_reaping():
    import subprocess
    from pbatch import process_exec

    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"],
                            start_new_session=True)
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and not process_exec.exited_unreaped(proc):
            time.sleep(0.01)
        assert process_exec.exited_unreaped(proc) is True
        assert proc.returncode is None, "the leader was reaped before its first cleanup"
    finally:
        if proc.returncode is None:
            proc.wait(timeout=2)


@pytest.mark.parametrize("mono", [1000.0, 10_000.0, 50_000.0])
def test_serial_clock_freezes_exact_window_independent_of_host(monkeypatch, tmp_path, mono):
    clock = _clock(monkeypatch, CUTOFF - 60, mono)
    settings = _frozen(tmp_path)
    deadline = settings._execution_deadline

    assert deadline.monotonic == mono + 60
    assert campaign_budget.gate(settings, tmp_path)()
    assert campaign_execution_guard.time.sleep is time.sleep
    assert campaign_budget.time is not time
    clock.mono += 60  # real test wait/poll clocks and the wall cutoff have not changed.
    assert not campaign_budget.gate(settings, tmp_path)()
