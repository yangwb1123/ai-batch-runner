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


def _hold_lock(tmp_path: Path, domain: str = "", outputs=(),
              session_name: str = "") -> Path:
    """Acquire and keep the lock like a background instance would."""
    path = acquire_lock(str(tmp_path), domain=domain, outputs=tuple(outputs),
                        session_name=session_name)
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


def test_disjoint_declared_outputs_run_parallel(tmp_path):
    """Default-domain instances declare disjoint outputs -> parallel."""
    held = _hold_lock(tmp_path, outputs=("docs/out-a.md",))
    try:
        assert acquire_lock(str(tmp_path),
                            outputs=("docs/out-b.md",)) is None
    finally:
        release_lock(held)


def test_overlapping_outputs_refuse_with_paths(tmp_path):
    held = _hold_lock(tmp_path, outputs=("docs/shared.md", "docs/other.md"))
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_lock(str(tmp_path), outputs=("docs/other.md",))
        assert exc.value.code == LOCK_EXIT_CODE
    finally:
        release_lock(held)


def test_same_declared_session_name_refuses(tmp_path):
    held = _hold_lock(tmp_path, session_name="chat-main")
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_lock(str(tmp_path), session_name="chat-main")
        assert exc.value.code == LOCK_EXIT_CODE
        # a different session name runs parallel
        assert acquire_lock(str(tmp_path), session_name="chat-other") is None
    finally:
        release_lock(held)


def test_declared_request_parallel_to_unlabelled_holder(tmp_path):
    """An unlabelled holder (no declarations) does not block a request
    that declares disjoint outputs."""
    held = _hold_lock(tmp_path)
    try:
        assert acquire_lock(str(tmp_path), outputs=("docs/mine.md",)) is None
    finally:
        release_lock(held)


def test_cli_planned_outputs_scan(tmp_path):
    """cli._planned_outputs reads --output and YAML tasks/stages outputs."""
    import argparse
    from pbatch import cli
    tasks = tmp_path / "t.yaml"
    tasks.write_text(
        "tasks:\n  - prompt: a\n    output: out-a.md\n"
        "  - prompt: b\n", encoding="utf-8")
    args = argparse.Namespace(
        output="direct.md", prompt="p", source=str(tasks))
    planned = cli._planned_outputs(args)
    assert planned == ["direct.md", "out-a.md"]
    bare = argparse.Namespace(output="", prompt="", source=None)
    assert cli._planned_outputs(bare) == []


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