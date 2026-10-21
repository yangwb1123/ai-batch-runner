"""Owned-group fencing and Linux native child/grandchild cutoff cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pbatch.campaign_budget import ExecutionDeadline
from pbatch.campaign_process_group import OwnedPipeline
from pbatch.campaign_watchdog import process_start_ticks, wait_for_pipeline


class _FakeChild:
    pid = 7654321  # synthetic; every signal in these unit cases is intercepted

    def __init__(self, trace, code=None):
        self.returncode = code
        self.trace = trace

    def poll(self):
        self.trace.append("poll")
        return self.returncode

    def wait(self, timeout=None):
        self.trace.append("wait")
        self.returncode = -9
        return -9


def test_group_signals_precede_reaping_even_with_concurrent_observer(monkeypatch):
    trace = []
    entered, release, observer_started = threading.Event(), threading.Event(), threading.Event()
    owned = OwnedPipeline(_FakeChild(trace))

    def signal_group(pid, sig):
        assert pid == 7654321
        trace.append(sig)
        if sig == signal.SIGTERM:
            entered.set()
            assert release.wait(2)

    def observe():
        observer_started.set()
        owned.poll()

    monkeypatch.setattr("pbatch.campaign_process_group.os.killpg", signal_group)
    stopper = threading.Thread(target=owned.stop)
    observer = threading.Thread(target=observe)
    stopper.start()
    try:
        assert entered.wait(2)
        observer.start()
        assert observer_started.wait(2)
        assert "poll" not in trace
    finally:
        release.set()
        stopper.join(2)
        if observer.ident is not None:
            observer.join(2)
    assert not stopper.is_alive() and not observer.is_alive()
    assert trace == [signal.SIGTERM, signal.SIGKILL, "wait", "poll"]


def test_already_reaped_pid_is_never_signalled(monkeypatch):
    trace = []
    owned = OwnedPipeline(_FakeChild(trace, 0))
    monkeypatch.setattr("pbatch.campaign_process_group.os.killpg",
                        lambda *a: pytest.fail("reaped PID was used as group authority"))
    owned.stop()
    assert trace == []


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group fixture")
def test_identity_probe_interruption_stops_fresh_owned_child(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)

    def interrupted(_pid):
        raise KeyboardInterrupt("fixture identity-probe interruption")

    monkeypatch.setattr("pbatch.campaign_process_group.process_start_ticks", interrupted)
    try:
        owned = OwnedPipeline(child)
        with pytest.raises(KeyboardInterrupt):
            owned.capture_identity()
        assert child.returncode is not None
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
@pytest.mark.skipif(os.name != "posix", reason="owned POSIX child fixture")
def test_unknown_child_after_identity_failure_stays_tracked(monkeypatch, tmp_path, error_type):
    from pbatch import campaign_process
    from pbatch.campaign_budget import CampaignGuardFailed
    from pbatch.campaign_models import CampaignSettings, Direction

    original, raw = subprocess.Popen, []

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        raw.append(child)
        return child

    def unavailable(_pid):
        raise error_type("fixture identity acquisition failed")

    monkeypatch.setattr(campaign_process.subprocess, "Popen", spawn)
    monkeypatch.setattr("pbatch.campaign_process_group.process_start_ticks", unavailable)
    monkeypatch.setattr(OwnedPipeline, "stop", lambda _self: None)  # deliberately unconfirmed
    try:
        with pytest.raises(CampaignGuardFailed, match="termination"):
            campaign_process.run_isolated_child(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                CampaignSettings(deadline_at=str(time.time() + 60)), Direction("src", "identity"),
                tmp_path, tmp_path / "p.yaml", tmp_path / "log")
        with campaign_process._ACTIVE_LOCK:
            tracked = [child for child in campaign_process._ACTIVE_PIPELINES if child.pid == raw[0].pid]
        assert len(tracked) == 1 and tracked[0].returncode is None
        assert tracked[0].start_ticks is None
    finally:
        for child in raw:
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            with campaign_process._ACTIVE_LOCK:
                for owner in list(campaign_process._ACTIVE_PIPELINES):
                    if owner.pid == child.pid:
                        campaign_process._ACTIVE_PIPELINES.discard(owner)


def _state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None


def _until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("owned fixture did not reach the required state")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native /proc identity fixture")
def test_expired_watch_kills_group_with_zombie_leader_not_unrelated_child(tmp_path):
    marker = tmp_path / "grandchild.json"
    grandchild_source = (
        "import os,signal,pathlib,json,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid()}}))\n"
        "time.sleep(30)\n"
    )
    leader_source = (
        "import subprocess,sys,pathlib,time,os\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_source!r}])\n"
        f"p=pathlib.Path({str(marker)!r})\n"
        "while not p.exists() or not p.stat().st_size: time.sleep(0.01)\n"
        "os._exit(0)\n"
    )
    owned = OwnedPipeline(subprocess.Popen(
        [sys.executable, "-c", leader_source], start_new_session=True))
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
    grandchild_pid, ticks = None, ""
    try:
        _until(lambda: _state(owned.pid) == "Z")  # do not poll/reap the leader
        grandchild_pid = json.loads(marker.read_text())["pid"]
        ticks = process_start_ticks(grandchild_pid)
        assert ticks and _state(grandchild_pid) != "Z"
        assert unrelated.poll() is None
        result = wait_for_pipeline(
            owned, pipeline=tmp_path / "p.yaml", logfile=tmp_path / "log", timeout=60,
            no_progress_timeout=0, lease_path=tmp_path / "lease", lease={},
            deadline=ExecutionDeadline(time.time() - 1, time.monotonic() - 1), stop=owned.stop)
        assert result.budget_exhausted
        assert owned.returncode is not None
        _until(lambda: _state(grandchild_pid) in (None, "Z"))
        assert unrelated.poll() is None
    finally:
        owned.stop()
        if (grandchild_pid and ticks and process_start_ticks(grandchild_pid) == ticks
                and _state(grandchild_pid) not in (None, "Z")):
            os.kill(grandchild_pid, signal.SIGKILL)  # exact owned fixture identity only
        unrelated.kill()
        unrelated.wait(timeout=2)
