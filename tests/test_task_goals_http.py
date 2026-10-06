"""The browser gets the same goal projection but never gains metadata/control writes."""
from __future__ import annotations

import json
import urllib.error

import pytest

from test_task_center_http import request, server
from test_task_goals import contract
from test_task_consumption import run, call, publish
from pbatch import task_sources
from pbatch import task_goal_store as store


def test_goal_http_snapshot_detail_and_source_scope_are_identical(server):
    value = store.define("custom", contract(), "operator", "confirmed")
    with request(server, "/api/tasks") as response:
        view = json.loads(response.read())
    with request(server, "/api/goals") as response:
        goals = json.loads(response.read())["goals"]
    assert goals == view["goals"]
    with request(server, "/api/goals/" + value["goal_key"]) as response:
        assert json.loads(response.read()) == goals[0]
    with request(server, "/api/goals?source=/etc") as response:
        assert json.loads(response.read())["goals"] == []


@pytest.mark.parametrize("token", ["", "wrong"])
def test_goals_api_requires_existing_global_auth(server, token):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/goals", token=token)
    assert exc.value.code == 401


def test_workspace_mode_does_not_gain_global_goals_and_http_cannot_attest(server):
    _, handler = server
    handler.global_tasks = False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/goals")
    assert exc.value.code == 404
    handler.global_tasks = True
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/goals/attest", method="POST")
    assert exc.value.code == 403


def test_invalid_goal_path_is_not_a_filesystem_lookup(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/goals/..%2Fetc%2Fpasswd")
    assert exc.value.code == 400


def test_goal_markup_is_only_text_and_does_not_become_embedded_html(server):
    manifest = contract()
    manifest["statement"] = "<img src=x onerror=alert(99)>"
    store.define("custom", manifest, "operator", "confirmed")
    with request(server, "/") as response:
        page = response.read().decode()
    assert '<tbody id="goals">' in page
    assert "onerror=alert(99)" not in page
    assert "td.textContent=text" in page


def test_goal_consumption_http_and_cli_share_window_units_without_writes(server, capsys):
    from pbatch.ops import ops_main
    sources, _ = task_sources.discover_sources()
    source = next(row for row in sources if row["name"] == "custom")
    checkpoint = run(source)
    value = store.define("custom", contract(), "operator", "confirmed")
    store.bind(value["goal_key"], checkpoint["run_id"], "operator", "same goal", 1)
    publish(source, [checkpoint], [call(source, checkpoint)])
    with request(server, "/api/goals/" + value["goal_key"]) as response:
        goal = json.loads(response.read())
    assert goal["consumption"]["usage"]["total_tokens"] == 10
    assert goal["consumption"]["billing"]["comparable_currency"] == "USD"
    assert goal["consumption"]["outcomes"]["independently_accepted_goals"] is None
    assert ops_main(["goals", "show", value["goal_key"]]) == 0
    assert json.loads(capsys.readouterr().out) == goal
