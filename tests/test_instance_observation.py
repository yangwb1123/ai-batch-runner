"""Regression protection for append-order and multi-session observation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pbatch import instance_view as view


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "projects" / "owned-workspace"
    project.mkdir(parents=True)
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    return project


def _append(project, name, value):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")


def _session(project, key, status, **extra):
    _append(project, "sessions.jsonl", {"session_id": key, "status": status, **extra})


def _receipt(project, *, ok=True, age=7200, **extra):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).strftime(view._TS_FORMAT)
    _append(project, "pipeline/receipts.jsonl", {
        "schema_version": 1, "stage": "owned-stage", "ok": ok,
        "ts": stamp, "knowledge_cited": 0, **extra})


def _row(project):
    rows = view.instance_rows()["workspaces"]
    return next(row for row in rows if row["workspace"] == project.name)


@pytest.mark.parametrize("terminal", ["MERGED", "READY", "ABORTED", "CONFLICT",
                                      "DIRTY", "TEST_FAILED", "NO_GATE", "QUEUE_REFUSED"])
def test_terminal_append_replaces_old_running_state(project, terminal):
    _receipt(project)
    _session(project, "same-full-id", "RUNNING")
    _session(project, "same-full-id", terminal)
    row = _row(project)
    assert row["session_state"] == terminal
    assert row["session_unfinished"] is False
    assert row["flag"] == ""


def test_last_append_wins_even_when_timestamps_disagree(project):
    _receipt(project)
    _session(project, "one", "RUNNING", ts="2099-01-01T00:00:00")
    _session(project, "one", "MERGED", ts="2000-01-01T00:00:00")
    assert _row(project)["session_state"] == "MERGED"


def test_another_completed_session_does_not_hide_pending_session(project):
    _receipt(project)
    _session(project, "pending", "RUNNING")
    _session(project, "finished", "RUNNING")
    _session(project, "finished", "MERGED")
    row = _row(project)
    assert row["session_state"] == "MERGED"
    assert row["session_unfinished"] is True
    assert row["sessions_observed"] == 2
    assert row["unfinished_sessions"] == [{"session_id": "pending", "state": "RUNNING"}]
    assert row["flag"] == "silent_unfinished_session"
    assert "MERGED unfinished" not in row["flag_reason"]


def test_latest_pending_session_can_start_after_finished_session(project):
    _receipt(project)
    _session(project, "old", "MERGED")
    _session(project, "new", "RUNNING")
    row = _row(project)
    assert row["session_state"] == "RUNNING"
    assert row["unfinished_sessions"] == [{"session_id": "new", "state": "RUNNING"}]
    assert row["flag"] == "silent_unfinished_session"


@pytest.mark.parametrize("tail", [b'{"status":"MERGED"}', b'{bad json}\n', b'\xff\n'])
def test_bad_or_unfinished_session_tail_does_not_reuse_old_state(project, tail):
    _receipt(project)
    _session(project, "one", "RUNNING")
    with (project / "sessions.jsonl").open("ab") as handle:
        handle.write(tail)
    row = _row(project)
    assert row["session_state"] is None
    assert row["flag"] == ""
    assert row["observation_complete"] is False
    assert row["session_window"]["incomplete"] or row["session_window"]["malformed"]


@pytest.mark.parametrize("extra", [
    {"session_id": ""}, {"session_id": None}, {"session_id": "same\x1b[31m"},
    {"status": "UNKNOWN"}, {"status": True}, {"state": "MERGED"},
])
def test_unknown_or_ambiguous_session_metadata_withholds_silence_cue(project, extra):
    _receipt(project)
    _session(project, "one", "RUNNING")
    _append(project, "sessions.jsonl", {"session_id": "one", "status": "RUNNING", **extra})
    row = _row(project)
    assert row["flag"] == ""
    assert row["session_window"]["malformed"] >= 1


def test_legacy_state_field_is_supported_when_unambiguous(project):
    _receipt(project)
    _append(project, "sessions.jsonl", {"session_id": "legacy", "state": "VALIDATING"})
    row = _row(project)
    assert row["session_state"] == "VALIDATING"
    assert row["session_unfinished"] is True


def test_session_window_limit_does_not_claim_lifetime_or_complete_inventory(project):
    _receipt(project)
    _session(project, "outside-window-pending", "RUNNING")
    for index in range(view.MAX_SESSION_RECORDS):
        _session(project, f"done-{index:03d}", "MERGED")
    row = _row(project)
    assert row["sessions_observed"] == view.MAX_SESSION_RECORDS
    assert row["session_window"]["truncated"] is True
    assert row["observation_complete"] is False
    assert row["flag"] == ""
    payload = view.instance_rows()
    assert payload["complete_inventory"] is False


@pytest.mark.parametrize("bad", [b'{bad}\n', b'\xff\n', b'[]\n',
                                  b'{"schema_version":true,"stage":"fake","ok":false}\n'])
def test_bad_receipt_breaks_failure_sequence_instead_of_joining_failures(project, bad):
    _receipt(project, ok=False)
    with (project / "pipeline/receipts.jsonl").open("ab") as handle:
        handle.write(bad)
    _receipt(project, ok=False)
    row = _row(project)
    assert row["consecutive_failures"] == 1
    assert row["flag"] == ""
    assert row["receipt_window"]["malformed"] >= 1


@pytest.mark.parametrize("invalid", [
    {"ok": "false"}, {"ok": 0}, {"knowledge_cited": "bad"},
    {"knowledge_cited": True}, {"knowledge_cited": -1}, {"stage": []},
])
def test_corrupt_receipt_fields_do_not_crash_or_fabricate_completion(project, invalid):
    _receipt(project, **invalid)
    row = _row(project)
    assert row["stages"] == 0
    assert row["malformed_records"] == 1
    assert row["flag"] == ""


def test_last_receipt_append_is_not_sorted_by_reported_clock(project):
    _receipt(project, stage="first", ts="2099-01-01 00:00:00")
    _receipt(project, stage="second", ts="2000-01-01 00:00:00")
    row = _row(project)
    assert row["last_stage"] == "second"
    assert row["last_activity"] == "2000-01-01 00:00:00"


def test_missing_receipt_ledger_with_session_is_unknown_not_zero(project):
    _session(project, "one", "RUNNING")
    row = _row(project)
    assert row["stages"] is None and row["passes"] is None and row["failures"] is None
    assert row["receipt_window"]["status"] == "missing"
    assert row["flag"] == ""


def test_cli_renders_unknown_counts_and_window_coverage(project, monkeypatch, capsys):
    from pbatch import ops
    monkeypatch.chdir(project.parent.parent.parent)
    _session(project, "one", "RUNNING")
    assert ops.ops_main(["instances"]) == 0
    text = capsys.readouterr().out
    assert "owned-workspace" in text
    assert "未追踪" in text
    assert "有界" in text
    assert ops.ops_main(["instances", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)["view"]
    assert payload["workspaces"][0]["passes"] is None
    assert payload["complete_inventory"] is False


def test_empty_cli_window_does_not_claim_no_running_instances(project, capsys):
    from pbatch import ops
    assert ops.ops_main(["instances"]) == 0
    text = capsys.readouterr().out
    assert "不证明没有运行实例" in text
    assert "有界" in text


def test_observation_never_creates_or_executes_anything(tmp_path, monkeypatch):
    from pbatch import registry
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "absent"))
    monkeypatch.setattr(registry, "list_runs", lambda: pytest.fail("no probe registry"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: pytest.fail("no directory creation"))
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("no writes"))
    assert view.instance_rows()["workspaces"] == []
    assert not (tmp_path / "absent").exists()
