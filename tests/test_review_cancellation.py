"""Review cancellation retains runner injection and reaps owned processes."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from textual.widgets import Input, Markdown

from pbatch import review
from pbatch.tui import PiBatchTUI, TuiOptions


def _sleeping_process(cwd):
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)


def test_cancel_before_review_never_calls_injected_runner(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    def forbidden(*args):
        pytest.fail("cancelled review must not start the provider")
    payload, outcome = review.run_review(
        "default", (), tmp_path, 30, tmp_path / "report.md",
        runner=forbidden, cancellation=cancelled)
    assert outcome.cancelled and outcome.codex_exit == -1
    assert "cancelled" in payload["summary"]


def test_cancel_racing_with_spawn_kills_new_process(tmp_path, monkeypatch):
    created, release, cancelled = (threading.Event() for _ in range(3))
    processes, outcomes = [], []
    def spawn(_argv, cwd):
        process = _sleeping_process(cwd)
        processes.append(process)
        created.set()
        assert release.wait(2)
        return process
    monkeypatch.setattr(review, "_spawn_process", spawn)
    capture = review.ReviewCapture(cancellation=cancelled)
    worker = threading.Thread(target=lambda: outcomes.append(
        review.run_review_process([], str(tmp_path), 30, capture)))
    worker.start()
    try:
        assert created.wait(2)
        cancelled.set()
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert processes[0].poll() is not None
        assert outcomes[0].cancelled and not outcomes[0].timed_out
    finally:
        release.set()
        for process in processes:
            if process.poll() is None:
                review._kill_group(process)
        worker.join(3)


def test_cancel_kills_review_descendant_without_touching_other_process(tmp_path, monkeypatch):
    cancelled = threading.Event()
    pid_path = tmp_path / "child.pid"
    code = ("import subprocess,sys,time,pathlib; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); time.sleep(30)")
    processes, outcomes = [], []
    def spawn(_argv, cwd):
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        processes.append(process)
        return process
    monkeypatch.setattr(review, "_spawn_process", spawn)
    unrelated = _sleeping_process(str(tmp_path))
    worker = threading.Thread(target=lambda: outcomes.append(review.run_review_process(
        [], str(tmp_path), 30, review.ReviewCapture(cancellation=cancelled))))
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        cancelled.set()
        worker.join(3)
        assert not worker.is_alive() and processes[0].poll() is not None
        child = int(pid_path.read_text())
        deadline = time.monotonic() + 2
        while Path(f"/proc/{child}/stat").exists() and time.monotonic() < deadline:
            if Path(f"/proc/{child}/stat").read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        if Path(f"/proc/{child}/stat").exists():
            assert Path(f"/proc/{child}/stat").read_text().split()[2] == "Z"
        assert unrelated.poll() is None
        assert outcomes[0].cancelled
    finally:
        cancelled.set()
        review._kill_group(unrelated)
        for process in processes:
            review._kill_group(process)
        worker.join(3)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process groups")
def test_cancel_during_pipe_drain_reaps_child_after_review_leader_exit(tmp_path, monkeypatch):
    cancelled = threading.Event()
    child_path = tmp_path / "drain-child.pid"
    code = ("import os,time,pathlib\n"
            "child=os.fork()\n"
            "if child == 0:\n time.sleep(30)\n"
            "else:\n"
            f" pathlib.Path({str(child_path)!r}).write_text(str(child))\n"
            " os._exit(0)\n")
    processes, outcomes = [], []
    def spawn(_argv, cwd):
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.append(process)
        return process
    monkeypatch.setattr(review, "_spawn_process", spawn)
    worker = threading.Thread(target=lambda: outcomes.append(review.run_review(
        "default", (), tmp_path, 30, tmp_path / "report.md", cancellation=cancelled)))
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while (not processes or processes[0].poll() is None) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert processes and processes[0].poll() == 0
        assert worker.is_alive()  # the inherited pipes are still open in the child
        cancelled.set()
        worker.join(2)
        assert not worker.is_alive() and outcomes[0][1].cancelled
        child = int(child_path.read_text())
        stat = Path(f"/proc/{child}/stat")
        if stat.exists():
            assert stat.read_text().split()[2] == "Z"
    finally:
        cancelled.set()
        for process in processes:
            review._kill_group(process)
        worker.join(3)


@pytest.mark.parametrize("quit_app", [False, True])
def test_tui_cancel_or_quit_reaps_running_review(tmp_path, monkeypatch, quit_app):
    processes = []
    def spawn(_argv, cwd):
        process = _sleeping_process(cwd)
        processes.append(process)
        return process
    monkeypatch.setattr(review, "_spawn_process", spawn)
    async def scenario():
        app = PiBatchTUI(TuiOptions(
            cwd=str(tmp_path), agent="missing-agent", runtime="legacy", memory_mode="off"))
        async with app.run_test() as pilot:
            app._slash_review(())
            deadline = time.monotonic() + 3
            while not processes and time.monotonic() < deadline:
                await pilot.pause(0.01)
            assert processes
            if quit_app:
                app.action_quit()
            else:
                app.action_cancel()
                while app._busy and time.monotonic() < deadline:
                    await pilot.pause(0.01)
                assert not app._busy and not app._review_busy
                assert not app.query_one("#composer", Input).disabled
                assert "cancelled" in app.query_one("#conversation", Markdown).source
        assert processes[0].poll() is not None
    asyncio.run(scenario())
