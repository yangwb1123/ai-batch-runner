"""Alert bookkeeping: repeats on a window, resolves itself, never lies about delivery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import instance_alerts as alerts


def _flag(workspace: str = "ws-a", flag: str = "consecutive_failures",
          reason: str = "2 consecutive failed stages", idle: int = 4000) -> dict:
    return {"workspace": workspace, "flag": flag, "flag_reason": reason,
            "idle_seconds": idle, "passes": 1, "failures": 2, "stages": 3,
            "cited_stages": 0, "last_stage": "implement", "last_activity": "",
            "session_state": "RUNNING", "session_unfinished": True,
            "receipt_records": 3, "malformed_records": 0}


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    return str(tmp_path)


def _state(cwd: str) -> dict:
    return json.loads(alerts.alerts_path(cwd).read_text(encoding="utf-8"))


def test_flag_is_announced_then_suppressed_inside_the_repeat_window(cwd):
    rows = [_flag()]
    first = alerts.notify(rows, repeat_seconds=3600, cwd=cwd, now=1000)
    assert (first["due"], first["sent"]) == (1, 1)

    second = alerts.notify(rows, repeat_seconds=3600, cwd=cwd, now=1000 + 60)
    assert (second["due"], second["sent"]) == (0, 0), "must not re-announce"

    later = alerts.notify(rows, repeat_seconds=3600, cwd=cwd, now=1000 + 3600)
    assert (later["due"], later["sent"]) == (1, 1), "but must repeat after the window"


def test_failed_delivery_is_not_recorded_so_the_next_run_retries(cwd, monkeypatch):
    """A webhook that is down must not turn into permanent silence."""
    monkeypatch.setattr(alerts, "deliver", lambda payload, url: False)
    result = alerts.notify([_flag()], url="http://127.0.0.1:1/hook",
                           repeat_seconds=3600, cwd=cwd, now=1000)
    assert result["due"] == 1 and result["sent"] == 0
    assert not alerts.alerts_path(cwd).exists(), "nothing was announced, so nothing stored"

    again = alerts.notify([_flag()], url="http://127.0.0.1:1/hook",
                          repeat_seconds=3600, cwd=cwd, now=1100)
    assert again["due"] == 1, "still due, because nothing was ever announced"


def test_a_cleared_flag_resolves_itself_and_forgets_it(cwd):
    alerts.notify([_flag()], repeat_seconds=3600, cwd=cwd, now=1000)
    assert len(_state(cwd)["alerts"]) == 1

    cleared = alerts.notify([], repeat_seconds=3600, cwd=cwd, now=1100)
    assert cleared["resolved"] == 1
    assert _state(cwd)["alerts"] == {}, "a fixed incident leaves no ghost behind"


def test_state_stays_bounded_by_evicting_the_least_recently_announced(cwd):
    rows = [_flag(workspace=f"ws-{index}") for index in range(alerts.MAX_ALERTS + 5)]
    alerts.notify(rows, repeat_seconds=0, cwd=cwd, now=5000)
    stored = _state(cwd)["alerts"]
    assert len(stored) == alerts.MAX_ALERTS
    assert "ws-0" not in stored, "the oldest announcement is dropped first"


def test_unreadable_or_foreign_state_is_ignored_not_fatal(cwd):
    path = alerts.alerts_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert alerts.sync_alerts([_flag()], cwd=cwd)[0]
    path.write_text(json.dumps({"schema_version": 999, "alerts": {"x": {}}}),
                    encoding="utf-8")
    due, resolved = alerts.sync_alerts([_flag()], cwd=cwd)
    assert len(due) == 1 and resolved == []


def test_deliver_reports_failure_instead_of_raising(cwd):
    assert alerts.deliver({"kind": "x"}, "") is False
    assert alerts.deliver({"kind": "x"}, "http://127.0.0.1:1/never") is False


def test_delivery_is_skipped_and_counted_when_no_url_is_configured(cwd):
    result = alerts.notify([_flag()], url="", cwd=cwd, now=1000)
    assert result["delivered"] is False
    assert result["sent"] == 1, "dry-run still records, so the window starts"


def test_alert_file_never_lands_in_the_project_tree(cwd):
    alerts.notify([_flag()], cwd=cwd, now=1000)
    path = alerts.alerts_path(cwd)
    assert path.is_file()
    assert "ops" in path.parts and "instance-alerts.json" in path.name