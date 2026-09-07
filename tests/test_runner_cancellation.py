"""Scoped local task and validator cancellation contracts."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from pbatch import config, runner


def test_active_task_cancel_kills_only_its_process_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "agent.pid"
    agent = tmp_path / "agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        f"echo $$ > '{pidfile}'\n"
        "while true; do sleep 30; done\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    cancel = threading.Event()

    def request_cancel():
        for _ in range(200):
            if pidfile.exists():
                cancel.set()
                return
            time.sleep(0.01)
        raise AssertionError("agent did not start")

    trigger = threading.Thread(target=request_cancel)
    trigger.start()
    result = runner.run_task(
        runner.Task(prompt="cancel", cwd=str(tmp_path), timeout=30),
        cancel_event=cancel)
    trigger.join(2)

    assert not result.success and result.reason == "cancelled"
    assert not trigger.is_alive()
    child_pid = int(pidfile.read_text(encoding="utf-8").strip())
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        return
    state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
    assert state == "Z"


def test_parallel_result_callback_releases_each_task_as_completed(monkeypatch):
    tasks = [runner.Task(prompt="one"), runner.Task(prompt="two")]
    seen = []

    def fake_retried(task, *args, **kwargs):
        return runner.TaskResult(task, True, returncode=0)

    monkeypatch.setattr(runner, "_run_retried", fake_retried)
    results = runner.run_parallel(
        tasks, workers=1, result_callback=lambda result: seen.append(result.task))

    assert [result.task for result in results] == tasks
    assert seen == tasks


def test_bounded_validator_observes_scoped_cancel(tmp_path):
    cancel = threading.Event()
    trigger = threading.Thread(
        target=lambda: (time.sleep(0.05), cancel.set()))
    trigger.start()
    result = runner.run_validation(
        "sleep 30", str(tmp_path), timeout=30, cancel_event=cancel)
    trigger.join(2)

    assert result.ok is False
    assert result.timed_out is False
    assert result.exit_code == -1
    assert not trigger.is_alive()
