"""Authenticated shared preservation projection, not a remote disposal controller."""
from __future__ import annotations

import copy
import json
import shutil
import urllib.error

import pytest

from test_task_center_http import server, request
from test_task_retention import source, retained, _view
from test_task_center_browser import run_browser
from pbatch import task_attention, task_center, task_retention_store as store
from pbatch.ops import ops_main


def test_resource_cli_http_and_task_snapshot_share_one_stat_only_plan(server, retained, capsys):
    value = retained[2]
    expected = _view(retained)
    with request(server, "/api/resources?source=" + retained[0]["source_id"]) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert json.loads(response.read())["resources"] == [expected]
    with request(server, "/api/resources/" + value["resource_id"]) as response:
        assert json.loads(response.read()) == expected
    assert ops_main(["retention", "show", value["resource_id"]]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    with request(server, "/api/tasks") as response:
        assert json.loads(response.read())["resources"] == [expected]


@pytest.mark.parametrize("token", ["", "wrong"])
def test_authentication_does_not_expand_for_retention(server, retained, token):
    for route in ("/api/resources", "/api/resources/" + retained[2]["resource_id"]):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, route, token=token)
        assert exc.value.code == 401


def test_workspace_server_defaults_remain_closed(server, retained):
    server[1].global_tasks = False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/resources")
    assert exc.value.code == 404


@pytest.mark.parametrize("route", ["/api/resources/../goals", "/api/resources/rr-" + "g" * 32,
                                   "/api/resources/not-an-id", "/api/resources/%2Fetc%2Fpasswd"])
def test_resource_detail_requires_typed_identity_never_filesystem_path(server, route):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, route)
    assert exc.value.code == 400


@pytest.mark.parametrize("action", ["register", "pin", "extend", "unpin", "archive", "delete", "cleanup", "force", "preserve", "verify-copy"])
def test_http_has_no_metadata_mutation_archiving_or_deletion_actions(server, retained, action):
    before = retained[1].read_bytes(), store.resource_path(retained[2]["resource_id"]).read_bytes()
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/resources/" + retained[2]["resource_id"] + "/" + action, method="POST")
    assert exc.value.code == 403
    assert (retained[1].read_bytes(), store.resource_path(retained[2]["resource_id"]).read_bytes()) == before


def test_query_paths_and_control_flags_cannot_import_or_delete_resources(server, retained, monkeypatch):
    monkeypatch.setattr(store, "register", lambda *args, **kwargs: pytest.fail("query attempted registration"))
    with request(server, "/api/resources?source=/etc&path=/etc/passwd&cleanup=true&confirm=true") as response:
        assert json.loads(response.read())["resources"] == []
    with request(server, "/api/resources/" + retained[2]["resource_id"] + "?path=/etc/passwd&delete=true") as response:
        assert json.loads(response.read())["plan"]["decision"] == "preserve"


def test_preservation_metadata_shared_but_http_never_reads_payload_or_verifies(server, retained, monkeypatch):
    from pbatch import task_preservation as preservation
    value = preservation.preserve(retained[2]["resource_id"], "operator", "private byte snapshot", 1, confirm=True)
    monkeypatch.setattr(preservation, "copy_stream", lambda *args: pytest.fail("HTTP read source/copy bytes"))
    monkeypatch.setattr(preservation, "verify_copy", lambda *args, **kwargs: pytest.fail("HTTP triggered verification"))
    with request(server, "/api/resources/" + retained[2]["resource_id"] + "?verify-copy=true&confirm=true") as response:
        row = json.loads(response.read())
        assert row["preservation"]["receipts"][0]["preservation_id"] == value["preservation_id"]
        assert row["preservation"]["complete_archive_verified"] is False
        assert row["plan"]["decision"] == "preserve" and row["archive_verified"] is False
    with pytest.raises(urllib.error.HTTPError):
        request(server, "/api/preservations/" + value["preservation_id"] + "/payload")


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
def test_web_file_copy_receipt_uses_quoted_text_not_archive_or_cleanup_authority(retained):
    from pbatch import task_preservation as preservation
    preservation.preserve(retained[2]["resource_id"], "operator", "private byte snapshot", 1, confirm=True)
    view = task_center.snapshot()
    marker = '<svg onload="alert(1)">archive instruction</svg>'
    view["resources"][0]["preservation"]["receipts"][0]["reason"] = marker
    result = run_browser([view], "resource")
    row = json.loads(result["detail"])
    assert result["tags"] == 0 and len(result["requests"]) == 1
    assert row["preservation"]["receipts"][0]["reason"] == marker
    assert row["archive_verified"] is False and row["capabilities"]["archive"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
def test_web_resource_plan_is_text_only_and_has_no_disposal_controls(retained):
    view = task_center.snapshot()
    markup = '<img src=x onerror="alert(1)"><script>alert(1)</script>'
    view["resources"][0]["audit"][0]["reason"] = markup
    result = run_browser([view], "resource")
    assert result["tags"] == 0 and len(result["requests"]) == 1
    assert "保留；未授清场权" in result["resources"]
    assert "过期不是删除批准" in result["resources"]
    assert json.loads(result["detail"])["audit"][0]["reason"] == markup


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
@pytest.mark.parametrize("mode", ["resource_switch", "resource_stale_error"])
def test_source_changes_abort_old_retention_plan_and_old_errors_cannot_restore_it(retained, mode):
    old = task_center.snapshot()
    old["resources"][0]["audit"][0]["reason"] = "OLD-SOURCE-RETENTION"
    new = copy.deepcopy(old)
    new["resources"] = []
    new["sources"] = []
    # Rebuild the index for this new window; stale coverage must be refused.
    new["attention"] = task_attention.attention_view(new, [])
    assert new["attention"]["coverage"]["source_records"] == 0
    result = run_browser([old, new], mode, source="src-" + "b" * 24, old_marker="OLD-SOURCE-RETENTION")
    assert result["resources"] == "" and "OLD-SOURCE-RETENTION" not in result["all"]
    assert result["requests"][1]["aborted"] is True
    assert result["error"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
@pytest.mark.parametrize("mode", ["resource_refresh", "resource_missing", "resource_failure"])
def test_resource_refresh_clears_cached_plan_before_rendering_new_snapshot(retained, mode):
    old = task_center.snapshot()
    old["resources"][0]["audit"][0]["reason"] = "OLD-RETENTION-RECORD"
    if mode == "resource_refresh":
        store.protect(retained[2]["resource_id"], "operator", "pin next snapshot", 1)
        new = task_center.snapshot()
    else:
        new = copy.deepcopy(old)
        new["resources"] = []
    result = run_browser([old, new], mode)
    assert "OLD-RETENTION-RECORD" not in result["all"]
    assert len(result["requests"]) == 2 and all(item["method"] == "GET" for item in result["requests"])
    if mode == "resource_refresh":
        assert json.loads(result["detail"])["pinned"] is True
    else:
        assert "resources" not in result["detail"]
        assert result["resources"] == ""
