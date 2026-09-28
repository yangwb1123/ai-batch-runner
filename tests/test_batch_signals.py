"""Real signal cancellation covers command workers, their children and locks."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process evidence")


def test_real_batch_signals_clean_processes_before_lock_handoff(tmp_path):
    output = tmp_path / "signals"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/smoke-batch-signals.py"), "--output-dir", str(output)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PBATCH_LOCK_FILE=str(tmp_path / "unrelated.lock")))
    report = json.loads((output / "report.json").read_text())
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert report["ok"] and len(report["cases"]) == 4
    assert all(case["exit_code"] == 130 for case in report["cases"])
    assert all(not case["forced_fixture_cleanup"] for case in report["cases"])
    assert not (tmp_path / "unrelated.lock").exists()


def test_signal_inside_main_thread_spawn_is_deferred_until_child_is_owned(tmp_path):
    code = """
import signal, subprocess, sys
from pbatch import process_exec
from pbatch.cli import _sigterm
original = subprocess.Popen
children = []
def spawn(*args, **kwargs):
    child = original(*args, **kwargs)
    children.append(child)
    _sigterm(signal.SIGTERM, None)
    _sigterm(signal.SIGTERM, None)
    return child
process_exec.subprocess.Popen = spawn
try:
    process_exec.run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'],
                             sys.argv[1], 30, 1024, False, 'fixture')
except KeyboardInterrupt:
    assert len(children) == 1 and children[0].poll() is not None
else:
    raise AssertionError('deferred signal was not delivered')
result = process_exec.run_bounded(['not-a-real-command'], sys.argv[1], 1, 1024, False, 'fixture')
assert not result.ok and 'cancelled' in result.stderr and len(children) == 1
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_signal_waits_for_inflight_worker_spawn_before_returning(tmp_path):
    code = """
import signal, subprocess, sys, threading
from pbatch import process_exec
from pbatch.cli import _sigterm
original = subprocess.Popen
entered, release = threading.Event(), threading.Event()
children, results = [], []
def spawn(*args, **kwargs):
    child = original(*args, **kwargs)
    children.append(child)
    entered.set()
    assert release.wait(2)
    return child
process_exec.subprocess.Popen = spawn
worker = threading.Thread(target=lambda: results.append(process_exec.run_bounded(
    [sys.executable, '-c', 'import time; time.sleep(30)'], sys.argv[1], 30, 1024, False, 'fixture')))
worker.start()
assert entered.wait(2)
timer = threading.Timer(.05, release.set)
timer.start()
try:
    _sigterm(signal.SIGTERM, None)
except KeyboardInterrupt:
    assert children[0].poll() is not None
else:
    raise AssertionError('shutdown did not interrupt the CLI')
finally:
    release.set()
    worker.join(2)
    timer.join(2)
assert not worker.is_alive() and len(results) == 1 and not results[0].ok
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, (result.stdout, result.stderr)
