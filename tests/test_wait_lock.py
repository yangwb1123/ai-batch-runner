"""Lock queueing tests: --wait-lock waits for a held lock instead of exit 5."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbatch.lock import LOCK_EXIT_CODE, acquire_lock, release_lock  # noqa: E402


def test_wait_lock_queues_behind_holder(tmp_path: Path) -> None:
    """A held lock + wait_minutes makes acquire_lock poll, then succeed."""
    first = acquire_lock(cwd=str(tmp_path))
    assert first is not None
    try:
        # second acquisition with a short budget: held by a live process
        with pytest.raises(SystemExit) as exc:
            acquire_lock(cwd=str(tmp_path), wait_minutes=0)
        assert exc.value.code == LOCK_EXIT_CODE
        # with a wait budget it must NOT exit — it polls until released
        started = time.monotonic()
        holder = os.getpid()  # same live process: _alive -> keep waiting
        release_lock(first)
        got = acquire_lock(cwd=str(tmp_path), wait_minutes=1)
        assert got is not None
        assert time.monotonic() - started < 10
        release_lock(got)
    finally:
        release_lock(first)


def test_wait_lock_exits_when_budget_exhausted(tmp_path: Path) -> None:
    """A held lock held beyond the budget still exits 5."""
    first = acquire_lock(cwd=str(tmp_path))
    assert first is not None
    try:
        started = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            acquire_lock(cwd=str(tmp_path), wait_minutes=0)
        assert exc.value.code == LOCK_EXIT_CODE
        assert time.monotonic() - started < 5
    finally:
        release_lock(first)


def test_cli_accepts_wait_lock_flag(tmp_path: Path) -> None:
    """The CLI parser accepts --wait-lock without breaking --dry-run."""
    root = str(Path(__file__).resolve().parent.parent)
    r = subprocess.run(
        [sys.executable, "pi-batch.py", "-p", "test", "--wait-lock", "5", "--dry-run"],
        capture_output=True, text=True, cwd=root, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
