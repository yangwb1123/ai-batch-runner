"""Global run registry tests: register/heartbeat/list/ps/prune lifecycle."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbatch import registry  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path: Path):
    """Point the registry at a temp dir and ensure clean teardown."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(registry, "_REGISTRY_DIR", runs_dir)
    yield
    registry.unregister()
    # remove any leftover files
    for f in runs_dir.glob("*.json"):
        f.unlink(missing_ok=True)
    for f in runs_dir.glob("*.tmp"):
        f.unlink(missing_ok=True)


def test_register_mark_list_unregister(tmp_path: Path):
    path = registry.register_run(mode="batch", repo=str(tmp_path), session_name="s1")
    assert path.exists()
    runs = registry.list_runs()
    assert len(runs) == 1
    assert runs[0]["mode"] == "batch"
    assert runs[0]["status"] == "queued"
    assert runs[0]["pid"] == os.getpid()
    registry.mark_running()
    runs = registry.list_runs()
    assert runs[0]["status"] == "running"
    registry.unregister()
    assert registry.list_runs() == []


def test_heartbeat_updates_last_active(tmp_path: Path):
    registry.register_run(mode="pipeline", repo=str(tmp_path))
    before = registry.list_runs()[0]["last_active"]
    time.sleep(0.05)
    registry.heartbeat()
    after = registry.list_runs()[0]["last_active"]
    assert after > before


def test_read_only_home_registry_uses_per_user_fallback(
        tmp_path: Path, monkeypatch):
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    fallback = tmp_path / "runtime-runs"
    monkeypatch.setattr(registry, "_REGISTRY_DIR", blocked_parent / "runs")
    monkeypatch.setattr(registry, "_registry_candidates",
                        lambda: [blocked_parent / "runs", fallback])

    path = registry.register_run(mode="batch", repo=str(tmp_path))

    assert path.parent == fallback
    assert path.exists()
    assert registry.list_runs()[0]["repo"] == str(tmp_path)


def test_dead_pid_marked_and_pruned(tmp_path: Path):
    # a registry file whose pid cannot exist on this host
    dead = registry._registry_dir() / "99999999.json"
    dead.write_text(json.dumps({"pid": 99999999, "host": "x", "start": time.time(),
                                "mode": "batch", "repo": "/tmp", "status": "running",
                                "last_active": time.time(), "argv": []}), encoding="utf-8")
    runs = registry.list_runs()
    assert any(r["status"] == "dead" for r in runs)
    removed = registry.prune_dead()
    assert removed >= 1
    assert not dead.exists()


def test_redacted_argv_hides_prompt_values():
    argv = ["pi-batch.py", "-p", "SECRET PROMPT", "--reuse", "--prompt", "ANOTHER"]
    out = registry._redacted_argv(argv)
    assert "SECRET PROMPT" not in out
    assert "ANOTHER" not in out
    assert out.count("<redacted>") == 2


def test_format_ps_smoke(tmp_path: Path):
    registry.register_run(mode="campaign", repo=str(tmp_path), session_name="c1")
    table = registry.format_ps(registry.list_runs())
    assert "campaign" in table
    assert "PID" in table
    assert "REPO" in table
