"""Activity windows must refuse stale/unsafe metadata, not invent freshness."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from pbatch import registry, registry_observation as reader
from pbatch import task_center_activity as activity


@pytest.fixture
def root(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    runs = home / "runs"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(activity, "observation_roots", lambda: [home])
    monkeypatch.setattr(activity.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(registry, "_registry_candidates", lambda: [runs])
    monkeypatch.setattr(registry, "_own_path", None)
    return runs


def _record(root, **fields):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "123.json"
    path.write_text(json.dumps({"pid": 123, "host": socket.gethostname(),
                               "start": 100, "last_active": 200,
                               "status": "running", **fields}), encoding="utf-8")
    return path


@pytest.mark.parametrize("field", ["repo", "mode", "session", "status", "private", "key"])
def test_escaped_unpaired_surrogate_is_refused_before_pid_probe(root, monkeypatch, field):
    root.mkdir(parents=True)
    value = {"pid": 123, "host": socket.gethostname(), field: "\ud800"}
    if field == "key":
        value = {"pid": 123, "host": socket.gethostname(), "\udfff": "quoted"}
    (root / "123.json").write_text(json.dumps(value), encoding="ascii")
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda *a: pytest.fail("no probe"))
    monkeypatch.setattr(registry, "_alive", lambda *a: pytest.fail("no probe"))
    view = activity.activity_snapshot()
    assert view["processes"] == [] and view["diagnostics"]
    snap = registry.run_snapshot()
    assert snap["runs"] == [] and snap["diagnostics"]
    json.dumps(view, ensure_ascii=False).encode("utf-8")


def test_valid_escaped_surrogate_pair_is_not_blanket_rejected(root, monkeypatch):
    _record(root, repo="quoted-\U0001f600")
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: True)
    assert activity.activity_snapshot()["processes"][0]["workspace"] == "quoted-\U0001f600"


def test_activity_admits_directory_before_enumeration_or_pid_probe(root, monkeypatch):
    _record(root)
    original = reader._directory

    def refused(path):
        if path == root:
            raise ValueError("unknown directory ownership")
        return original(path)

    monkeypatch.setattr(reader, "_directory", refused)
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda *a: pytest.fail("no probe"))
    value = activity.activity_snapshot()
    assert value["processes"] == [] and value["diagnostics"]


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_activity_directory_changed_after_complete_record_read_withholds_old_facts(
        root, tmp_path, monkeypatch, change):
    _record(root)
    original = reader.read_record

    def changed(path):
        value = original(path)
        root.rename(tmp_path / "preserved-original-directory")
        if change == "replace":
            root.mkdir()
        return value

    monkeypatch.setattr(reader, "read_record", changed)
    monkeypatch.setattr(activity, "read_record", changed, raising=False)
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda *a: pytest.fail("no old PID probe"))
    value = activity.activity_snapshot()
    assert value["processes"] == [] and value["diagnostics"]
    assert (tmp_path / "preserved-original-directory" / "123.json").exists()


def test_scan_directory_removal_is_unavailable_not_initially_missing(root, tmp_path, monkeypatch):
    _record(root)
    original = reader._scan

    def removed(path, maximum, info):
        values = original(path, maximum, info)
        root.rename(tmp_path / "preserved-directory")
        return values

    monkeypatch.setattr(reader, "_scan", removed)
    rows, info = reader.scan(root)
    assert rows == [] and info["error"]
    assert info["status"] == "unavailable" and info["available"] is False


@pytest.mark.parametrize("field", ["repo", "host", "session", "mode", "status"])
def test_activity_literals_remove_c1_and_bidi_without_reading_more(root, monkeypatch, field):
    path = _record(root, **{field: "quoted\u202e\x85\u2066"})
    before = path.read_bytes()
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: True)
    rows = activity.activity_snapshot()["processes"]
    wire = json.dumps(rows, ensure_ascii=False)
    assert "quoted" in wire and not any(c in wire for c in ("\u202e", "\x85", "\u2066"))
    assert path.read_bytes() == before


def test_future_heartbeat_is_unknown_not_zero_second_idle(root, monkeypatch):
    _record(root, last_active=600)
    monkeypatch.setattr(activity.time, "time", lambda: 500)
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: True)
    row = activity.activity_snapshot()["processes"][0]
    assert row["heartbeat_age_seconds"] is None
    assert row["heartbeat_clock"] == "reported_future"
    assert row["progress"] == "untracked" and row["controllable"] is False


def test_future_registry_start_and_heartbeat_do_not_render_zero_age(root, monkeypatch, capsys):
    _record(root, start=700, last_active=600)
    monkeypatch.setattr(registry.time, "time", lambda: 500)
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    registry.ps_main([])
    output = capsys.readouterr().out
    assert output.count("未追踪") >= 2
    assert " 0s " not in output


def test_activity_invalid_pid_metadata_is_a_visible_gap(root, monkeypatch):
    _record(root, pid="123")
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda *a: pytest.fail("no probe"))
    value = activity.activity_snapshot()
    assert value["processes"] == [] and value["diagnostics"]


def test_activity_window_exposes_missing_not_read_and_false_authority(root):
    value = activity.activity_snapshot()
    assert value["complete_inventory"] is False and value["grants_execution"] is False
    assert value["coverage"] and value["coverage"][0]["status"] == "missing"
    assert not root.exists()


def test_exhausted_global_process_budget_does_not_read_next_registry(root, tmp_path, monkeypatch):
    _record(root)
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    fallback = tmp_path / f"pi-batch-runs-{uid}"
    _record(fallback, host="owned-foreign-fallback")
    original = reader._directory

    def admission(path):
        assert path != fallback, "budget must refuse before fallback stat/enumeration"
        return original(path)

    monkeypatch.setattr(activity, "MAX_PROCESSES", 1)
    monkeypatch.setattr(reader, "_directory", admission)
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: True)
    value = activity.activity_snapshot()
    assert len(value["processes"]) == 1 and value["diagnostics"]
    assert value["coverage"][-1]["status"] == "not_read"
    assert value["coverage"][-1]["admitted_processes"] is None
    assert value["coverage"][-1]["truncated"] is True


@pytest.mark.parametrize("last_active", [None, True, "invalid", -1])
def test_unsupported_heartbeat_never_invents_recent_activity(root, monkeypatch, last_active):
    _record(root, last_active=last_active)
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: True)
    row = activity.activity_snapshot()["processes"][0]
    assert row["heartbeat_age_seconds"] is None and row["heartbeat_clock"] == "untracked"
    assert row["liveness"] == "not_verified" and row["progress"] == "untracked"


def test_help_does_not_claim_a_complete_live_process_list(root, capsys):
    with pytest.raises(SystemExit) as exit_info:
        registry.ps_main(["--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "list live" not in help_text
    assert "bounded" in help_text and not root.exists()
