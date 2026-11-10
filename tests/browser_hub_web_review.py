"""Result review in Chrome/Hub; issuer, provider and emitted command records are fixtures."""

import os
import re
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import (
    controlled_hub, task_page, create_task, send_prompt, complete_turn, until,
)
from browser_hub_web_results import results
from test_hub_auth import signed
from test_hub_web import web_env

pytestmark = pytest.mark.parametrize("controlled_hub", [True], indirect=True)

FAILED_COMMAND = "python -m pytest tests/test_review_failure.py"
OUTPUT_NEEDLE = "review-output-needle"
PRIVATE_VALUE = "SYNTHETIC-REVIEW-SECRET"
REPORTED_OUTPUT = (OUTPUT_NEEDLE + " assertion did not match\n"
                   "<script>window.reviewScriptExecuted = true</script>\n"
                   "<img src=https://probe.invalid/review onerror=window.reviewScriptExecuted=true>\n"
                   "password=" + PRIVATE_VALUE)


def emit_review_records(runtime, successes=65):
    """Typed provider reports, not a claim that the fixture ran these OS commands."""
    for index in range(successes):
        runtime.emit("command.completed", {"item_id": f"review-ok-{index:03d}",
            "command": f"reported-success-command-{index:03d}", "exit_code": 0})
    runtime.emit("command.completed", {"item_id": "review-failed", "command": FAILED_COMMAND,
        "exit_code": 7, "aggregated_output": REPORTED_OUTPUT})
    runtime.emit("command.completed", {"item_id": "review-unknown", "command": "unknown-review-command"})
    runtime.emit("validation.completed", {"item_id": "review-validation", "name": "fixture validation",
        "success": True, "validation_exit": 0})
    runtime.emit("gate.verdict", {"item_id": "review-gate", "gate": "fixture review gate",
        "verdict": "REJECT", "reason": "fixture acceptance evidence is missing"})


def reviewed_turn(task_page, successes=65):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "review-reported-results")
    turn = send_prompt(page, store, runtimes, session, "produce reviewable execution records")
    emit_review_records(runtimes[0], successes)
    complete_turn(runtimes[0], "All tests passed. VERDICT: PASS is still only model prose.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    results(page)
    workspace = page.get_by_role("region", name="结果记录工作区", exact=True)
    expect(workspace).to_be_visible()
    return session, turn, workspace


def records(workspace):
    return workspace.get_by_role("list", name="结果记录", exact=True).get_by_role("button")


def search_review(workspace, query="", state="attention", keyboard=False):
    field = workspace.get_by_label("搜索结果记录", exact=True)
    field.fill(query)
    workspace.get_by_label("结果状态筛选", exact=True).select_option(state)
    if keyboard:
        field.press("Enter")
    else:
        workspace.get_by_role("button", name="搜索结果记录", exact=True).click()


def select_failure(page, workspace):
    search_review(workspace, OUTPUT_NEEDLE)
    expect(records(workspace)).to_have_count(1)
    records(workspace).filter(has_text=FAILED_COMMAND).click()
    selected = page.get_by_role("region", name="选中结果记录", exact=True)
    expect(selected).to_contain_text(FAILED_COMMAND)
    return selected


def start_preview(page, selected):
    expect(selected).to_be_visible()
    page.get_by_role("button", name="起草后续指令", exact=True).click()
    preview = page.get_by_label("后续指令草稿", exact=True)
    expect(preview).to_be_visible()
    return preview


def screenshot_review(page, tmp_path):
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    page.get_by_label("后续指令草稿", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(target / f"review-workspace-{page.viewport_size['width']}.png"), full_page=True)


def screenshot_review_overview(page, workspace, tmp_path):
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    workspace.get_by_label("搜索结果记录", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(target / f"review-overview-{page.viewport_size['width']}.png"))


def submit_prepared_draft(page, store, runtimes, session, text):
    expect(page.get_by_label("发送下一条指令")).to_have_value(text)
    page.get_by_role("button", name="发送指令", exact=True).click()
    until(lambda: runtimes[0].prompts[-1] == text and bool(store.session("team", session)["active_turn_id"]))
    turn = store.session("team", session)["active_turn_id"]
    complete_turn(runtimes[0], "explicitly requested follow-up result")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("explicitly requested follow-up result")


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_search_late_result_edit_reference_append_draft_then_explicitly_send_once(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, turn, workspace = reviewed_turn(task_page)
    existing = "retain my original scope and acceptance requirements"
    page.get_by_label("发送下一条指令").fill(existing)
    writes, remote_content = [], []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.on("request", lambda request: remote_content.append(request.url) if "probe.invalid" in request.url else None)
    expect(records(workspace)).to_have_count(3)
    screenshot_review_overview(page, workspace, tmp_path)
    expected_count = records(workspace).count()
    workspace.get_by_label("搜索结果记录", exact=True).fill(OUTPUT_NEEDLE)
    page.wait_for_timeout(200)
    expect(records(workspace)).to_have_count(expected_count)
    selected = select_failure(page, workspace)
    selected.locator("summary").filter(has_text="查看命令输出").click()
    expect(selected).to_contain_text(OUTPUT_NEEDLE)
    output = selected.get_by_label("命令输出正文", exact=True)
    assert output.evaluate("node => node.scrollWidth <= node.clientWidth"), "Command output should wrap inside the result detail"
    preview = start_preview(page, selected)
    original = preview.input_value()
    event = next(row for row in store.events("team", session, limit=100, turn_id=turn)
                 if row["payload"].get("text") == FAILED_COMMAND)
    for value in [session, turn, event["event_id"], '"event_id"', '"cursor"', OUTPUT_NEEDLE]:
        assert value in original
    assert PRIVATE_VALUE not in original and "[REDACTED]" in original
    assert len(original.encode("utf-8")) <= 8192
    assert selected.locator("script, img, iframe").count() == 0
    assert page.evaluate("window.reviewScriptExecuted === undefined") and remote_content == []
    edited = "人工补充：仅核对该失败，不扩大执行范围。\n" + original
    preview.fill(edited)
    latest_draft = existing + "\nKeep this newer manual edit too."
    page.get_by_label("发送下一条指令").fill(latest_draft)
    screenshot_review(page, tmp_path)
    page.get_by_role("button", name="追加到当前草稿", exact=True).evaluate("button => { button.click(); button.click() }")
    combined = latest_draft + "\n\n" + edited
    expect(page.get_by_label("发送下一条指令")).to_have_value(combined)
    expect(page.get_by_label("发送下一条指令")).to_be_focused()
    expect(page.get_by_role("tab", name="对话", exact=True)).to_have_attribute("aria-selected", "true")
    assert writes == [] and len(runtimes[0].prompts) == 1
    submit_prepared_draft(page, store, runtimes, session, combined)
    assert len(writes) == 1 and runtimes[0].prompts == ["produce reviewable execution records", combined]


def test_all_loaded_results_paginate_and_status_filters_do_not_claim_acceptance(task_page):
    page, _, _, runtimes = task_page
    _, _, workspace = reviewed_turn(task_page)
    search_review(workspace, state="all")
    expect(records(workspace)).to_have_count(20)
    first = records(workspace).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))")
    workspace.get_by_role("button", name="下一页结果", exact=True).click()
    expect(records(workspace)).to_have_count(20)
    second = records(workspace).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))")
    assert not set(first).intersection(second)
    workspace.get_by_role("button", name="上一页结果", exact=True).click()
    assert records(workspace).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))") == first
    search_review(workspace, state="failure")
    expect(records(workspace)).to_have_count(2)
    expect(records(workspace).filter(has_text=FAILED_COMMAND)).to_be_visible()
    search_review(workspace, state="unknown")
    expect(records(workspace)).to_have_count(1)
    expect(workspace).to_contain_text("unknown-review-command")
    search_review(workspace, "reported-success-command-064", state="success", keyboard=True)
    expect(records(workspace)).to_have_count(1)
    search_review(workspace, "absent-result-marker", state="all")
    expect(records(workspace)).to_have_count(0)
    expect(page.get_by_role("tabpanel", name="结果检查", exact=True)).to_contain_text("未独立验证")
    assert len(runtimes[0].prompts) == 1


def test_reference_append_budget_rejects_without_truncating_either_draft(task_page):
    page, _, _, runtimes = task_page
    _, _, workspace = reviewed_turn(task_page, successes=0)
    original = "字" * 5461
    page.get_by_label("发送下一条指令").fill(original)
    preview = start_preview(page, select_failure(page, workspace))
    edited = "请先核对这一条明确的问题。"
    preview.fill(edited)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.get_by_role("button", name="追加到当前草稿", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="16384")).to_contain_text("草稿")
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    expect(preview).to_have_value(edited)
    assert writes == [] and len(runtimes[0].prompts) == 1


def test_switching_turn_clears_reference_preview_but_preserves_session_draft(task_page):
    page, _, store, runtimes = task_page
    session, first, _ = reviewed_turn(task_page, successes=0)
    second = send_prompt(page, store, runtimes, session, "new selected round")
    complete_turn(runtimes[0], "new selected result")
    until(lambda: store.turn("team", second)["state"] == "completed")
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    page.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    results(page)
    workspace = page.get_by_role("region", name="结果记录工作区", exact=True)
    preview = start_preview(page, select_failure(page, workspace))
    preview.fill("old unappended private reference")
    page.get_by_label("发送下一条指令").fill("keep existing session draft")
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{second}")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("new selected result")
    expect(page.get_by_label("后续指令草稿", exact=True)).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_value("keep existing session draft")
    results(page)
    assert "old unappended private reference" not in page.locator("body").inner_text()
    assert len(runtimes[0].prompts) == 2


def test_revocation_clears_result_search_selection_preview_and_main_draft(task_page):
    page, context, _, runtimes = task_page
    _, _, workspace = reviewed_turn(task_page, successes=0)
    preview = start_preview(page, select_failure(page, workspace))
    preview.fill("private unappended reference")
    page.get_by_label("发送下一条指令").fill("private unsent draft")
    context.route("**/web/api/**", lambda route: route.fulfill(status=403, content_type="application/json",
        body='{"error":{"code":"forbidden"}}'))
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("权限")
    expect(page.get_by_label("搜索结果记录", exact=True)).to_have_count(0)
    expect(page.get_by_label("后续指令草稿", exact=True)).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    assert "private unappended reference" not in page.locator("body").inner_text()
    assert "private unsent draft" not in page.locator("body").inner_text()
    assert len(runtimes[0].prompts) == 1


def test_unknown_submission_blocks_reference_drafts_until_original_key_is_reconciled(task_page):
    page, context, store, runtimes = task_page
    session, first, workspace = reviewed_turn(task_page, successes=0)
    start_preview(page, select_failure(page, workspace))
    sent = []

    def drop_first(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            assert route.fetch().status == 202
            route.abort("failed")
        else:
            route.continue_()

    pattern = "**/web/api/sessions/" + session + "/turns"
    context.route(pattern, drop_first)
    turn = send_prompt(page, store, runtimes, session, "submitted instruction must not be replaced")
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    complete_turn(runtimes[0], "unknown response nevertheless executed once")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{first}")
    expect(page.get_by_role("region", name="轮次查看模式", exact=True)).to_contain_text("固定查看此轮")
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    results(page)
    workspace = page.get_by_role("region", name="结果记录工作区", exact=True)
    selected = select_failure(page, workspace)
    expect(page.get_by_role("button", name="起草后续指令", exact=True)).to_be_disabled()
    expect(page.get_by_label("后续指令草稿", exact=True)).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_value("submitted instruction must not be replaced")
    assert len(sent) == 1 and len(runtimes[0].prompts) == 2
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts.count("submitted instruction must not be replaced") == 1


def test_partial_shared_output_stays_reviewable_without_promoting_exit_zero_to_acceptance(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "partial-reported-results")
    turn = send_prompt(page, store, runtimes, session, "report a truncated output window")
    runtimes[0].emit("command.completed", {"item_id": "review-partial", "command": "partial-output-command",
        "exit_code": 0, "aggregated_output": "only part of reported output", "truncated": True})
    complete_turn(runtimes[0], "All work passed according to this unverified prose.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    panel = results(page)
    workspace = page.get_by_role("region", name="结果记录工作区", exact=True)
    expect(workspace.get_by_label("结果状态筛选", exact=True)).to_have_value("attention")
    records(workspace).filter(has_text="partial-output-command").click()
    selected = page.get_by_role("region", name="选中结果记录", exact=True)
    expect(selected).to_contain_text("不完整")
    preview = start_preview(page, selected)
    assert '"partial": true' in preview.input_value()
    assert '"exit_code": 0' in preview.input_value()
    expect(panel).to_contain_text("未独立验证")
    assert len(runtimes[0].prompts) == 1


def test_combined_command_reference_keeps_actual_event_identities_and_kinds(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "combined-result-provenance")
    turn = send_prompt(page, store, runtimes, session, "report one command across several events")
    runtime = runtimes[0]
    runtime.emit("command.started", {"item_id": "combined", "command": "pytest combined"})
    runtime.emit("command.completed", {"item_id": "combined", "exit_code": 7})
    runtime.emit("command.output", {"item_id": "combined", "aggregated_output": "separate final output"})
    complete_turn(runtime, "combined command report is ready for inspection")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    results(page)
    page.get_by_role("list", name="结果记录", exact=True).get_by_role("button").filter(has_text="pytest combined").click()
    selected = page.get_by_role("region", name="选中结果记录", exact=True)
    preview = start_preview(page, selected)
    text = preview.input_value()
    quote, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    assert quote["source"]["projection"] == "grouped_reported_events"
    actual = {row["event_id"]: row for row in store.events("team", session, limit=100, turn_id=turn)
              if row["kind"].startswith("command.")}
    quoted = quote["source"]["events"]
    assert len(quoted) == len(actual) == 3
    for item in quoted:
        assert item["kind"] == actual[item["event_id"]]["kind"]
        assert item["cursor"] == actual[item["event_id"]]["cursor"]
        assert item.get("exit_code") == actual[item["event_id"]]["payload"].get("exit_code")
    output = next(item for item in quoted if item["kind"] == "command.output")
    assert "exit_code" not in output


def test_preview_survives_same_facts_refresh_but_expires_when_command_evidence_changes(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "changing-result-reference")
    turn = send_prompt(page, store, runtimes, session, "keep processing while evidence changes")
    runtime = runtimes[0]
    runtime.emit("command.completed", {"item_id": "changing", "command": "inspect changing output", "exit_code": 7})
    results(page)
    page.get_by_role("list", name="结果记录", exact=True).get_by_role("button").filter(has_text="inspect changing output").click()
    selected = page.get_by_role("region", name="选中结果记录", exact=True)
    preview = start_preview(page, selected)
    preview.fill("retain this edited reference while facts stay the same")
    page.get_by_label("发送下一条指令").fill("preserve the original main draft")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("button", name="刷新任务现场", exact=True)).to_be_enabled()
    expect(preview).to_have_value("retain this edited reference while facts stay the same")
    runtime.emit("command.output", {"item_id": "changing", "aggregated_output": "new evidence now changes the reference"})
    expect(preview).to_have_count(0)
    expect(page.get_by_role("alert").filter(has_text="引用依据已变化")).to_be_visible()
    expect(page.get_by_label("发送下一条指令")).to_have_value("preserve the original main draft")
    complete_turn(runtime, "processing finished")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    assert len(runtimes[0].prompts) == 1
