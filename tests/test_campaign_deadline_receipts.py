"""Deadline metadata, live-owner preservation, and independent alarm coverage."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from pbatch import campaign_deadline, campaign_process, campaign_watchdog
from pbatch.campaign_budget import CampaignDeadlineExpired, CampaignGuardFailed, ExecutionDeadline
from pbatch.campaign_models import CampaignSettings, Direction
from pbatch.campaign_process_group import OwnedPipeline
from pbatch.campaign_watchdog import lease_file


def _run(tmp_path, settings):
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("stages: []\n", encoding="utf-8")
    return campaign_process.run_isolated_child(
        [sys.executable, "-c", "import time; time.sleep(30)"], settings,
        Direction(module="src", title="receipt"), tmp_path, pipeline, tmp_path / "log")


def _capture_owned(monkeypatch, *, stop_works=True):
    owned = []
    real_stops = []

    def wrap(child):
        handle = OwnedPipeline(child)
        owned.append(handle)
        real_stops.append(handle.stop)
        if not stop_works:
            handle.stop = lambda: None  # fixture: termination cannot be observed
        return handle

    monkeypatch.setattr(campaign_process, "OwnedPipeline", wrap)
    return owned, real_stops


def test_deadline_receipts_are_exclusive_and_unknown_fields_stay_null(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    direction = Direction(module="src", title="receipt")
    deadline = ExecutionDeadline(1.0, 1.0)
    first = campaign_deadline.deadline_error(
        deadline, direction, tmp_path, tmp_path / "p.yaml", tmp_path / "log", phase="before_spawn")
    first_path = Path(first.evidence[-1])
    original = first_path.read_bytes()
    second = campaign_deadline.deadline_error(
        deadline, direction, tmp_path, tmp_path / "p.yaml", tmp_path / "log", phase="before_spawn")
    assert first.evidence[-1] != second.evidence[-1]
    assert first_path.read_bytes() == original
    payload = json.loads(original)
    assert payload["child_pid"] is None
    assert payload["child_start_ticks"] is None
    assert payload["child_returncode"] is None
    assert payload["group_stop_requested"] is False
    assert payload["effects_rolled_back"] is False
    assert first_path.stat().st_mode & 0o777 == 0o600


def test_receipt_io_failure_does_not_lose_budget_terminal_status(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owned, _stops = _capture_owned(monkeypatch)

    def fail(*_args, **_kwargs):
        raise OSError("fixture receipt I/O refusal")

    monkeypatch.setattr(campaign_deadline, "_create_json", fail)
    settings = CampaignSettings(deadline_at=str(time.time() + 0.3), pipeline_timeout=3)
    with pytest.raises(CampaignDeadlineExpired, match="receipt unavailable") as caught:
        _run(tmp_path, settings)
    assert owned[0].returncode is not None
    assert owned[0] not in campaign_process._ACTIVE_PIPELINES
    assert not any(path.endswith(".json") for path in caught.value.evidence)


def test_unobserved_termination_keeps_live_owner_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owned, stops = _capture_owned(monkeypatch, stop_works=False)
    settings = CampaignSettings(deadline_at=str(time.time() + 0.3), pipeline_timeout=3)
    try:
        with pytest.raises(CampaignDeadlineExpired) as caught:
            _run(tmp_path, settings)
        child = owned[0]
        assert child.returncode is None
        assert child in campaign_process._ACTIVE_PIPELINES
        lease = lease_file(tmp_path, Direction(module="src", title="receipt").direction_id)
        assert json.loads(lease.read_text())["pid"] == child.pid
        record = json.loads(Path(caught.value.evidence[-1]).read_text())
        assert record["child_returncode"] is None
        assert record["group_stop_requested"] is True
    finally:
        for child, stop in zip(owned, stops):
            stop()  # exact owned native fixture, not a prior campaign process
            with campaign_process._ACTIVE_LOCK:
                campaign_process._ACTIVE_PIPELINES.discard(child)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native /proc start identity")
def test_alarm_stops_child_while_lease_setup_is_still_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owned, _stops = _capture_owned(monkeypatch)
    original_prepare = campaign_process.prepare_lease
    observed = []

    def delayed_prepare(*args, **kwargs):
        end = time.monotonic() + 2
        while owned[0].returncode is None and time.monotonic() < end:
            time.sleep(0.01)
        assert owned[0].returncode is not None, "child survived blocked lease setup"
        observed.append(kwargs["process_start_identity"])
        assert observed[-1] == owned[0].start_ticks and observed[-1]
        time.sleep(0.05)  # setup remains unfinished after the child was stopped
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(campaign_process, "prepare_lease", delayed_prepare)
    monkeypatch.setattr(campaign_watchdog, "process_start_ticks",
                        lambda *a: pytest.fail("reaped PID was probed for a new lease identity"))
    settings = CampaignSettings(deadline_at=str(time.time() + 0.3), pipeline_timeout=3)
    with pytest.raises(CampaignDeadlineExpired) as caught:
        _run(tmp_path, settings)
    record = json.loads(Path(caught.value.evidence[-1]).read_text())
    assert record["child_start_ticks"] == observed[0]
    assert record["child_returncode"] is not None


def test_partial_monitor_start_is_cancelled_and_joined(monkeypatch, tmp_path):
    from pbatch import campaign_execution_guard

    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    original_guard = campaign_process.ExecutionGuard
    original_start = campaign_execution_guard.threading.Thread.start
    guards = []

    def capture(*args):
        guard = original_guard(*args)
        guards.append(guard)
        return guard

    def started_then_failed(thread):
        original_start(thread)
        raise RuntimeError("fixture partial monitor start")

    monkeypatch.setattr(campaign_process, "ExecutionGuard", capture)
    monkeypatch.setattr(campaign_execution_guard.threading.Thread, "start", started_then_failed)
    try:
        with pytest.raises(CampaignGuardFailed):
            _run(tmp_path, CampaignSettings(deadline_at=str(time.time() + 60)))
        assert not guards[0]._thread.is_alive(), "partially started deadline monitor leaked"
    finally:
        for guard in guards:
            guard._done.set()
            if guard._thread.ident is not None:
                guard._thread.join(timeout=3)


def test_monitor_construction_failure_stops_unmonitored_child(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owned, stops = _capture_owned(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("fixture alarm allocation refusal")

    monkeypatch.setattr(campaign_process, "ExecutionGuard", unavailable)
    try:
        with pytest.raises(CampaignGuardFailed, match="initialize"):
            _run(tmp_path, CampaignSettings(deadline_at=str(time.time() + 60)))
        assert owned[0].returncode is not None
        assert owned[0] not in campaign_process._ACTIVE_PIPELINES
    finally:
        for child, stop in zip(owned, stops):
            stop()
            with campaign_process._ACTIVE_LOCK:
                campaign_process._ACTIVE_PIPELINES.discard(child)


@pytest.mark.parametrize("failure_phase", ["lease", "heartbeat"])
def test_metadata_error_after_alarm_keeps_budget_refusal(monkeypatch, tmp_path, failure_phase):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owned, stops = _capture_owned(monkeypatch)

    def fail_after_stop(*_args, **_kwargs):
        end = time.monotonic() + 2
        while owned[0].returncode is None and time.monotonic() < end:
            time.sleep(0.01)
        assert owned[0].returncode is not None
        raise OSError("fixture metadata failure after budget stop")

    if failure_phase == "lease":
        monkeypatch.setattr(campaign_process, "prepare_lease", fail_after_stop)
    else:
        monkeypatch.setattr(campaign_watchdog, "_LEASE_WRITE_INTERVAL", 0)
        monkeypatch.setattr(campaign_watchdog, "_touch_lease", fail_after_stop)
    try:
        with pytest.raises(CampaignDeadlineExpired) as caught:
            _run(tmp_path, CampaignSettings(deadline_at=str(time.time() + 0.3)))
        record = json.loads(Path(caught.value.evidence[-1]).read_text())
        assert record["supervision_error_type"] == "OSError"
        assert record["phase"] == "supervision_error"
    finally:
        for stop in stops:
            stop()


@pytest.mark.parametrize("timeout,stalled", [(1, 0), (3, 1)])
def test_unobserved_timeout_or_stall_is_preserved_failure(monkeypatch, tmp_path, timeout, stalled):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    owned, stops = _capture_owned(monkeypatch, stop_works=False)
    try:
        with pytest.raises(CampaignGuardFailed, match="termination"):
            _run(tmp_path, CampaignSettings(pipeline_timeout=timeout,
                                            pipeline_no_progress_timeout=stalled))
        assert owned[0].returncode is None
        assert owned[0] in campaign_process._ACTIVE_PIPELINES
        assert lease_file(tmp_path, Direction(module="src", title="receipt").direction_id).exists()
    finally:
        for child, stop in zip(owned, stops):
            stop()
            with campaign_process._ACTIVE_LOCK:
                campaign_process._ACTIVE_PIPELINES.discard(child)


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("fault", ["failed", "unsettled"])
@pytest.mark.parametrize("cancellation", [KeyboardInterrupt, SystemExit])
def test_monitor_fault_preserves_known_cancellation_only(monkeypatch, known, fault, cancellation):
    from pbatch.campaign_execution_guard import ExecutionGuard
    from pbatch.campaign_deadline import supervision_failure
    from types import SimpleNamespace

    child = SimpleNamespace(returncode=0 if known else None, stop=lambda: None)
    deadline = ExecutionDeadline(time.time() + 60, time.monotonic() + 60)
    guard = ExecutionGuard(child, time.monotonic(), 60, deadline)
    guard.failed = fault == "failed"
    monkeypatch.setattr(guard, "_finish", lambda: fault != "unsettled")
    error = cancellation(7) if cancellation is SystemExit else cancellation("fixture cancellation")
    original_args = error.args
    if known:
        assert guard.__exit__(type(error), error, None) is False
        assert supervision_failure(error, deadline, guard, child, None, None, None, None) is error
        assert error.args == original_args
    else:
        with pytest.raises(CampaignGuardFailed, match="monitor"):
            guard.__exit__(type(error), error, None)


def test_expiry_during_environment_setup_refuses_popen(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))

    def delayed_env(_worktree):
        time.sleep(0.15)
        return {}

    monkeypatch.setattr(campaign_process, "isolated_child_environment", delayed_env)
    monkeypatch.setattr(campaign_process.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("spawn occurred after environment exhausted budget"))
    settings = CampaignSettings(deadline_at=str(time.time() + 0.05), pipeline_timeout=3)
    with pytest.raises(CampaignDeadlineExpired):
        _run(tmp_path, settings)
