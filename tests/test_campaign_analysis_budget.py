"""Analysis-phase campaign budget: admission, per-call bound, honest stops."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from pbatch import (campaign_analysis, campaign_analysis_phase, campaign_analysis_retry,
                    campaign_budget, campaign_exec, runner)
from pbatch.campaign_models import CampaignSettings
from pbatch.campaign_state import StateStore
from pbatch.models import Task, TaskResult


DEADLINE = "2030-01-01T00:00:00+00:00"
CUTOFF = 1893456000.0


def _clock(monkeypatch, epoch, mono=10_000.0):
    clock = SimpleNamespace(epoch=epoch, mono=mono)
    # Replace module references, never the shared stdlib time object used by waits.
    namespace = SimpleNamespace(**vars(time))
    namespace.time, namespace.monotonic = lambda: clock.epoch, lambda: clock.mono
    for module in (campaign_budget, campaign_analysis_retry, campaign_analysis_phase,
                   campaign_analysis, campaign_exec, runner):
        monkeypatch.setattr(module, "time", namespace)
    monkeypatch.setattr(campaign_budget, "monotonic", namespace.monotonic)
    return clock


def _context(tmp_path, **settings):
    return SimpleNamespace(
        root=tmp_path,
        settings=CampaignSettings(state_file="state.jsonl", summary_file="SUMMARY.md",
                                  **settings),
        args=SimpleNamespace(dry_run=False, modules="", model="", provider="", timeout=0,
                             thinking="", agent_bin="pi", memory_mode="off",
                             stream_output="none", retry_delay=0, focus=""),
        store=StateStore(tmp_path / "state.jsonl"),
        snapshot={},
    )


def _frozen(context, tmp_path):
    context.deadline_settings = campaign_budget.with_frozen_deadline(context.settings, tmp_path)
    return context.deadline_settings


def _module(tmp_path, name="mod-a"):
    path = tmp_path / name
    path.mkdir()
    (path / "code.py").write_text("x = 1\n", encoding="utf-8")
    return name


def _quiet(monkeypatch):
    monkeypatch.setattr("pbatch.memory.record_campaign", lambda *a, **k: None)


def test_expired_deadline_dispatches_no_analysis(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _frozen(context, tmp_path)
    _quiet(monkeypatch)
    dispatched = []
    monkeypatch.setattr(campaign_analysis_phase, "run_analysis_tasks",
                        lambda tasks, *a, **k: dispatched.append(list(tasks)) or {})

    directions, failures = campaign_exec._analyze_modules(
        context, [_module(tmp_path)], None, campaign_exec._event)

    assert dispatched == [[]], "expired budget produced analysis work"
    assert directions == {} and failures == 0
    assert context.analysis_budget_stopped is True
    row = context.store.latest_event("mod-a", "__analysis__")
    assert row["status"] == "BUDGET_BLOCKED"
    assert "deadline" in row["reason"]


def test_expiry_during_preparation_blocks_remaining_modules(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _frozen(context, tmp_path)
    _quiet(monkeypatch)
    real_fingerprint = campaign_analysis_phase.analysis_fingerprint
    calls = {"count": 0}

    def fingerprint(*args, **kwargs):
        result = real_fingerprint(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            clock.epoch = CUTOFF
        return result

    monkeypatch.setattr(campaign_analysis_phase, "analysis_fingerprint", fingerprint)
    dispatched = []

    def fake_run(tasks, jobs, retries, retry_delay=10, validator=None, deadline=None):
        dispatched.append([task.prompt for task in tasks])
        return {}

    monkeypatch.setattr(campaign_analysis_phase, "run_analysis_tasks", fake_run)
    modules = [_module(tmp_path, "mod-a"), _module(tmp_path, "mod-b")]

    campaign_exec._analyze_modules(context, modules, None, campaign_exec._event)

    assert len(dispatched) == 1 and len(dispatched[0]) == 1
    for module in modules:
        assert context.store.latest_event(module, "__analysis__")["status"] == "BUDGET_BLOCKED"
    assert context.analysis_budget_stopped is True


def test_analysis_timeout_is_capped_by_remaining_budget(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF - 5)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _frozen(context, tmp_path)
    _quiet(monkeypatch)
    captured = []

    def fake_run(tasks, jobs, retries, retry_delay=10, validator=None, deadline=None):
        captured.extend(tasks)
        assert deadline is not None
        return {}

    monkeypatch.setattr(campaign_analysis_phase, "run_analysis_tasks", fake_run)

    campaign_exec._analyze_modules(context, [_module(tmp_path)], None, campaign_exec._event)

    assert captured and captured[0].timeout == 5


def test_analysis_retries_stop_at_the_cutoff(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 30)
    deadline = campaign_budget.ExecutionDeadline(CUTOFF, clock.mono + 30)
    task = campaign_analysis.build_analysis_task(
        tmp_path, CampaignSettings(state_file="state.jsonl"), "mod-a")
    attempts = []

    def fake_parallel(tasks, workers, **_kwargs):
        attempts.append(len(tasks))
        clock.epoch = CUTOFF
        return [TaskResult(task=tasks[0], success=False, elapsed=0.1,
                           returncode=1, reason="transient network failure")]

    sleeps = []
    monkeypatch.setattr(campaign_analysis, "run_parallel", fake_parallel)
    monkeypatch.setattr(campaign_analysis.time, "sleep", lambda delay: sleeps.append(delay))

    final = campaign_analysis.run_analysis_tasks(
        [task], jobs=1, retries=3, retry_delay=10, deadline=deadline)

    assert attempts == [1]
    assert sleeps == []
    assert final[str(task.output_path())].success is False


def test_queued_analysis_is_refused_at_worker_boundary(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 2)
    deadline = campaign_budget.ExecutionDeadline(CUTOFF, clock.mono + 2)
    settings = CampaignSettings(state_file="state.jsonl")
    first = campaign_analysis.build_analysis_task(tmp_path, settings, "mod-a")
    second = campaign_analysis.build_analysis_task(tmp_path, settings, "mod-b")
    started = []

    def fake_run_retried(task, *_args, **_kwargs):
        started.append(task.prompt)
        clock.epoch = CUTOFF  # the first worker consumes the whole remaining window
        return TaskResult(task=task, success=True, returncode=0, elapsed=0.1)

    monkeypatch.setattr("pbatch.runner._run_retried", fake_run_retried)

    final = campaign_analysis.run_analysis_tasks(
        [first, second], jobs=1, retries=0, deadline=deadline)

    assert len(started) == 1, "a queued analysis spawned after the cutoff"
    refused = final[str(second.output_path())]
    assert refused.success is False
    assert "deadline" in refused.reason
    assert deadline._exhausted.is_set()


def test_pre_spawn_admission_refuses_after_bounded_rate_wait(monkeypatch, tmp_path):
    from pbatch import runner

    clock = _clock(monkeypatch, CUTOFF - 5)
    deadline = campaign_budget.ExecutionDeadline(CUTOFF, clock.mono + 5)
    base = campaign_analysis_retry.deadline_admission(deadline)
    seen = []

    def admit(task, started_at=None):
        seen.append(started_at)
        return base(task, started_at)

    admit.rate_timeout = base.rate_timeout
    task = campaign_analysis.build_analysis_task(
        tmp_path, CampaignSettings(state_file="state.jsonl"), "mod-a")
    waits = []

    def fake_rate(task, cancel_event=None, timeout=None):
        waits.append(timeout)
        clock.epoch = CUTOFF  # the token wait crosses the campaign cutoff
        return True

    monkeypatch.setattr(runner, "_rate_gate", fake_rate)
    monkeypatch.setattr(runner, "_spawn_agent",
                        lambda *a, **k: pytest.fail("spawned after the cutoff"))

    result = runner.run_task(task, admit=admit)

    assert result.success is False
    assert "deadline" in result.reason
    assert len(waits) == 1 and 4.5 <= waits[0] <= 5.0, \
        "rate wait was not bounded by the campaign window"
    assert seen and all(value is not None for value in seen), \
        "run_task did not pass its run start to the admission boundary"


def test_spawn_lock_rechecks_admission_under_contention(monkeypatch, tmp_path):
    import threading
    from pbatch import process_shutdown

    task = Task(prompt="p", output="o", cwd=str(tmp_path))
    refusal = TaskResult(task=task, success=False,
                         reason="campaign deadline exhausted before spawn")
    calls, results = [], []
    started = threading.Event()

    def worker():
        started.set()
        try:
            process_shutdown.spawn_registered(
                lambda: pytest.fail("spawned after the lock-wait cutoff"),
                lambda proc: None,
                admit=lambda: (calls.append("admit"), refusal)[1])
        except process_shutdown.SpawnRefused as refused:
            results.append(refused.result)

    thread = threading.Thread(target=worker)
    with process_shutdown._LOCK:  # another worker is still creating its process
        thread.start()
        assert started.wait(2)
        time.sleep(0.05)
        assert calls == [], "admission ran before the spawn lock was acquired"
    thread.join(2)
    assert calls == ["admit"]
    assert results == [refusal]


def test_run_task_returns_refusal_when_spawn_lock_rejects(monkeypatch, tmp_path):
    from pbatch import process_shutdown, runner

    task = campaign_analysis.build_analysis_task(
        tmp_path, CampaignSettings(state_file="state.jsonl"), "mod-a")
    refusal = TaskResult(task=task, success=False,
                         reason="campaign deadline exhausted before spawn")
    monkeypatch.setattr(runner, "_rate_gate", lambda *a, **k: True)

    def refuse(spawn, stop, admit=None):
        raise process_shutdown.SpawnRefused(refusal)

    monkeypatch.setattr(runner, "spawn_registered", refuse)

    assert runner.run_task(task) is refusal


def test_re_admission_keeps_timeout_measured_from_run_start(monkeypatch):
    clock = _clock(monkeypatch, CUTOFF - 10)
    deadline = campaign_budget.ExecutionDeadline(CUTOFF, clock.mono + 10)
    task = Task(prompt="p", output="o", cwd=".")
    started = clock.mono
    clock.mono += 6  # slow but still in-budget preparation/rate/lock wait

    assert campaign_budget.bound_task_timeout(task, deadline, started) is True
    assert task.timeout == 10, "re-admission reset the window instead of charging elapsed time"

    clock.mono += 4
    assert campaign_budget.bound_task_timeout(task, deadline, started) is False


def test_rate_boundary_re_admits_with_the_run_start(monkeypatch):
    from pbatch import runner_cancel

    clock = _clock(monkeypatch, CUTOFF - 10)
    deadline = campaign_budget.ExecutionDeadline(CUTOFF, clock.mono + 10)
    task = Task(prompt="p", output="o", cwd=".")
    start = clock.mono
    admit = campaign_analysis_retry.deadline_admission(deadline)
    waits = []

    def fake_rate(task, cancel_event=None, timeout=None):
        waits.append(timeout)
        clock.mono += 6
        return True

    cancelled = lambda task: TaskResult(task=task, success=False, reason="cancelled")

    assert runner_cancel.rate_bounded_admission(
        task, None, admit, cancelled, fake_rate, start) is None
    assert waits == [10.0]
    assert task.timeout == 10  # the wait is charged, not the whole window reset


def test_zero_window_after_first_sample_still_refuses_admission(monkeypatch):
    samples = [101.0, 102.0]  # expired() passes once, remaining() observes zero
    clock = SimpleNamespace(mono=9.0)
    namespace = SimpleNamespace(time=lambda: samples.pop(0),
                                monotonic=lambda: clock.mono)
    monkeypatch.setattr(campaign_budget, "time", namespace)
    monkeypatch.setattr(campaign_analysis_retry, "time", namespace)
    deadline = campaign_budget.ExecutionDeadline(102.0, 10.0)
    task = Task(prompt="p", output="o", cwd=".")

    assert campaign_budget.bound_task_timeout(task, deadline, 9.0) is False
    assert deadline._exhausted.is_set()


def test_spawn_lock_gate_refuses_latched_zero_window(monkeypatch, tmp_path):
    from pbatch import process_shutdown, runner

    samples = [101.0, 102.0]
    clock = SimpleNamespace(mono=9.0)
    namespace = SimpleNamespace(time=lambda: samples.pop(0),
                                monotonic=lambda: clock.mono)
    monkeypatch.setattr(campaign_budget, "time", namespace)
    monkeypatch.setattr(campaign_analysis_retry, "time", namespace)
    deadline = campaign_budget.ExecutionDeadline(102.0, 10.0)
    admit = campaign_analysis_retry.deadline_admission(deadline)
    task = Task(prompt="p", output="o", cwd=str(tmp_path))

    with pytest.raises(process_shutdown.SpawnRefused):
        runner._spawn_agent(["true"], str(tmp_path), {}, admit, task, 9.0)


def test_failed_analysis_at_cutoff_is_budget_blocked_not_analysis_failed(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _frozen(context, tmp_path)
    _quiet(monkeypatch)
    task = campaign_analysis.build_analysis_task(tmp_path, context.settings, _module(tmp_path))

    parsed, failed = campaign_analysis_phase._finish_analysis(
        context, "mod-a", task, None, "fp-fixture", campaign_exec._event)

    assert parsed == [] and failed == 0
    row = context.store.latest_event("mod-a", "__analysis__")
    assert row["status"] == "BUDGET_BLOCKED"
    assert "deadline" in row["reason"]
    assert not row.get("failure_fingerprint")
    assert context.analysis_budget_stopped is True


def test_successful_analysis_before_cutoff_is_recorded_as_analyzed(monkeypatch, tmp_path):
    _clock(monkeypatch, CUTOFF - 60)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _frozen(context, tmp_path)
    _quiet(monkeypatch)
    module = _module(tmp_path)
    task = campaign_analysis.build_analysis_task(tmp_path, context.settings, module)
    output = task.output_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('[{"title":"fix it","problem":"p","evidence":["code.py"],'
                      '"acceptance":["a"]}]', encoding="utf-8")
    result = TaskResult(task=task, success=True, returncode=0, elapsed=1.0)

    parsed, failed = campaign_analysis_phase._finish_analysis(
        context, module, task, result, "fp-fixture", campaign_exec._event)

    assert parsed and not failed
    assert context.store.latest_event(module, "__analysis__")["status"] == "ANALYZED"
    assert getattr(context, "analysis_budget_stopped", False) is False


def test_one_frozen_deadline_is_shared_across_analysis_and_implementation(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _quiet(monkeypatch)
    _frozen(context, tmp_path)
    deadline = context.deadline_settings._execution_deadline
    assert deadline.expired(time.monotonic())
    clock.epoch = CUTOFF - 7200  # wall clock rolls back far behind the cutoff

    settings = campaign_budget.context_settings(context)

    assert settings is context.deadline_settings
    assert campaign_budget.context_deadline(context) is deadline
    assert campaign_budget.gate(settings, tmp_path)() is False
    assert campaign_budget.with_frozen_deadline(settings, tmp_path) is settings
    assert "_execution_deadline" not in context.settings.__dict__


def test_run_campaign_returns_terminal_budget_when_analysis_stops(monkeypatch, tmp_path):
    clock = _clock(monkeypatch, CUTOFF - 60)
    context = _context(tmp_path, deadline_at=DEADLINE)
    _quiet(monkeypatch)
    module = _module(tmp_path)
    monkeypatch.setattr(campaign_exec, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(campaign_exec, "discover_modules", lambda *a: [module])
    monkeypatch.setattr(campaign_exec, "_campaign_plan", lambda *a: SimpleNamespace(
        selected={module}, blocked=False, reason="fixture plan", mapping=lambda: {}))
    monkeypatch.setattr(campaign_exec, "_provider_breaker_admission", lambda *a: ("", []))
    monkeypatch.setattr(campaign_exec, "_preflight_validators", lambda *a: None)
    monkeypatch.setattr(campaign_exec, "_report_working_tree_residue", lambda *a: None)
    monkeypatch.setattr(campaign_exec, "_select",
                        lambda *a: pytest.fail("selection ran after an analysis budget stop"))
    monkeypatch.setattr(campaign_exec, "_implement",
                        lambda *a: pytest.fail("implementation ran after an analysis budget stop"))
    monkeypatch.setattr(campaign_analysis, "run_parallel",
                        lambda *a: pytest.fail("analysis dispatched after cutoff"))
    real_fingerprint = campaign_analysis_phase.analysis_fingerprint

    def fingerprint(*args, **kwargs):
        result = real_fingerprint(*args, **kwargs)
        clock.epoch = CUTOFF
        return result

    monkeypatch.setattr(campaign_analysis_phase, "analysis_fingerprint", fingerprint)

    code = campaign_exec.run_campaign(context)

    assert code == 3
    row = context.store.latest_event("__campaign__", "__budget__")
    assert row["status"] == "CAMPAIGN_BUDGET_EXHAUSTED"
    assert "during analysis" in row["reason"]
    assert context.store.latest_event(module, "__analysis__")["status"] == "BUDGET_BLOCKED"


@pytest.mark.parametrize("mono", [1000.0, 10_000.0, 50_000.0])
def test_analysis_clock_freezes_exact_window_independent_of_host(monkeypatch, tmp_path, mono):
    clock = _clock(monkeypatch, CUTOFF - 5, mono)
    context = _context(tmp_path, deadline_at=DEADLINE)
    deadline = _frozen(context, tmp_path)._execution_deadline
    task = campaign_analysis.build_analysis_task(tmp_path, context.settings, "mod-a")

    assert deadline.monotonic == mono + 5
    assert campaign_budget.bound_task_timeout(task, deadline, clock.mono)
    assert task.timeout == 5
    assert campaign_analysis.time.sleep is time.sleep
    assert campaign_budget.time is not time
    clock.mono += 5  # wall clock remains before cutoff; monotonic budget still ends.
    assert not campaign_budget.bound_task_timeout(task, deadline, mono)
