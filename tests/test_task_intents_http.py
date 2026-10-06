"""Authorized hints are readable globally, but clients cannot supply files or write grants."""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from test_task_center_http import request, server
from test_task_goals import contract
from test_task_intents import manifest, message
from pbatch import task_goal_store, task_intent_store, task_sources


def import_for_server(server):
    data = Path(server[1].data_dir)
    goal = task_goal_store.define("custom", contract(), "operator", "goal confirmed")
    file = data / "prompt-history.json"
    file.write_text(json.dumps(manifest(goal, [message(text="实现 " + "private-full-body " * 80)])))
    return task_intent_store.import_history("custom", goal["goal_key"], str(file), "operator", "local consent")


def test_global_history_api_uses_projection_and_never_returns_raw_full_text(server):
    value = import_for_server(server)
    with request(server, "/api/intents") as response:
        payload = response.read().decode()
        row = json.loads(payload)["intents"][0]
    assert row["intent_id"] == value["intent_id"]
    assert '"text":' not in payload and "messages" not in row and "private-full-body " * 80 not in payload
    with request(server, "/api/intents/" + value["intent_id"]) as response:
        assert json.loads(response.read())["analysis"]["hints"] == row["analysis"]["hints"]
    with request(server, "/api/intents?source=/etc/passwd") as response:
        assert json.loads(response.read())["intents"] == []


@pytest.mark.parametrize("token", ["", "wrong"])
def test_history_api_does_not_bypass_existing_auth(server, token):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/intents", token=token)
    assert exc.value.code == 401


def test_history_api_cannot_import_revoke_or_read_an_arbitrary_path(server):
    import_for_server(server)
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/intents/import", method="POST")
    assert exc.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/intents/..%2Fetc%2Fpasswd")
    assert exc.value.code == 400
    server[1].global_tasks = False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/intents")
    assert exc.value.code == 404


def test_revocation_removes_inferences_from_the_next_http_read(server):
    value = import_for_server(server)
    task_intent_store.revoke(value["intent_id"], "operator", "withdraw consent", 1)
    with request(server, "/api/intents/" + value["intent_id"]) as response:
        result = json.loads(response.read())
    assert result["status"] == "revoked" and result["analysis"] is None


def test_native_import_has_shared_http_metadata_without_raw_history_or_http_writes(server, capsys):
    from pbatch.ops import ops_main
    from test_task_native_history import codex_rows, write_native
    data = Path(server[1].data_dir)
    original = task_sources.registered_sources()[0]
    task_sources.unregister_source(original["source_id"])
    source = task_sources.source("custom", str(data), str(data))
    task_sources.register_source(source)
    goal = task_goal_store.define("custom", contract(), "operator", "confirmed goal")
    path = write_native(source, codex_rows(source))
    value = task_intent_store.import_history("custom", goal["goal_key"], str(path), "op", "consent",
                                           format="codex-session", channel="selected_codex")
    before = task_intent_store.intent_path(value["intent_id"]).read_bytes()
    assert ops_main(["intents", "show", value["intent_id"]]) == 0
    cli = json.loads(capsys.readouterr().out)
    with request(server, "/api/intents/" + value["intent_id"]) as response:
        payload = response.read().decode()
        http = json.loads(payload)
    for key in ("origin", "identity", "coverage", "imports", "audit", "accounting", "capabilities"):
        assert http[key] == cli[key]
    assert "messages" not in http and "private-codex-id" not in payload and str(path) not in payload
    assert "DO NOT COPY" not in payload
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/intents/import?format=codex-session&file=" + str(path), method="POST")
    assert exc.value.code == 403
    assert task_intent_store.intent_path(value["intent_id"]).read_bytes() == before


def test_quoted_prompt_markup_is_not_embedded_in_the_browser_page(server):
    data = Path(server[1].data_dir)
    goal = task_goal_store.define("custom", contract(), "operator", "scope")
    file = data / "export.json"
    file.write_text(json.dumps(manifest(goal, [message(text="实现 <script>alert(97)</script>")])))
    task_intent_store.import_history("custom", goal["goal_key"], str(file), "operator", "consent")
    with request(server, "/") as response:
        page = response.read().decode()
    assert '<tbody id="intents">' in page
    assert "alert(97)" not in page
    assert "td.textContent=text" in page
