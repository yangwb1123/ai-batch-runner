"""Event-controlled campaign lifecycle regressions (no provider calls)."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import campaign_dispatch, campaign_exec, campaign_pipeline, task_center, task_sources
from pbatch import campaign_merge, task_center_adapters
from pbatch.campaign_models import CampaignSettings, Direction
from pbatch.campaign_pipeline import PipelineOutcome
from pbatch.campaign_state import StateStore, failure_fingerprint


def _context(tmp_path, monkeypatch, concurrency=2):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    monkeypatch.setattr("pbatch.memory.record_campaign", lambda *a: None)
    data = tmp_path / "runtime" / "projects" / "fixture"
    data.mkdir(parents=True)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(data))
    source = task_sources.source("fixture", str(data), str(tmp_path))
    task_sources.register_source(source)
    context = SimpleNamespace(
        root=tmp_path, settings=CampaignSettings(pipeline_concurrency=concurrency),
        args=SimpleNamespace(agent_bin="unused", memory_mode="off", stream_output="none",
                             model="", provider="", timeout=0, auto_merge=False),
        store=StateStore(Path(source["campaign_state"])), snapshot={}, run_id="run", round_id="r1")
    items = [(Direction("src", title), tmp_path / "analysis.json", "fp-" + title)
             for title in ("fast", "slow", "queued")]
    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree", lambda r, s, d: (
        tmp_path / d.title, "branch-" + d.title, "base"))
    return context, items, source


def _inspect_snapshot(source, observed, release, failures):
    try:
        assert observed.wait(5), "no execution-completed ledger fact while sibling blocked"
        tasks = task_center.snapshot(source_id=source["source_id"])["tasks"]
        by_title = {task["title"]: task for task in tasks}
        assert by_title["fast"]["status"] == "EXECUTION_COMPLETED"
        assert by_title["fast"]["scheduling"]["execution_status"] == "PASSED"
        assert by_title["slow"]["status"] == "RUNNING"
        assert by_title["queued"]["status"] == "QUEUED"
        assert by_title["fast"]["delivery"]["merge_status"] == ""
        assert by_title["fast"]["goal"]["acceptance"] == "untracked"
    except BaseException as exc:
        failures.append(exc)
    finally:
        release.set()


def test_live_snapshot_completes_before_sibling_and_keeps_queued_distinct(tmp_path, monkeypatch):
    context, items, source = _context(tmp_path, monkeypatch)
    release = threading.Event()
    observed = threading.Event()
    slow_started = threading.Event()
    failures = []

    def run(settings, direction, *_args):
        if direction.title == "fast":
            assert slow_started.wait(5), "fixture slow sibling did not start"
        else:
            slow_started.set()
            assert release.wait(5), "fixture sibling was not released"
        return PipelineOutcome(direction, "PASSED", "execution only", 1, [], clean_commit="base")

    original = context.store.append
    def append(row):
        result = original(row)
        if row["status"] == "EXECUTION_COMPLETED":
            observed.set()
            assert release.wait(5), "snapshot inspector did not finish"
        return result

    monkeypatch.setattr(context.store, "append", append)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    inspector = threading.Thread(target=_inspect_snapshot,
                                 args=(source, observed, release, failures))
    inspector.start()
    try:
        outcomes, _ = campaign_exec._execute_pipeline_batch(context, items, 2)
    finally:
        release.set()
        inspector.join(6)
    assert not inspector.is_alive()
    assert not failures, failures
    assert len(outcomes) == 3
    assert observed.is_set(), "batch published nothing until every worker finished"


class _TrackingPool(ThreadPoolExecutor):
    """Track outstanding submitted work, not just threads currently executing."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = threading.Lock()
        self.outstanding = self.peak = self.submissions = 0

    def submit(self, *args, **kwargs):
        with self.guard:
            self.outstanding += 1
            self.submissions += 1
            self.peak = max(self.peak, self.outstanding)
        future = super().submit(*args, **kwargs)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, _future):
        with self.guard:
            self.outstanding -= 1


def _track_pool(monkeypatch):
    pools = []
    def create(*args, **kwargs):
        pool = _TrackingPool(*args, **kwargs)
        pools.append(pool)
        return pool
    monkeypatch.setattr(campaign_dispatch, "ThreadPoolExecutor", create)
    return pools


def test_dispatch_caps_submissions_refills_and_observers_are_coordinator_owned(tmp_path, monkeypatch):
    context, items, _ = _context(tmp_path, monkeypatch)
    pools = _track_pool(monkeypatch)
    first_release, slow_release, third_started = [threading.Event() for _ in range(3)]
    both_started = threading.Barrier(3)
    writer_threads, worker_threads, completed = [], [], []
    original = context.store.append
    def append(row):
        writer_threads.append(threading.get_ident())
        if row["status"] == "EXECUTION_COMPLETED":
            completed.append(row["direction"])
        return original(row)
    def run(settings, direction, *_args):
        worker_threads.append(threading.get_ident())
        if direction.title in {"fast", "slow"}:
            both_started.wait(5)
            assert (first_release if direction.title == "fast" else slow_release).wait(5)
        else:
            third_started.set()
        return PipelineOutcome(direction, "PASSED", "", 1, [])
    monkeypatch.setattr(context.store, "append", append)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    with ThreadPoolExecutor(max_workers=1) as coordinator:
        owner = coordinator.submit(threading.get_ident).result(5)
        future = coordinator.submit(campaign_exec._execute_pipeline_batch, context, items, 2)
        try:
            both_started.wait(5)
            assert pools[0].submissions == 2
            first_release.set()
            assert third_started.wait(5), "free slot was not replenished while sibling blocked"
            assert completed[0] == "fast"
            assert not slow_release.is_set()
        finally:
            first_release.set()
            slow_release.set()
        outcomes, _ = future.result(5)
    assert len(outcomes) == pools[0].submissions == 3
    assert pools[0].peak == 2
    assert set(writer_threads) == {owner}
    assert owner not in worker_threads


def test_submitted_worker_stays_queued_until_actual_attempt_entry(tmp_path, monkeypatch):
    context, items, source = _context(tmp_path, monkeypatch)
    submitted, permit_start, running, release = [threading.Event() for _ in range(4)]
    original_run, original_append = campaign_pipeline._safe_worktree_run, context.store.append
    def delayed(*args, **kwargs):
        submitted.set()
        assert permit_start.wait(5)
        return original_run(*args, **kwargs)
    def run(settings, direction, *_args):
        assert release.wait(5)
        return PipelineOutcome(direction, "PASSED", "", 0, [])
    def append(row):
        result = original_append(row)
        if row["status"] == "RUNNING":
            running.set()
        return result
    monkeypatch.setattr(campaign_pipeline, "_safe_worktree_run", delayed)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    monkeypatch.setattr(context.store, "append", append)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(campaign_exec._execute_pipeline_batch, context, items[:1], 1)
        try:
            assert submitted.wait(5)
            task = task_center.snapshot(source_id=source["source_id"])["tasks"][0]
            assert task["status"] == "QUEUED"
            assert task["scheduling"]["queue_wait_seconds"] is None
            assert task["time"]["started_at"] is None
            permit_start.set()
            assert running.wait(5), "start signals were not drained while worker blocked"
            task = task_center.snapshot(source_id=source["source_id"])["tasks"][0]
            assert task["status"] == "RUNNING"
            assert task["scheduling"]["queue_wait_seconds"] >= 0
            assert task["scheduling"]["execution_wall_seconds"] is None
        finally:
            permit_start.set()
            release.set()
        future.result(5)


@pytest.mark.parametrize("failed_status", ["QUEUED", "RUNNING", "EXECUTION_COMPLETED", "PASSED"])
def test_required_ledger_failure_propagates_without_fresh_dispatch(tmp_path, monkeypatch, failed_status):
    context, items, _ = _context(tmp_path, monkeypatch)
    ran, released = [], threading.Event()
    original = context.store.append
    def append(row):
        if row["status"] == failed_status:
            released.set()
            raise OSError("fixture required ledger write failed")
        return original(row)
    def run(settings, direction, *_args):
        ran.append(direction.title)
        if failed_status == "RUNNING":
            assert released.wait(5)
        return PipelineOutcome(direction, "PASSED", "", 0, [])
    monkeypatch.setattr(context.store, "append", append)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    with pytest.raises(OSError, match="required ledger write failed"):
        campaign_exec._execute_pipeline_batch(context, items, 1)
    assert ran == ([] if failed_status == "QUEUED" else
                   ["fast", "slow", "queued"] if failed_status == "PASSED" else ["fast"])


def test_completion_hook_failure_stops_refill_with_sibling_in_flight(tmp_path, monkeypatch):
    context, items, _ = _context(tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    ran = []
    def run(settings, direction, *_args):
        ran.append(direction.title)
        if direction.title == "fast":
            assert started.wait(5)
        else:
            started.set()
            assert release.wait(5)
        return PipelineOutcome(direction, "PASSED", "", 0, [])
    def completed(_outcome):
        release.set()
        raise OSError("fixture outcome observer failed")
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    with pytest.raises(OSError, match="outcome observer failed"):
        campaign_pipeline.run_directions_isolated(
            tmp_path, context.settings, [(d, a) for d, a, _ in items], 2, [], on_outcome=completed)
    assert set(ran) == {"fast", "slow"}


@pytest.mark.parametrize("during_preparation", [False, True])
def test_expired_frozen_deadline_refuses_every_fresh_submit(tmp_path, monkeypatch, during_preparation):
    context, items, _ = _context(tmp_path, monkeypatch)
    expired, ran, starts = threading.Event(), [], []
    context.settings._execution_deadline = SimpleNamespace(expired=lambda _now: expired.is_set())
    pools = _track_pool(monkeypatch)
    def prepare(root, settings, direction):
        if during_preparation:
            expired.set()
        return tmp_path, "branch", "base"
    def run(settings, direction, *_args):
        ran.append(direction.title)
        return PipelineOutcome(direction, "PASSED", "", 0, [])
    def completed(_outcome):
        expired.set()
    monkeypatch.setattr(campaign_pipeline, "_ensure_worktree", prepare)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    outcomes = campaign_pipeline.run_directions_isolated(
        tmp_path, context.settings, [(d, a) for d, a, _ in items], 1, [],
        on_start=lambda direction, timing: starts.append(direction.title), on_outcome=completed)
    expected = [] if during_preparation else ["fast"]
    assert ran == starts == expected
    assert pools[0].submissions == len(expected)
    assert sum(outcome.status == "BUDGET_BLOCKED" for outcome in outcomes) == 3 - len(expected)
    for outcome in outcomes:
        if outcome.status == "BUDGET_BLOCKED":
            assert outcome.scheduling["queue_wait_seconds"] is None
            assert outcome.scheduling["execution_wall_seconds"] is None


@pytest.mark.parametrize("prior_count", [0, 1])
@pytest.mark.parametrize("status", ["PIPELINE_FAILED", "GATE_REJECTED", "VALIDATION_FAILED"])
def test_execution_fact_and_final_record_count_one_failure(tmp_path, monkeypatch, prior_count, status):
    context, items, source = _context(tmp_path, monkeypatch, concurrency=1)
    direction, analysis, fingerprint = items[0]
    signature = failure_fingerprint(fingerprint, status, "fixture failure")
    context.store.append({"campaign": context.settings.name, "module": direction.module,
                          "direction_id": direction.direction_id, "status": "SELECTED",
                          "prior_failure_fingerprint": signature if prior_count else "",
                          "prior_failure_count": prior_count})
    monkeypatch.setattr(campaign_exec, "_run_direction_safe", lambda c, d, a: PipelineOutcome(
        d, status, "fixture failure", 2.5, [str(a)]))
    outcomes, _ = campaign_exec._execute_pipeline_batch(context, items[:1], 1)
    row = context.store.latest_event(direction.module, direction.direction_id)
    assert row["same_failure_count"] == prior_count + 1
    assert len(outcomes) == 1 and outcomes[0].status == status
    rows = context.store.events()
    assert [row["status"] for row in rows] == [
        "SELECTED", "QUEUED", "RUNNING", "EXECUTION_COMPLETED", status]
    task = task_center.snapshot(source_id=source["source_id"])["tasks"][0]
    assert task["analysis"]["failure_observations"] == 1
    assert task["time"]["reported_work_seconds"] == 2.5
    assert task["scheduling"]["execution_status"] == status
    json.dumps(task, allow_nan=False)


@pytest.mark.parametrize("auto_merge", [False, True])
def test_completion_cannot_bypass_opt_in_or_merge_refusal(tmp_path, monkeypatch, auto_merge):
    context, items, _ = _context(tmp_path, monkeypatch)
    context.args.auto_merge = auto_merge
    direction = items[0][0]
    merges, observed = [], []
    owner, original = threading.get_ident(), context.store.append
    def review(*args, **kwargs):
        merges.append(threading.get_ident())
        return campaign_merge.MergeResult("MERGE_BLOCKED", "fixture gate rejected")
    def append(row):
        if row["status"] == "EXECUTION_COMPLETED":
            assert merges == [], "execution observation invoked the merge controller"
            observed.append(row["execution_status"])
        return original(row)
    monkeypatch.setattr(campaign_merge, "review_and_merge", review)
    monkeypatch.setattr(context.store, "append", append)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", lambda *a: PipelineOutcome(
        direction, "PASSED", "", 1, [str(tmp_path / "worktree")], branch="branch", clean_commit="base"))
    outcomes, _ = campaign_exec._execute_pipeline_batch(context, items[:1], 1)
    assert observed == ["PASSED"]
    assert merges == ([owner] if auto_merge else [])
    assert outcomes[0].status == ("MERGE_BLOCKED" if auto_merge else "PASSED")
    row = context.store.latest_event(direction.module, direction.direction_id)
    assert row["scheduling"]["execution_status"] == "PASSED"
    assert row.get("merge_status", "") == ("MERGE_BLOCKED" if auto_merge else "")
    assert context.round_progress == (0 if auto_merge else 1)


def test_failed_outcome_is_durable_before_sibling_finishes(tmp_path, monkeypatch):
    context, items, source = _context(tmp_path, monkeypatch)
    slow_started, release, observed = [threading.Event() for _ in range(3)]
    original = context.store.append
    def run(settings, direction, *_args):
        if direction.title == "fast":
            assert slow_started.wait(5)
            return PipelineOutcome(direction, "GATE_REJECTED", "fixture gate refusal", 2, [])
        slow_started.set()
        assert release.wait(5)
        return PipelineOutcome(direction, "PASSED", "", 1, [])
    def append(row):
        result = original(row)
        if row["status"] == "GATE_REJECTED":
            try:
                assert not release.is_set(), "failure accounting waited for slow sibling"
                task = next(task for task in task_center.snapshot(
                    source_id=source["source_id"])["tasks"] if task["title"] == "fast")
                assert task["status"] == "GATE_REJECTED"
                assert task["analysis"]["failure_observations"] == 1
                assert row["same_failure_count"] == 1
                observed.set()
            finally:
                release.set()
        return result
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", run)
    monkeypatch.setattr(context.store, "append", append)
    try:
        outcomes, _ = campaign_exec._execute_pipeline_batch(context, items[:2], 2)
    finally:
        release.set()
    assert observed.is_set()
    assert sum(outcome.status != "PASSED" for outcome in outcomes) == 1
    failures = [row for row in context.store.events() if row["status"] == "GATE_REJECTED"]
    assert len(failures) == 1


def test_legacy_isolated_call_returns_outcomes_without_observers(tmp_path, monkeypatch):
    context, items, _ = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(campaign_pipeline, "_run_in_worktree", lambda s, d, *a: PipelineOutcome(
        d, "GATE_REJECTED", "fixture fail closed", 0, []))
    outcomes = campaign_pipeline.run_directions_isolated(
        tmp_path, context.settings, [(d, a) for d, a, _ in items], 1, [])
    assert isinstance(outcomes, list) and len(outcomes) == 3
    assert all(isinstance(outcome, PipelineOutcome) for outcome in outcomes)
    assert all(outcome.status == "GATE_REJECTED" for outcome in outcomes)
    assert all(outcome.scheduling["execution_wall_seconds"] >= 0 for outcome in outcomes)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 1e16, "2"])
def test_scheduling_projection_is_bounded_and_never_zero_fills_unknown(value):
    row = {"scheduling": {"queue_wait_seconds": value, "execution_wall_seconds": value,
                          "execution_status": "PASSED", "untrusted_extra": "not projected"}}
    projected = task_center_adapters._scheduling(row)
    assert projected == {"queue_wait_seconds": None, "execution_wall_seconds": None,
                         "execution_status": "PASSED"}
    assert task_center_adapters._scheduling({}) is None
    json.dumps(projected, allow_nan=False)


def test_measured_scheduling_uses_worker_finish_not_observer_time(tmp_path, monkeypatch):
    context, items, _ = _context(tmp_path, monkeypatch)
    clock, owner = SimpleNamespace(now=10.0), threading.get_ident()
    monkeypatch.setattr(campaign_dispatch, "time", SimpleNamespace(
        monotonic=lambda: clock.now if threading.get_ident() == owner else 15.0))
    def run(item, signal, allowed):
        assert allowed()
        signal(12.0)
        return PipelineOutcome(item[0], "PASSED", "", 0, [])
    def observer(*_args):
        clock.now = 1000.0
    outcomes = campaign_dispatch.run_dispatch(
        [(items[0][0],)], 1, run, lambda: True, lambda item: pytest.fail("unexpected refusal"),
        queued_at=10.0, on_start=observer, on_outcome=observer)
    assert outcomes[0].scheduling == {"queue_wait_seconds": 2.0,
                                     "execution_wall_seconds": 3.0, "execution_status": "PASSED"}
