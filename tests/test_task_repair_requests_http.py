"""Authenticated request views grant no registration, withdrawal or execution API."""
from __future__ import annotations

import json
import urllib.error

import pytest

from test_task_center_http import server, request
from test_task_repair_requests import source, prepared, register, view_of
from pbatch import task_center
from pbatch.ops import ops_main


def test_http_cli_and_shared_snapshot_use_same_request_projection(server, prepared, capsys):
    source = prepared[0]
    value = register(prepared)
    expected = view_of(value, source)
    with request(server, "/api/repairs?source=" + source["source_id"]) as response:
        assert response.headers["Cache-Control"] == "no-store"
        records = json.loads(response.read())
    with request(server, "/api/repairs/" + value["repair_id"]) as response:
        detail = json.loads(response.read())
    assert ops_main(["repairs", "show", value["repair_id"]]) == 0
    assert json.loads(capsys.readouterr().out) == detail == expected
    assert records["repairs"] == [expected]
    assert ops_main(["repairs", "check", value["repair_id"], "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {"repair_id": value["repair_id"], "assessment": expected["assessment"]}
    with request(server, "/api/tasks") as response:
        assert json.loads(response.read())["repairs"] == [expected]


@pytest.mark.parametrize("token", ["", "wrong"])
def test_request_routes_require_existing_authentication(server, prepared, token):
    value = register(prepared)
    for route in ("/api/repairs", "/api/repairs/" + value["repair_id"]):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, route, token=token)
        assert exc.value.code == 401


def test_workspace_default_does_not_gain_request_access(server, prepared):
    register(prepared)
    server[1].global_tasks = False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/repairs")
    assert exc.value.code == 404


@pytest.mark.parametrize("route", ["/api/repairs/../goals", "/api/repairs/not-an-id",
                                    "/api/repairs/rq-" + "g" * 32])
def test_request_detail_accepts_only_typed_id_not_path(server, route):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, route)
    assert exc.value.code == 400


@pytest.mark.parametrize("action", ["register", "withdraw", "execute", "resume", "reset-budget", "check", "inspect-workspace"])
def test_no_http_mutation_or_execution_route_is_added(server, prepared, action):
    value = register(prepared)
    before = view_of(value, prepared[0])
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/repairs/" + value["repair_id"] + "/" + action, method="POST")
    assert exc.value.code == 403
    assert view_of(value, prepared[0]) == before


def test_http_reads_and_query_flags_never_trigger_workspace_git_inspection(server, prepared, monkeypatch):
    from pbatch import task_workspace_git, task_repair_workspace
    value = register(prepared)
    def forbidden(*args, **kwargs):
        pytest.fail("read-only HTTP refresh cannot spawn Git or inspect a workspace")
    monkeypatch.setattr(task_workspace_git, "run_bounded", forbidden)
    monkeypatch.setattr(task_repair_workspace, "inspect_workspace", forbidden)
    with request(server, "/api/repairs/" + value["repair_id"] + "?inspect-workspace=true&expected-head=" + "0" * 40) as response:
        record = json.loads(response.read())
        assert "git_observation" not in record
        assert record["assessment"]["live_preflight_performed"] is False
    with request(server, "/api/tasks") as response:
        assert len(json.loads(response.read())["repairs"]) == 1


def test_client_source_path_is_not_followed_or_selected(server, prepared):
    value = register(prepared)
    with request(server, "/api/repairs?source=/etc") as response:
        assert json.loads(response.read())["repairs"] == []
    assert task_center.snapshot(source_id=prepared[0]["source_id"])["repairs"][0]["repair_id"] == value["repair_id"]
