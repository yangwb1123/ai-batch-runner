"""User-range origin reads share local facts; HTTP cannot export/import or select paths."""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from test_task_center_http import request, server
from test_task_goals import contract
from test_task_native_history import codex_rows, write_native
from pbatch import task_sources, task_goal_store, task_intent_store
from pbatch.task_history_export import export_history


def prepare(server):
    data = Path(server[1].data_dir)
    original = task_sources.registered_sources()[0]
    task_sources.unregister_source(original["source_id"])
    source = task_sources.source("custom", str(data), str(data))
    task_sources.register_source(source)
    goal = task_goal_store.define("custom", contract(), "op", "confirmed goal")
    path = write_native(source, codex_rows(source))
    receipt = export_history("custom", goal["goal_key"], str(path), "codex-session", "selected", "", 2, 2,
                            "range.json", "<script>export-label</script>", "private label", 1)
    value = task_intent_store.import_history("custom", goal["goal_key"], receipt["path"], "op", "replay consent",
                                           origin_file=str(path))
    return path, receipt, value


def test_range_projection_matches_cli_and_returns_no_native_paths_or_full_messages(server, capsys):
    from pbatch.ops import ops_main
    path, receipt, value = prepare(server)
    before = task_intent_store.intent_path(value["intent_id"]).read_bytes()
    assert ops_main(["intents", "show", value["intent_id"]]) == 0
    cli = json.loads(capsys.readouterr().out)
    with request(server, "/api/intents/" + value["intent_id"]) as response:
        payload = response.read().decode()
        public = json.loads(payload)
    for key in ("coverage", "origin", "imports", "accounting", "audit", "capabilities"):
        assert public[key] == cli[key]
    origin = public["imports"][0]["export_origin"]
    assert origin["selection"]["first_user"] == origin["selection"]["last_user"] == 2
    assert origin["replay_basis"] == "explicit_native_file_replayed_at_import_only"
    assert "messages" not in public and str(path) not in payload and receipt["path"] not in payload
    assert "private-codex-id" not in payload and public["identity"] == "caller_supplied_roles_not_authenticated"
    assert task_intent_store.intent_path(value["intent_id"]).read_bytes() == before
    with request(server, "/") as response:
        assert b"export-label" not in response.read()


@pytest.mark.parametrize("action", ["export", "import"])
def test_http_cannot_export_or_authorize_the_origin_file(server, action):
    path, receipt, value = prepare(server)
    before = task_intent_store.intent_path(value["intent_id"]).read_bytes()
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/intents/" + action + "?file=" + str(path), method="POST")
    assert exc.value.code == 403
    assert task_intent_store.intent_path(value["intent_id"]).read_bytes() == before


def test_read_views_do_not_reopen_native_input_and_revocation_removes_range_hints(server):
    path, receipt, value = prepare(server)
    path.unlink()
    Path(receipt["path"]).unlink()
    with request(server, "/api/intents/" + value["intent_id"]) as response:
        assert json.loads(response.read())["status"] == "authorized"
    task_intent_store.revoke(value["intent_id"], "op", "stop projection", 1)
    with request(server, "/api/intents/" + value["intent_id"]) as response:
        public = json.loads(response.read())
    assert public["status"] == "revoked" and public["analysis"] is None
    assert public["accounting"]["import_operations"] == 1
