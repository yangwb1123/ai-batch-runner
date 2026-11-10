"""Real Chrome workbench acceptance; real Hub/Gateway, synthetic identity/provider."""

import re

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from test_hub_auth import signed
from test_hub_web import web_env


def conversation(page):
    return page.get_by_label("任务对话", exact=True)


def confirm_operation(page, name):
    summary = page.locator("summary").filter(has_text="任务操作与连接信息")
    if summary.locator("..").get_attribute("open") is None:
        summary.click()
    page.get_by_label("我确认操作当前会话；取消或关闭不会回滚已发生的代码改动。", exact=True).check()
    page.get_by_role("button", name=name, exact=True).click()


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_task_workflow_through_actual_gateway(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store)
    first = send_prompt(page, store, runtimes, session, "inspect synthetic project")
    runtime = runtimes[0]
    runtime.emit("message.delta", {"text": "working preview"})
    expect(conversation(page)).to_contain_text("working preview")
    output = '<img src=x onerror="window.compromised=1"> verified fixture output'
    complete_turn(runtime, output)
    until(lambda: store.turn("team", first)["state"] == "completed")
    expect(conversation(page)).to_contain_text(output)
    expect(conversation(page)).not_to_contain_text("working preview")
    assert conversation(page).locator("img, script").count() == 0
    assert conversation(page).inner_text().count("verified fixture output") == 1
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    second = send_prompt(page, store, runtimes, session, "continue with the second instruction", keyboard=True)
    complete_turn(runtime, "second fixture result")
    until(lambda: store.turn("team", second)["state"] == "completed")
    expect(conversation(page)).to_contain_text("second fixture result")
    assert runtime.prompts == ["inspect synthetic project", "continue with the second instruction"]
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    expect(page.get_by_role("list", name="轮次记录", exact=True)).to_contain_text("inspect synthetic project")
    expect(page.get_by_role("list", name="轮次记录", exact=True)).to_contain_text("continue with the second instruction")
    page.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    page.get_by_role("tab", name="对话", exact=True).click()
    expect(conversation(page)).to_contain_text(output)
    expect(conversation(page)).not_to_contain_text("second fixture result")
    confirm_operation(page, "关闭空闲会话")
    until(lambda: store.session("team", session)["status"] == "closed")
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()


def test_failed_turn_can_continue_and_cancellation_waits_for_gateway(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "failure-recovery")
    failed = send_prompt(page, store, runtimes, session, "first instruction fails")
    runtime = runtimes[0]
    runtime.emit("turn.failed", {"reason": "synthetic provider refusal"})
    until(lambda: store.turn("team", failed)["state"] == "failed")
    expect(conversation(page)).to_contain_text("synthetic provider refusal")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    cancelled = send_prompt(page, store, runtimes, session, "second instruction will be cancelled")
    confirm_operation(page, "请求取消活动轮次")
    until(lambda: store.turn("team", cancelled)["state"] == "interrupted")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    recovered = send_prompt(page, store, runtimes, session, "third instruction continues explicitly")
    complete_turn(runtime, "recovered fixture result")
    until(lambda: store.turn("team", recovered)["state"] == "completed")
    expect(conversation(page)).to_contain_text("recovered fixture result")
    assert runtime.prompts == ["first instruction fails", "second instruction will be cancelled",
                               "third instruction continues explicitly"]


def test_live_reads_preserve_draft_and_refresh_restores_selected_task(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "draft-task")
    draft = "unsent instruction must survive reads"
    page.get_by_label("发送下一条指令").fill(draft)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    page.get_by_role("tab", name="对话", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    page.get_by_role("button", name="工作台", exact=True).click()
    page.get_by_role("button", name="Agent 任务", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    assert runtimes[0].prompts == []
    page.reload()
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    expect(page.get_by_label("发送下一条指令")).to_have_value("")
    assert runtimes[0].prompts == []  # In-memory drafts are not sent or persisted by reloading.


def test_back_forward_selection_and_memory_drafts_never_cross_sessions(task_page):
    page, _, store, runtimes = task_page
    first = create_task(page, store, "first-draft-task")
    page.get_by_label("发送下一条指令").fill("first private draft")
    second = create_task(page, store, "second-draft-task")
    page.get_by_label("发送下一条指令").fill("second private draft")
    page.go_back()
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(first) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_have_value("first private draft")
    page.go_forward()
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(second) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_have_value("second private draft")
    assert all(runtime.prompts == [] for runtime in runtimes)
    page.get_by_role("button", name="退出本地 abr 会话").click()
    expect(page.get_by_label("任务对话", exact=True)).to_have_count(0)
    assert "private draft" not in page.locator("body").inner_text()


def test_many_events_stream_without_manual_paging_or_duplicate_completed_text(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "streaming-task")
    turn = send_prompt(page, store, runtimes, session, "produce a multi-page reply")
    runtime = runtimes[0]
    parts = [f"part-{index:03d} " for index in range(65)]
    for part in parts:
        runtime.emit("message.delta", {"text": part})
    text = "".join(parts) + "界" * 6000 + "final marker"
    complete_turn(runtime, text)
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(conversation(page)).to_contain_text("final marker")
    rendered = conversation(page).inner_text()
    assert rendered.count("part-000") == 1
    assert rendered.count("part-064") == 1
    assert rendered.count("final marker") == 1
    assert rendered.count("界") == 6000
    page.get_by_role("tab", name="执行事件", exact=True).click()
    expect(page.get_by_label("执行事件窗口", exact=True)).to_contain_text("turn.completed")


def test_lost_write_response_reconciles_same_key_without_duplicate_execution(task_page):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "unknown-submit")
    sent = []

    def drop_first_response(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            response = route.fetch()
            assert response.status == 202
            route.abort("failed")
        else:
            route.continue_()

    pattern = "**/web/api/sessions/" + session + "/turns"
    context.route(pattern, drop_first_response)
    send_prompt(page, store, runtimes, session, "execute this instruction exactly once")
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts == ["execute this instruction exactly once"]
    complete_turn(runtimes[0], "once-only fixture result")
    expect(conversation(page)).to_contain_text("once-only fixture result")
    context.unroute(pattern, drop_first_response)


def test_invalid_new_task_does_not_inherit_previous_receipt(task_page):
    page, _, store, _ = task_page
    create_task(page, store, "valid-first-task")
    page.get_by_role("button", name="新建任务", exact=True).click()
    page.get_by_label("目标实例 / 固定项目").select_option("alpha")
    page.get_by_label("任务名称", exact=True).fill("字" * 100)
    page.get_by_role("button", name="创建任务", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("校验")
    expect(page.get_by_label("任务名称", exact=True)).to_have_value("字" * 100)
    assert len(store.session_operations("team")["data"]) == 1
    assert len(store.sessions("team", "alpha")) == 1


def test_read_permission_loss_clears_reply_draft_and_late_polling(task_page):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "revoked-access")
    turn = send_prompt(page, store, runtimes, session, "read this authorized reply")
    complete_turn(runtimes[0], "sensitive fixture answer")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(conversation(page)).to_contain_text("sensitive fixture answer")
    page.get_by_label("发送下一条指令").fill("sensitive unsent draft")
    pattern = "**/web/api/**"
    context.route(pattern, lambda route: route.fulfill(status=403, content_type="application/json", body='{"error":{"code":"forbidden"}}'))
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("权限")
    assert "sensitive fixture answer" not in page.locator("body").inner_text()
    assert page.get_by_label("发送下一条指令").count() == 0
    context.unroute(pattern)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    page.get_by_role("button", name="revoked-access", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value("")


def test_bfcache_restoration_reauthorizes_and_never_restores_unsent_content(task_page):
    page, _, store, runtimes = task_page
    create_task(page, store, "bfcache-task")
    page.get_by_label("发送下一条指令").fill("discard on pagehide")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    expect(page.get_by_label("Agent 任务列表", exact=True)).not_to_contain_text("bfcache-task")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    expect(page.get_by_label("Agent 任务列表", exact=True)).to_contain_text("bfcache-task")
    expect(page.get_by_label("发送下一条指令")).to_have_value("")
    assert runtimes[0].prompts == []


def test_invalid_task_link_clears_selected_content_without_unhandled_error(task_page):
    page, _, store, runtimes = task_page
    create_task(page, store, "valid-before-bad-link")
    page.evaluate("location.hash = 'tasks/%2E%2E%2Fbad'")
    expect(page.get_by_role("alert")).to_contain_text("任务链接无效")
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    assert runtimes[0].prompts == []


def test_cancelled_leave_keeps_account_controls_and_unsent_draft(task_page):
    page, _, store, runtimes = task_page
    create_task(page, store, "cancelled-navigation")
    page.get_by_label("发送下一条指令").fill("keep this unsent draft")
    page.remove_listener("dialog", page._abr_dialog_handler)
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="退出本地 abr 会话").click()
    expect(page.get_by_role("button", name="退出本地 abr 会话")).to_be_enabled()
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value("keep this unsent draft")
    assert runtimes[0].prompts == []
