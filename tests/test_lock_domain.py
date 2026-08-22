"""Run-domain lock contract: multi-instance parallel vs exclusive default.

Multi-instance is opt-in: an instance declaring a NON-EMPTY --run-domain
runs parallel to a live holder of a DIFFERENT non-default domain, while
same-domain and any-vs-default conflicts keep the historical exclusive
contract (exit 5).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from pbatch import lock as lock_mod
from pbatch.lock import LOCK_EXIT_CODE, acquire_lock, release_lock


def _hold_lock(tmp_path: Path, domain: str = "") -> Path:
    """Acquire and keep the lock like a background instance would."""
    path = acquire_lock(str(tmp_path), domain=domain)
    assert path is not None
    return path


def test_default_domain_conflict_refuses(tmp_path):
    held = _hold_lock(tmp_path)
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_lock(str(tmp_path))
        assert exc.value.code == LOCK_EXIT_CODE
        with pytest.raises(SystemExit) as exc:
            acquire_lock(str(tmp_path), domain="dev")  # default vs named
        assert exc.value.code == LOCK_EXIT_CODE
    finally:
        release_lock(held)


def test_same_domain_conflict_refuses(tmp_path):
    held = _hold_lock(tmp_path, domain="batch-a")
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_lock(str(tmp_path), domain="batch-a")
        assert exc.value.code == LOCK_EXIT_CODE
    finally:
        release_lock(held)


def test_different_named_domains_run_parallel(tmp_path):
    held = _hold_lock(tmp_path, domain="batch-a")
    try:
        # parallel instance: no lock file, no exception
        assert acquire_lock(str(tmp_path), domain="batch-b") is None
        holder = json.loads((tmp_path / ".pi-batch.lock").read_text())
        assert holder["domain"] == "batch-a"  # holder lock untouched
    finally:
        release_lock(held)


def test_stale_default_lock_is_broken_even_vs_named_domain(tmp_path):
    """A dead holder is never 'parallel': it is stale and must be broken
    (parallel mode is only for live holders)."""
    held = _hold_lock(tmp_path)
    holder = json.loads((tmp_path / ".pi-batch.lock").read_text())
    lock_mod._alive = lambda pid: False  # simulate dead holder
    try:
        path = acquire_lock(str(tmp_path), domain="dev")
        assert path is not None  # stale broken, lock taken
        release_lock(path)
    finally:
        release_lock(held)
        lock_mod._alive = lambda pid: pid != os.getpid() or True


def test_existing_lock_tests_still_pass_with_domain_key(tmp_path):
    """The payload now carries domain; old readers that only look at
    pid/host/argv keep working."""
    held = _hold_lock(tmp_path)
    try:
        holder = json.loads((tmp_path / ".pi-batch.lock").read_text())
        assert holder["domain"] == "default"
        assert holder["pid"] == os.getpid()
    finally:
        release_lock(held)