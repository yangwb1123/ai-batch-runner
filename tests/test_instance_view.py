"""Instance progress view: read-only, evidence-flagged, and honest when empty."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import instance_view as view


def _stamp(offset_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    moment = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return moment.strftime(view._TS_FORMAT)


def _receipt(project: Path, stage: str, *, ok: bool = True, age: int = 0,
             cited: int = 0) -> None:
    path = project / "pipeline" / "receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "schema_version": view.RECEIPT_SCHEMA_VERSION,
            "ts": _stamp(age), "stage": stage, "ok": ok,
            "verdict": "", "knowledge_cited": cited, "tasks": [],
        }) + "\n")


def _session(project: Path, status: str) -> None:
    (project / "sessions.jsonl").write_text(
        json.dumps({"status": status, "session_id": "s1"}) + "\n",
        encoding="utf-8")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    return home / "projects"


def _row(payload, name):
    return next(item for item in payload["workspaces"] if item["workspace"] == name)


def test_reports_progress_and_cites_count_per_workspace(runtime):
    project = runtime / "ws-a"
    _receipt(project, "design", age=30)
    _receipt(project, "implement", age=5, cited=1)

    payload = view.instance_rows()
    row = _row(payload, "ws-a")
    assert (row["stages"], row["passes"], row["failures"]) == (2, 2, 0)
    assert row["cited_stages"] == 1
    assert row["last_stage"] == "implement"
    assert 0 <= row["idle_seconds"] < 120
    assert row["flag"] == ""
    assert payload["schema_version"] == view.INSTANCE_VIEW_SCHEMA_VERSION


def test_flags_repeated_failures_without_guessing_from_idle_time(runtime):
    project = runtime / "ws-a"
    _receipt(project, "design", ok=True, age=7200)
    _receipt(project, "implement", ok=False, age=3600)
    _receipt(project, "implement", ok=False, age=3500)

    row = _row(view.instance_rows(), "ws-a")
    assert row["consecutive_failures"] == 2
    assert row["flag"] == "consecutive_failures"


def test_flags_silence_only_with_an_unfinished_session(runtime):
    """Idle time alone is ambiguous; it needs the independent session ledger."""
    silent = runtime / "ws-silent"
    _receipt(silent, "requirements", age=7200)
    _session(silent, "RUNNING")
    done = runtime / "ws-done"
    _receipt(done, "requirements", age=7200)
    _session(done, "MERGED")

    payload = view.instance_rows(stale_seconds=900)
    flagged = _row(payload, "ws-silent")
    assert flagged["session_state"] == "RUNNING"
    assert flagged["flag"] == "silent_unfinished_session"
    assert "7200s" in flagged["flag_reason"]

    finished = _row(payload, "ws-done")
    assert finished["flag"] == "", "silence after MERGED is expected, not a problem"


def test_session_ledger_is_not_filtered_by_the_receipt_schema(runtime):
    """The isolate ledger carries its own shape; dropping its records would
    leave every workspace looking like it had no session at all."""
    project = runtime / "ws-a"
    _receipt(project, "requirements", age=60)
    _session(project, "VALIDATING")

    row = _row(view.instance_rows(), "ws-a")
    assert row["session_state"] == "VALIDATING"
    assert row["session_unfinished"] is True


def test_missing_and_corrupt_ledgers_read_as_no_data_not_as_failure(runtime):
    empty = runtime / "ws-empty"
    (empty / "pipeline").mkdir(parents=True)
    (empty / "pipeline" / "receipts.jsonl").write_text(
        "not json\n{\"schema_version\":1}\n", encoding="utf-8")

    payload = view.instance_rows()
    row = _row(payload, "ws-empty")
    assert row["stages"] == 0 and row["idle_seconds"] is None
    assert row["flag"] == ""
    # The malformed record is counted as a record, not as a finished stage,
    # so a broken ledger stays visible without inflating activity.
    malformed = _row(payload, "ws-empty")
    assert malformed["receipt_records"] == 1
    assert malformed["malformed_records"] == 1


def test_workspace_without_ledgers_is_omitted_and_missing_root_is_reported(
        runtime, tmp_path):
    (runtime / "empty-dir").mkdir()
    assert view.instance_rows()["workspaces"] == []

    monkey = pytest.MonkeyPatch()
    monkey.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "absent"))
    try:
        payload = view.instance_rows()
        assert payload["workspaces"] == []
        assert payload["unavailable"] is not None
    finally:
        monkey.undo()