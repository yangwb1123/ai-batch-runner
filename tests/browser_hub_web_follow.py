"""Live versus fixed turns in Chrome/Hub; issuer/provider and history seed rows are fixtures."""

import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from browser_hub_web_history import (
    finish_prompt, seed_history, history_panel, history_rows, search_history, query_of,
)
from test_hub_auth import signed
from test_hub_web import web_env

FIRST_PROMPT = "first browser instruction for follow mode"
FIRST_REPLY = "first-only historical reply"
EXTERNAL_PROMPT = "external client starts a different real turn"
EXTERNAL_REPLY = "external-only completed reply"


def mode(page):
    return page.get_by_role("region", name="轮次查看模式", exact=True)


def browser_posts(page):
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    return writes


def initial_turn(task_page, name="follow-mode-task"):
    page, _, store, runtimes = task_page
    session = create_task(page, store, name)
    turn = finish_prompt(page, store, runtimes, session, FIRST_PROMPT, FIRST_REPLY)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(FIRST_REPLY)
    return session, turn


def external_start(task_page, session, prompt=EXTERNAL_PROMPT):
    """Separate authenticated HTTP client; this exercises the actual Hub/Gateway execution path."""
    page, context, store, runtimes = task_page
    address = urlsplit(page.url)
    origin = f"{address.scheme}://{address.netloc}"
    access = context.request.get(origin + "/web/api/access", timeout=5000, max_redirects=0)
    assert access.status == 200
    response = context.request.post(origin + f"/web/api/sessions/{session}/turns",
        headers={"Origin": origin, "Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4()),
                 "X-ABR-Context": access.json()["data"]["context"]},
        data={"prompt": prompt}, timeout=5000, max_redirects=0)
    assert response.status == 202
    turn = response.json()["data"]["turn_id"]
    until(lambda: prompt in runtimes[0].prompts and store.session("team", session)["active_turn_id"] == turn)
    return turn


def external_finish(task_page, session, prompt=EXTERNAL_PROMPT, reply=EXTERNAL_REPLY):
    turn = external_start(task_page, session, prompt)
    complete_turn(task_page[3][0], reply)
    until(lambda: task_page[2].turn("team", turn)["state"] == "completed")
    return turn


def fixed_link(page, session, turn):
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{turn}")
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}/turns/{re.escape(turn)}$"))
    expect(mode(page)).to_contain_text("固定查看此轮")


def live_link(page, session):
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}$"))
    expect(mode(page)).to_contain_text("跟随任务现场")


def screenshot_mode(page, tmp_path):
    destination = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    mode(page).scroll_into_view_if_needed()
    page.screenshot(path=str(destination / f"turn-follow-{page.viewport_size['width']}.png"))


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_task_route_follows_external_active_and_completed_turn_without_history_or_posts(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, first = initial_turn(task_page)
    draft = "keep this unsent scope while another client runs"
    page.get_by_label("发送下一条指令").fill(draft)
    writes = browser_posts(page)
    original_url, length = page.url, page.evaluate("history.length")
    second = external_start(task_page, session)
    runtimes[0].emit("message.delta", {"text": "external live progress marker"})
    conversation = page.get_by_label("任务对话", exact=True)
    expect(conversation).to_contain_text("external live progress marker")
    expect(conversation).to_contain_text(EXTERNAL_PROMPT)
    expect(conversation).not_to_contain_text(FIRST_REPLY)
    live_link(page, session)
    screenshot_mode(page, tmp_path)
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    page.get_by_role("tab", name="执行事件", exact=True).click()
    events = page.get_by_label("执行事件窗口", exact=True)
    expect(events).to_contain_text(second)
    expect(events).not_to_contain_text(first)
    page.get_by_role("tab", name="对话", exact=True).click()
    complete_turn(runtimes[0], EXTERNAL_REPLY)
    until(lambda: store.turn("team", second)["state"] == "completed")
    expect(conversation).to_contain_text(EXTERNAL_REPLY)
    expect(conversation).not_to_contain_text(FIRST_REPLY)
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    assert page.url == original_url and page.evaluate("history.length") == length
    assert writes == [] and runtimes[0].prompts == [FIRST_PROMPT, EXTERNAL_PROMPT]


def test_same_current_id_link_is_fixed_and_can_return_while_another_turn_runs(task_page):
    page, _, store, runtimes = task_page
    session, first = initial_turn(task_page)
    draft = "retained draft for this same session"
    page.get_by_label("发送下一条指令").fill(draft)
    writes = browser_posts(page)
    fixed_link(page, session, first)
    back = mode(page).get_by_role("button", name="回到任务现场", exact=True)
    expect(back).to_be_enabled()
    second = external_start(task_page, session)
    runtimes[0].emit("message.delta", {"text": "second turn is actually running"})
    expect(mode(page)).to_contain_text("任务现场已有另一轮")
    expect(mode(page)).to_contain_text("执行中")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(FIRST_REPLY)
    expect(page.get_by_label("任务对话", exact=True)).not_to_contain_text(EXTERNAL_PROMPT)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("button", name="刷新任务现场", exact=True)).to_be_enabled()
    expect(mode(page)).to_contain_text("固定查看此轮")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(FIRST_REPLY)
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    back.click()
    live_link(page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("second turn is actually running")
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    complete_turn(runtimes[0], EXTERNAL_REPLY)
    until(lambda: store.turn("team", second)["state"] == "completed")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_REPLY)
    assert writes == [] and runtimes[0].prompts == [FIRST_PROMPT, EXTERNAL_PROMPT]


def test_return_to_live_keeps_filtered_older_history_page_and_uses_unfiltered_latest(task_page):
    page, _, store, runtimes = task_page
    session, first = initial_turn(task_page)
    second = finish_prompt(page, store, runtimes, session, "second browser instruction", "second browser reply")
    seed_history(store, first, second, count=23)  # Read-only ledger fixtures, not 23 executed turns.
    panel = history_panel(page)
    search_history(page, panel, "History fixture")
    expect(history_rows(panel)).to_have_count(20)
    panel.get_by_role("button", name="查看更早轮次", exact=True).click()
    expect(history_rows(panel)).to_have_count(3)
    expected_rows = history_rows(panel).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))")
    page.get_by_label("发送下一条指令").fill("preserve draft and the older search page")
    fixed_link(page, session, first)
    writes = browser_posts(page)
    page.get_by_role("button", name="工作台", exact=True).click()
    latest = external_finish(task_page, session)
    assert store.session("team", session)["active_turn_id"] == ""
    page.get_by_role("button", name="Agent 任务", exact=True).click()
    expect(mode(page)).to_contain_text("固定查看此轮")
    expect(mode(page)).to_contain_text("任务现场已有另一轮")
    mode(page).get_by_role("button", name="回到任务现场", exact=True).click()
    live_link(page, session)
    page.get_by_role("tab", name="对话", exact=True).click()
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_REPLY)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(latest)
    expect(page.get_by_label("发送下一条指令")).to_have_value("preserve draft and the older search page")
    panel = history_panel(page)
    expect(panel.get_by_label("搜索轮次", exact=True)).to_have_value("History fixture")
    expect(panel).to_contain_text("第 2 页")
    expect(history_rows(panel)).to_have_count(3)
    assert history_rows(panel).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))") == expected_rows
    assert writes == [] and runtimes[0].prompts == [FIRST_PROMPT, "second browser instruction", EXTERNAL_PROMPT]


def test_back_forward_reload_and_successful_send_keep_the_intended_view_mode(task_page):
    page, _, store, runtimes = task_page
    session, first = initial_turn(task_page)
    second = finish_prompt(page, store, runtimes, session, "second browser instruction", "second browser reply")
    fixed_link(page, session, first)
    writes = browser_posts(page)
    page.go_back()
    live_link(page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("second browser reply")
    page.go_forward()
    expect(mode(page)).to_contain_text("固定查看此轮")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(FIRST_REPLY)
    page.reload()
    expect(mode(page)).to_contain_text("固定查看此轮")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(FIRST_REPLY)
    latest = finish_prompt(page, store, runtimes, session, "explicit send from fixed view", "new explicitly sent result")
    live_link(page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("new explicitly sent result")
    page.reload()
    live_link(page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(latest)
    expect(page.get_by_label("任务对话", exact=True)).not_to_contain_text(FIRST_REPLY)
    assert latest != second and len(writes) == 1 and len(runtimes[0].prompts) == 3


def test_read_only_user_can_leave_fixed_view_and_follow_without_write_permission(task_page, web_env):
    page, _, store, runtimes = task_page
    session, first = initial_turn(task_page)
    fixed_link(page, session, first)
    second = external_start(task_page, session)
    web_env[1].grants = [replace(grant, scopes=grant.scopes - {"agent.sessions:write", "agent.turns:cancel"})
                         if grant.kind == "user" else grant for grant in web_env[1].grants]
    writes = browser_posts(page)
    page.reload()
    expect(mode(page)).to_contain_text("固定查看此轮")
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    back = mode(page).get_by_role("button", name="回到任务现场", exact=True)
    expect(back).to_be_enabled()
    back.click()
    live_link(page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_PROMPT)
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    complete_turn(runtimes[0], EXTERNAL_REPLY)
    until(lambda: store.turn("team", second)["state"] == "completed")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_REPLY)
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    assert writes == [] and runtimes[0].prompts == [FIRST_PROMPT, EXTERNAL_PROMPT]


def test_unknown_submission_can_return_to_live_without_replacing_pending_body_or_key(task_page):
    page, context, store, runtimes = task_page
    session, first = initial_turn(task_page)
    fixed_link(page, session, first)
    sent = []

    def drop_first(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            assert route.fetch().status == 202
            route.abort("failed")
        else:
            route.continue_()

    context.route("**/web/api/sessions/" + session + "/turns", drop_first)
    original = "unknown acknowledgement must retain this exact instruction"
    second = send_prompt(page, store, runtimes, session, original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    complete_turn(runtimes[0], "unknown submission actually completed once")
    until(lambda: store.turn("team", second)["state"] == "completed")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(mode(page)).to_contain_text("固定查看此轮")
    mode(page).get_by_role("button", name="回到任务现场", exact=True).click()
    live_link(page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("unknown submission actually completed once")
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    assert len(sent) == 1
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    live_link(page, session)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts == [FIRST_PROMPT, original]


def wait_for_delayed_events(page):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if page.evaluate("window.followReadGate.ready"):
            return
        page.wait_for_timeout(20)
    assert page.evaluate("window.followReadGate.ready"), "Background event read did not reach the delivery gate"


def delay_old_event_delivery(page, session, turn):
    """Delay delivery of real HTTP bytes, including when the caller cancels its read."""
    page.add_init_script("""(() => {
      const original = window.fetch.bind(window);
      const gate = window.followReadGate = {enabled: false, ready: false, reads: 0, aborts: 0};
      window.fetch = async (input, init) => {
        const response = await original(input, init), url = new URL(String(input), location.href);
        if (gate.enabled && !gate.ready && url.pathname === `/web/api/sessions/${gate.session}/events`
            && url.searchParams.get('turn_id') === gate.turn) {
          const bytes = await response.arrayBuffer();
          const saved = new Response(bytes, {status: response.status, headers: response.headers});
          gate.ready = true; gate.reads++;
          init.signal.addEventListener('abort', () => gate.aborts++, {once: true});
          return new Promise(resolve => { gate.release = () => { gate.enabled = false; window.fetch = original; resolve(saved); }; });
        }
        return response;
      };
    })()""")
    page.reload()
    expect(mode(page)).to_contain_text("固定查看此轮")
    page.evaluate("target => Object.assign(window.followReadGate, target, {enabled: true})", {"session": session, "turn": turn})


def test_late_old_events_do_not_reappear_after_navigation_to_live(task_page):
    page, _, _, runtimes = task_page
    session, first = initial_turn(task_page)
    fixed_link(page, session, first)
    second = external_finish(task_page, session)
    expect(mode(page)).to_contain_text("任务现场已有另一轮")
    delay_old_event_delivery(page, session, first)
    writes = browser_posts(page)
    try:
        wait_for_delayed_events(page)
        back = mode(page).get_by_role("button", name="回到任务现场", exact=True)
        expect(back).to_be_enabled()
        back.click()
        live_link(page, session)
        expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_REPLY)
        assert page.evaluate("window.followReadGate.reads") == 1
        assert page.evaluate("window.followReadGate.aborts") == 1
    finally:
        page.evaluate("window.followReadGate.release?.()")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_REPLY)
    expect(page.get_by_label("任务对话", exact=True)).not_to_contain_text(FIRST_REPLY)
    page.get_by_role("tab", name="执行事件", exact=True).click()
    expect(page.get_by_label("执行事件窗口", exact=True)).to_contain_text(second)
    expect(page.get_by_label("执行事件窗口", exact=True)).not_to_contain_text(first)
    assert writes == [] and len(runtimes[0].prompts) == 2


def test_revocation_clears_followed_turn_and_unsent_draft(task_page, web_env):
    page, _, store, runtimes = task_page
    session, _ = initial_turn(task_page)
    second = external_start(task_page, session)
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(EXTERNAL_PROMPT)
    page.get_by_label("发送下一条指令").fill("private draft before follow revocation")
    writes = browser_posts(page)
    web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
    expect(mode(page)).to_have_count(0)
    expect(page.get_by_label("任务对话", exact=True)).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    assert EXTERNAL_PROMPT not in page.locator("body").inner_text()
    assert "private draft before follow revocation" not in page.locator("body").inner_text()
    complete_turn(runtimes[0], EXTERNAL_REPLY)
    until(lambda: store.turn("team", second)["state"] == "completed")
    assert writes == [] and runtimes[0].prompts == [FIRST_PROMPT, EXTERNAL_PROMPT]


def test_missing_fixed_turn_stays_missing_instead_of_falling_back_to_live(task_page):
    page, _, _, runtimes = task_page
    session, _ = initial_turn(task_page)
    writes = browser_posts(page)
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/missing-fixed-turn")
    expect(page.get_by_role("alert")).to_contain_text(re.compile("不存在|无效"))
    expect(mode(page)).to_have_count(0)
    expect(page.get_by_label("任务对话", exact=True)).to_have_count(0)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("button", name="刷新任务现场", exact=True)).to_be_enabled()
    expect(page.get_by_label("任务对话", exact=True)).to_have_count(0)
    assert FIRST_REPLY not in page.locator("body").inner_text()
    assert writes == [] and runtimes[0].prompts == [FIRST_PROMPT]
