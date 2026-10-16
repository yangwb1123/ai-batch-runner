"""Shared authenticated feedback metadata, never a case-body/upload/execution API."""
from __future__ import annotations

import copy
import json
import shutil
import urllib.error
from pathlib import Path

import pytest

from test_task_center_http import server, request
from test_task_feedback import source, feedback, drafting, input_file, case
from test_task_center_browser import run_browser
from pbatch import task_attention, task_center, task_feedback_store as store
from pbatch.task_feedback_view import feedback_views
from pbatch.ops import ops_main


def test_feedback_cli_http_and_snapshot_share_metadata_without_case_reads(server, feedback, capsys, monkeypatch):
    source, path, value = feedback
    expected = feedback_views([source])[0][0]
    original = store.read_bytes
    def metadata_only(file, *args, **kwargs):
        assert Path(file).suffix == ".json" and file != path
        return original(file, *args, **kwargs)
    monkeypatch.setattr(store, "read_bytes", metadata_only)
    monkeypatch.setattr(store, "_verify_case", lambda *args: pytest.fail("implicit verification"))
    with request(server, "/api/feedback?source=" + source["source_id"]) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert json.loads(response.read())["feedback"] == [expected]
    with request(server, "/api/feedback/" + value["feedback_id"]) as response:
        assert json.loads(response.read()) == expected
    with request(server, "/api/tasks") as response:
        assert json.loads(response.read())["feedback"] == [expected]
    assert ops_main(["feedback", "show", value["feedback_id"]]) == 0
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize("token", ["", "wrong"])
def test_feedback_requires_authentication(server, feedback, token):
    for route in ("/api/feedback", "/api/feedback/" + feedback[2]["feedback_id"]):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, route, token=token)
        assert exc.value.code == 401


def test_workspace_default_does_not_gain_feedback_access(server):
    server[1].global_tasks = False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/feedback")
    assert exc.value.code == 404


@pytest.mark.parametrize("key", ["../goals", "fd-" + "g" * 32, "not-an-id", "%2Fetc%2Fpasswd"])
def test_feedback_details_require_typed_keys_never_paths(server, key):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/feedback/" + key)
    assert exc.value.code == 400


@pytest.mark.parametrize("action", ["draft", "withdraw", "verify-copy", "upload", "execute", "reproduce", "promote", "release", "upgrade", "delete", "force"])
def test_no_remote_feedback_mutation_reproduction_release_or_upload(server, feedback, action):
    value = feedback[2]
    paths = [feedback[1], store.feedback_path(value["feedback_id"]), store.feedback_root() / (value["feedback_id"] + ".case")]
    before = [path.read_bytes() for path in paths]
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/feedback/" + value["feedback_id"] + "/" + action, method="POST")
    assert exc.value.code == 403
    assert [path.read_bytes() for path in paths] == before


def test_http_query_controls_cannot_import_verify_upload_or_download_case(server, feedback, monkeypatch):
    key = feedback[2]["feedback_id"]
    monkeypatch.setattr(store, "draft", lambda *args, **kwargs: pytest.fail("remote case import"))
    from pbatch import task_feedback_integrity
    monkeypatch.setattr(task_feedback_integrity, "verify_copy", lambda *args, **kwargs: pytest.fail("remote copy verification"))
    with request(server, "/api/feedback?source=/etc&file=/etc/passwd&draft=true&confirm=true") as response:
        assert json.loads(response.read())["feedback"] == []
    with request(server, "/api/feedback/" + key + "?upload=true&reproduce=true&verify-copy=true&confirm=true&file=/etc/passwd") as response:
        row = json.loads(response.read())
        assert row["capabilities"]["feedback_upload"] is False
        assert row["copy"]["current_copy_reverified"] is False
    for suffix in ("case", "verify-copy"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, "/api/feedback/" + key + "/" + suffix)
        assert exc.value.code == 400


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
def test_feedback_web_quotes_text_and_never_renders_private_case_or_control_buttons(feedback):
    view = task_center.snapshot()
    row = view["feedback"][0]
    marker = '<img src=x onerror="alert(1)"><script>alert(1)</script>'
    row["summary"] = marker
    row["audit"][0]["reason"] = marker
    result = run_browser([view], "feedback")
    assert marker in result["feedback"] and result["tags"] == 0
    assert "未授执行/上传权" in result["feedback"] and "独立复现未完成" in result["feedback"]
    assert "PRIVATE-CASE-BODY" not in result["all"] and str(feedback[1]) not in result["all"]
    assert len(result["requests"]) == 1 and result["requests"][0]["method"] == "GET"


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
@pytest.mark.parametrize("mode", ["feedback_refresh", "feedback_missing", "feedback_failure"])
def test_feedback_refresh_withdrawal_missing_or_failure_cannot_retain_old_summary(feedback, mode):
    old = task_center.snapshot()
    old["feedback"][0]["summary"] = "OLD-PRIVATE-FEEDBACK-SUMMARY"
    if mode == "feedback_refresh":
        store.withdraw(feedback[2]["feedback_id"], "operator", "withdraw before next poll", 1)
        new = task_center.snapshot()
    else:
        new = copy.deepcopy(old); new["feedback"] = []
    result = run_browser([old, new], mode)
    assert "OLD-PRIVATE-FEEDBACK-SUMMARY" not in result["all"]
    assert len(result["requests"]) == 2
    if mode == "feedback_refresh":
        row = json.loads(result["detail"])
        assert row["state"] == "withdrawn" and row["summary"] is None
    else:
        assert result["feedback"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
@pytest.mark.parametrize("status", ["withdrawn", "stale_source_definition", "unrecognized_state"])
def test_inconsistent_client_status_still_suppresses_old_feedback_summary(feedback, status):
    view = task_center.snapshot()
    view["feedback"][0].update(status=status, summary="DO-NOT-RENDER-OLD-FEEDBACK")
    result = run_browser([view], "feedback")
    assert "DO-NOT-RENDER-OLD-FEEDBACK" not in result["all"]
    assert json.loads(result["detail"])["summary"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
@pytest.mark.parametrize("mode", ["feedback_switch", "feedback_stale_error"])
def test_source_switch_aborts_old_feedback_and_late_response_or_error_cannot_restore_it(feedback, mode):
    old = task_center.snapshot()
    old["feedback"][0]["summary"] = "OLD-SOURCE-FEEDBACK"
    new = copy.deepcopy(old); new["feedback"] = []; new["sources"] = []
    # A new empty source window must not carry the previous source's attention counts.
    new["attention"] = task_attention.attention_view(new, [])
    assert new["attention"]["coverage"]["source_records"] == 0
    result = run_browser([old, new], mode, source="src-" + "b" * 24, old_marker="OLD-SOURCE-FEEDBACK")
    assert result["feedback"] == "" and "OLD-SOURCE-FEEDBACK" not in result["all"]
    assert result["requests"][1]["aborted"] is True and result["error"] == ""


def test_duplicate_candidate_refs_are_shared_by_http_cli_and_task_snapshot(server, source, capsys):
    first = drafting(source, input_file(source, case("one"), "one.json"))
    second = drafting(source, input_file(source, case("two"), "two.json"))
    expected = next(row for row in feedback_views([source])[0] if row["feedback_id"] == first["feedback_id"])
    assert expected["duplicate_inspection"]["matching_feedback_ids"] == [second["feedback_id"]]
    with request(server, "/api/feedback/" + first["feedback_id"] + "?source=" + source["source_id"]) as response:
        assert json.loads(response.read()) == expected
    assert ops_main(["feedback", "show", first["feedback_id"], "--source", source["source_id"]]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    view = task_center.snapshot(source_id=source["source_id"], limit=1)
    assert next(row for row in view["feedback"] if row["feedback_id"] == first["feedback_id"]) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
def test_feedback_web_displays_candidate_count_not_duplicate_incident_verdict(source):
    drafting(source, input_file(source, case("one")))
    drafting(source, input_file(source, case("two")))
    result = run_browser([task_center.snapshot()], "feedback")
    assert "内容元数据匹配候选 1" in result["feedback"]
    assert "非同根因、非完整去重；副本未复核" in result["feedback"]
    assert json.loads(result["detail"])["duplicate_inspection"]["current_copies_reverified"] is False
    assert len(result["requests"]) == 1 and result["requests"][0]["method"] == "GET"


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
@pytest.mark.parametrize("field,value", [("status", "withdrawn"), ("status", "stale_source_definition"), ("state", "withdrawn")])
def test_inactive_or_inconsistent_client_state_cannot_display_old_match_associations(feedback, field, value):
    view = task_center.snapshot()
    row = view["feedback"][0]
    row.update(summary="OLD-FEEDBACK-SUMMARY", duplicate_inspection={"observed_matches": 1,
        "matching_feedback_ids": ["OLD-PEER-ASSOCIATION"], "selection": "matching_metadata_candidates"})
    row[field] = value
    result = run_browser([view], "feedback")
    assert "OLD-PEER-ASSOCIATION" not in result["all"] and "OLD-FEEDBACK-SUMMARY" not in result["all"]
    assert json.loads(result["detail"])["duplicate_inspection"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
def test_selecting_other_record_clears_feedback_detail_selection(server, feedback):
    # Existing synthetic task from the HTTP fixture may belong to a different runtime home.
    view = task_center.snapshot()
    if not view["tasks"]:
        from test_task_repair_preview import _checkpoint, _publish
        _publish(feedback[0], _checkpoint(feedback[0]))
        view = task_center.snapshot()
    result = run_browser([view, view], "feedback_select_other")
    assert "task_id" in result["detail"] and "feedback_id" not in result["detail"]
