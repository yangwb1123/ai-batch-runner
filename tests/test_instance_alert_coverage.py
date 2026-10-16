"""Unknown observation coverage cannot silently resolve a saved alert."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pbatch import config, instance_alerts as alerts, ops


@pytest.fixture
def source(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    project = home / "projects" / "owned-workspace"
    project.mkdir(parents=True)
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": home / "alert-runtime")
    monkeypatch.chdir(tmp_path)
    return project


def _flagged():
    return {"workspace": "owned-workspace", "flag": "silent_unfinished_session",
            "flag_reason": "synthetic metadata cue", "idle_seconds": 7200}


def _stored():
    return json.loads(alerts.alerts_path().read_text(encoding="utf-8"))["alerts"]


def _receipt(project, ok=True):
    path = project / "pipeline" / "receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps({"schema_version": 1, "stage": "owned-stage",
                               "ok": ok, "ts": stamp, "knowledge_cited": 0}) + "\n")


def test_explicit_unknown_coverage_preserves_saved_alert(source):
    alerts.notify([_flagged()], now=1000)
    before = alerts.alerts_path().read_bytes()
    result = alerts.notify([], now=1100, resolvable_workspaces=set())
    assert result["resolved"] == 0
    assert alerts.alerts_path().read_bytes() == before


def test_only_selected_complete_workspace_can_resolve(source):
    first, second = _flagged(), {**_flagged(), "workspace": "outside-window"}
    alerts.notify([first, second], now=1000)
    result = alerts.notify([], now=1100, resolvable_workspaces={"owned-workspace"})
    assert result["resolved"] == 1
    assert set(_stored()) == {"outside-window:silent_unfinished_session"}


@pytest.mark.parametrize("damage", ["missing", "bad_json", "incomplete", "no_receipts",
                                    "empty_receipts", "empty_sessions"])
def test_cli_notify_does_not_resolve_after_lost_observation(source, capsys, damage):
    alerts.notify([_flagged()], now=1000)
    before = alerts.alerts_path().read_bytes()
    if damage != "missing":
        _receipt(source)
        sessions = source / "sessions.jsonl"
        complete = json.dumps({"session_id": "owned-session", "status": "MERGED"})
        sessions.write_text("BAD\n" if damage == "bad_json" else
                            complete + ("" if damage == "incomplete" else "\n"))
        if damage == "no_receipts":
            (source / "pipeline" / "receipts.jsonl").unlink()
        elif damage == "empty_receipts":
            (source / "pipeline" / "receipts.jsonl").write_bytes(b"")
        elif damage == "empty_sessions":
            sessions.write_bytes(b"")
    assert ops.ops_main(["instances", "--notify", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["notified"]["resolved"] == 0
    assert alerts.alerts_path().read_bytes() == before


def test_cli_resolves_only_after_complete_observed_terminal_session(source, capsys):
    alerts.notify([_flagged()], now=1000)
    _receipt(source)
    records = [{"session_id": "owned-session", "status": state}
               for state in ("CREATED", "RUNNING", "MERGED")]
    (source / "sessions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    assert ops.ops_main(["instances", "--notify", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    row = response["view"]["workspaces"][0]
    assert row["session_state"] == "MERGED" and row["flag"] == ""
    assert response["notified"]["resolved"] == 1
    assert _stored() == {}


@pytest.mark.parametrize("arguments", [
    ["--stale-seconds", "0"], ["--stale-seconds", "-1"], ["--repeat-seconds", "-1"],
])
def test_bad_cli_intervals_do_not_read_ledger_or_send_notify(source, monkeypatch, arguments):
    from pbatch import instance_view
    monkeypatch.setattr(instance_view, "instance_rows", lambda **kw: pytest.fail("no reads"))
    monkeypatch.setattr(alerts, "notify", lambda *a, **kw: pytest.fail("no notification"))
    assert ops.ops_main(["instances", "--notify", *arguments]) == 2
    assert not alerts.alerts_path().exists()
