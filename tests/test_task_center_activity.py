"""Process activity is read-only and cannot masquerade as completed work."""
from __future__ import annotations

import json
import socket

from pbatch import task_center_activity as activity


def test_activity_does_not_write_probe_prune_dead_or_expose_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(activity.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: pid == 123)
    directory = tmp_path / "runtime" / "runs"
    directory.mkdir(parents=True)
    for pid in (123, 456):
        (directory / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "host": socket.gethostname(), "repo": "/work/demo",
            "start": 100, "last_active": 200, "status": "running",
            "argv": ["--prompt", "do-not-expose"],
        }))
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    view = activity.activity_snapshot()
    assert {row["presence"] for row in view["processes"]} == {"pid_present", "pid_absent"}
    assert all(row["progress"] == "untracked" and not row["controllable"]
               for row in view["processes"])
    assert "do-not-expose" not in json.dumps(view)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_remote_host_is_not_a_local_pid_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(activity.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tmp_path / "runtime" / "runs"
    directory.mkdir(parents=True)
    (directory / "123.json").write_text(json.dumps({"pid": 123, "host": "another-host"}))
    (directory / "999.json").write_text(json.dumps({"pid": 123, "host": socket.gethostname()}))
    view = activity.activity_snapshot()
    assert len(view["processes"]) == 1
    assert view["processes"][0]["presence"] == "unknown_host"
