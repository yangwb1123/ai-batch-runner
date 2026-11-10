"""Prompt reuse in real Chrome/TLS/Hub; issuer/provider and altered read facts are fixtures."""

import json
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from browser_hub_web_control import confirm_operation
from browser_hub_web_history import finish_prompt
from browser_hub_web_follow import browser_posts, fixed_link, external_start
from browser_hub_web_find import install_request_probe
from test_hub_auth import signed
from test_hub_web import web_env

PROMPT = ("目标：精确复用中文 Δé 😀 [a+b].*，保留范围与验收。\n"
          "<script>window.reuseExecuted = true</script>\n"
          "<img src=https://probe.invalid/reuse onerror=window.reuseExecuted=true>\n"
          "password=SYNTHETIC-REUSE-SECRET\n最后一行：不自动执行。")
DISPLAY = PROMPT.replace("SYNTHETIC-REUSE-SECRET", "[REDACTED]")


def conversation(page):
    return page.get_by_label("任务对话", exact=True)


def preview(page):
    return page.get_by_role("region", name="复用指令预览", exact=True)


def editor(page):
    return page.get_by_label("发送下一条指令", exact=True)


def reuse_button(page):
    return page.get_by_role("button", name="复用本轮指令", exact=True)


def local_click(page, button, twice=False):
    expect(button).to_be_enabled()
    before = page.url
    calls = button.evaluate("""async (button, twice) => {
      const probe = window.findRequestProbe;
      probe.calls = []; probe.active = true;
      try { button.click(); if (twice) button.click(); await Promise.resolve(); await Promise.resolve(); }
      finally { probe.active = false; }
      return probe.calls;
    }""", twice)
    assert calls == [] and page.url == before
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")


def start_reuse(page):
    local_click(page, reuse_button(page))
    expect(preview(page)).to_be_visible()
    field = preview(page).get_by_role("textbox", name="待加入的指令", exact=True)
    expect(field).to_be_visible()
    return field


def apply_reuse(page, twice=False):
    local_click(page, preview(page).get_by_role("button", name="加入当前草稿", exact=True), twice)


def refresh(page):
    button = page.get_by_role("button", name="刷新任务现场", exact=True)
    expect(button).to_be_enabled()
    button.click()
    expect(button).to_be_enabled()


def prepared_task(task_page, prompt="original reusable instruction", name="prompt-reuse"):
    page, _, store, runtimes = task_page
    install_request_probe(page)
    session = create_task(page, store, name)
    turn = finish_prompt(page, store, runtimes, session, prompt, "original result")
    expect(conversation(page).locator(".turn-context .session-status")).to_have_attribute("data-state", "completed")
    return session, turn


def change_read_facts(page, identifier, fields):
    """Alter selected fields in real authenticated read responses, not request/authorization logic."""
    def change(route):
        path = urlsplit(route.request.url).path
        if route.request.method != "GET" or not ("/turns" in path or path.endswith("/sessions/" + identifier)):
            route.continue_()
            return
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        body = response.json()
        rows = body["data"] if isinstance(body.get("data"), list) else [body.get("data")]
        for row in rows:
            if isinstance(row, dict) and (row.get("turn_id") == identifier or row.get("session_id") == identifier):
                row.update(fields)
        route.fulfill(response=response, json=body)
    page.route("**/web/api/**", change)
    return change


def screenshot_reuse(page, tmp_path):
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    preview(page).scroll_into_view_if_needed()
    page.screenshot(path=str(target / f"prompt-reuse-{page.viewport_size['width']}.png"), full_page=True)


def reuse_geometry(field, tmp_path, phase):
    geometry = field.evaluate("""field => {
      const panel = field.closest('.task-conversation');
      const bounds = panel.getBoundingClientRect(), rect = field.getBoundingClientRect();
      const top = Math.max(bounds.top, 0), bottom = Math.min(bounds.bottom, window.innerHeight);
      return {
        field: {top: rect.top, bottom: rect.bottom, height: rect.height},
        panel: {top: bounds.top, bottom: bounds.bottom, height: bounds.height},
        clip: {top, bottom}, fully_visible: rect.top >= top && rect.bottom <= bottom,
        scroll_top: panel.scrollTop, scroll_height: panel.scrollHeight, client_height: panel.clientHeight,
        window_scroll_y: window.scrollY, window_height: window.innerHeight,
        focused: document.activeElement === field,
        active_element: {tag: document.activeElement?.tagName, id: document.activeElement?.id}
      };
    }""")
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    (target / f"prompt-reuse-stream-geometry-{phase}.json").write_text(
        json.dumps(geometry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return geometry


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_reuse_visible_prompt_edit_append_and_explicit_send_once(task_page, tmp_path):
    page, _, store, runtimes = task_page
    external = []
    page.on("request", lambda request: external.append(request.url) if "probe.invalid" in request.url else None)
    session, turn = prepared_task(task_page, PROMPT)
    writes = browser_posts(page)
    editor(page).fill("earlier draft")
    field = start_reuse(page)
    expect(field).to_have_value(DISPLAY)
    expect(preview(page)).to_contain_text("来源会话：" + session)
    expect(preview(page)).to_contain_text("来源轮次：" + turn)
    expect(preview(page)).to_contain_text("当前可见的脱敏记录，不是原始提交")
    assert "SYNTHETIC-REUSE-SECRET" not in field.input_value()
    assert preview(page).locator("script, img, iframe").count() == 0
    assert page.evaluate("window.reuseExecuted === undefined") and external == []
    field.fill("discard this preview edit")
    local_click(page, preview(page).get_by_role("button", name="放弃复用", exact=True))
    expect(preview(page)).to_have_count(0)
    expect(editor(page)).to_have_value("earlier draft")
    expect(page.get_by_role("button", name="已暂停跟随输出", exact=True)).to_have_attribute("aria-pressed", "false")
    field = start_reuse(page)
    expect(field).to_have_value(DISPLAY)
    edited = DISPLAY + "\n验收补充：只核对当前范围，不重放旧副作用。"
    field.fill(edited)
    latest = "retain newer manual scope and acceptance"
    editor(page).fill(latest)
    screenshot_reuse(page, tmp_path)
    apply_reuse(page, twice=True)
    expected = latest + "\n\n" + edited
    expect(editor(page)).to_have_value(expected)
    expect(editor(page)).to_be_focused()
    expect(preview(page)).to_have_count(0)
    expect(page.get_by_role("status").filter(has_text="指令已加入当前草稿；尚未发送")).to_be_visible()
    assert writes == [] and runtimes[0].prompts == [PROMPT]
    page.get_by_role("button", name="发送指令", exact=True).click()
    until(lambda: runtimes[0].prompts == [PROMPT, expected] and bool(store.session("team", session)["active_turn_id"]))
    followup = store.session("team", session)["active_turn_id"]
    complete_turn(runtimes[0], "explicit reused prompt completed")
    until(lambda: store.turn("team", followup)["state"] == "completed")
    expect(conversation(page)).to_contain_text("explicit reused prompt completed")
    assert len(writes) == 1 and writes[0].endswith(f"/sessions/{session}/turns")


def test_fixed_history_reuses_selected_prompt_while_active_offline_facts_only_limit_send(task_page):
    page, _, store, runtimes = task_page
    session, first = prepared_task(task_page, "historical instruction to edit")
    active = external_start(task_page, session, "different currently running instruction")
    fixed_link(page, session, first)
    expect(conversation(page).locator(".user-message pre")).to_have_text("historical instruction to edit")
    altered = change_read_facts(page, session, {"online": False})
    refresh(page)
    writes = browser_posts(page)
    field = start_reuse(page)
    expect(field).to_have_value("historical instruction to edit")
    expect(preview(page)).to_contain_text(first)
    assert active not in preview(page).inner_text()
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    apply_reuse(page)
    expect(editor(page)).to_have_value("historical instruction to edit")
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    assert store.session("team", session)["active_turn_id"] == active and writes == []
    assert runtimes[0].prompts == ["historical instruction to edit", "different currently running instruction"]
    page.unroute("**/web/api/**", altered)
    complete_turn(runtimes[0], "active instruction ended independently")
    until(lambda: store.turn("team", active)["state"] == "completed")


def test_utf8_draft_capacity_refuses_overflow_preserves_both_then_accepts_exact_limit(task_page):
    page, _, _, runtimes = task_page
    prepared_task(task_page)
    original = "猫" * 21844 + "AB"
    assert len(original.encode("utf-8")) == 65534
    editor(page).fill(original)
    field = start_reuse(page)
    field.fill("😀")
    writes = browser_posts(page)
    apply_reuse(page)
    expect(preview(page).get_by_role("alert").filter(has_text="65536")).to_be_visible()
    expect(editor(page)).to_have_value(original)
    expect(field).to_have_value("😀")
    exact = "猫" * 21843 + "A"
    editor(page).fill(exact)
    apply_reuse(page)
    expected = exact + "\n\n😀"
    assert len(expected.encode("utf-8")) == 65536
    expect(editor(page)).to_have_value(expected)
    expect(preview(page)).to_have_count(0)
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    expect(page.get_by_role("alert").filter(has_text="单次发送最多 16384 字节")).to_be_visible()
    editor(page).press("Control+Enter")
    assert writes == [] and len(runtimes[0].prompts) == 1


def test_truncated_readonly_and_closed_sources_cannot_be_reused(task_page, web_env):
    page, _, store, runtimes = task_page
    session, turn = prepared_task(task_page)
    altered = change_read_facts(page, turn, {"prompt_truncated": True})
    refresh(page)
    expect(reuse_button(page)).to_be_disabled()
    expect(conversation(page)).to_contain_text("本轮指令显示已截断，不能复用不完整的指令")
    expect(preview(page)).to_have_count(0)
    page.unroute("**/web/api/**", altered)
    grants = web_env[1].grants
    web_env[1].grants = [replace(grant, scopes=grant.scopes - {"agent.sessions:write", "agent.turns:cancel"})
                         if grant.kind == "user" else grant for grant in grants]
    page.reload()
    expect(editor(page)).to_be_disabled()
    expect(reuse_button(page)).to_be_disabled()
    expect(preview(page)).to_have_count(0)
    web_env[1].grants = grants
    page.reload()
    start_reuse(page)
    confirm_operation(page, "关闭空闲会话")
    until(lambda: store.session("team", session)["status"] == "closed")
    expect(editor(page)).to_be_disabled()
    expect(reuse_button(page)).to_be_disabled()
    expect(preview(page)).to_have_count(0)
    assert len(runtimes[0].prompts) == 1


def test_preview_survives_refresh_and_turn_state_change_but_not_replaced_prompt(task_page, tmp_path):
    page, _, store, runtimes = task_page
    install_request_probe(page)
    session = create_task(page, store, "reuse-changing-source")
    turn = send_prompt(page, store, runtimes, session, "original active instruction")
    runtimes[0].emit("message.delta", {"item_id": "reuse-stream", "text": "\n".join(f"line {i:03d}" for i in range(160))})
    expect(conversation(page)).to_contain_text("line 159")
    field = start_reuse(page)
    field.fill("my edited pending reuse")
    editor(page).fill("separate original draft")
    writes = browser_posts(page)
    field.evaluate("""field => {
      field.focus({preventScroll: true});
      field.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    }""")
    expect(page.get_by_role("button", name="已暂停跟随输出", exact=True)).to_have_attribute("aria-pressed", "false")
    before = reuse_geometry(field, tmp_path, "before")
    assert before["fully_visible"] and before["focused"], before
    runtimes[0].emit("message.delta", {"item_id": "reuse-stream", "text": "\n" + "later output\n" * 80 + "reuse-stream-tail"})
    expect(conversation(page)).to_contain_text("reuse-stream-tail")
    after = reuse_geometry(field, tmp_path, "after")
    assert abs(after["scroll_top"] - before["scroll_top"]) <= 2, (before, after)
    assert abs(after["window_scroll_y"] - before["window_scroll_y"]) <= 2, (before, after)
    expect(field).to_have_value("my edited pending reuse")
    assert after["fully_visible"], (before, after)
    assert after["focused"], (before, after)
    refresh(page)
    expect(field).to_have_value("my edited pending reuse")
    complete_turn(runtimes[0], "source reached terminal without prompt change")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    refresh(page)
    expect(conversation(page).locator(".turn-context .session-status")).to_have_attribute("data-state", "completed")
    expect(field).to_have_value("my edited pending reuse")
    with store._write() as conn:
        conn.execute("UPDATE hub_turns SET prompt=? WHERE turn_id=?", ("corrected visible instruction", turn))
    refresh(page)
    expect(conversation(page).locator(".user-message pre")).to_have_text("corrected visible instruction")
    expect(preview(page)).to_have_count(0)
    expect(editor(page)).to_have_value("separate original draft")
    assert writes == [] and runtimes[0].prompts == ["original active instruction"]


def test_turn_session_navigation_and_same_tick_old_apply_do_not_cross_drafts(task_page):
    page, _, store, runtimes = task_page
    first, older = prepared_task(task_page, "first historical prompt")
    newer = finish_prompt(page, store, runtimes, first, "newer prompt", "newer response")
    start_reuse(page).fill("preview for newer prompt")
    expect(preview(page)).to_contain_text(newer)
    fixed_link(page, first, older)
    expect(conversation(page)).to_contain_text(older)
    expect(preview(page)).to_have_count(0)
    assert newer != older
    editor(page).fill("first retained draft")
    second = create_task(page, store, "reuse-other-task")
    second_turn = finish_prompt(page, store, runtimes, second, "second prompt", "second response")
    editor(page).fill("second retained draft")
    fixed_link(page, first, older)
    expect(editor(page)).to_have_value("first retained draft")
    start_reuse(page).fill("must not reach either draft")
    stale = preview(page).get_by_role("button", name="加入当前草稿", exact=True).element_handle()
    writes = browser_posts(page)
    stale.evaluate("(button, hash) => { location.hash = hash; button.click() }", f"tasks/{second}/turns/{second_turn}")
    expect(editor(page)).to_have_value("second retained draft")
    expect(preview(page)).to_have_count(0)
    fixed_link(page, first, older)
    expect(editor(page)).to_have_value("first retained draft")
    assert writes == [] and sum(len(runtime.prompts) for runtime in runtimes) == 3


def test_context_change_and_real_revocation_remove_private_preview_and_draft(task_page, web_env):
    page, _, _, runtimes = task_page
    prepared_task(task_page)
    start_reuse(page).fill("private preview before context change")
    editor(page).fill("private draft before context change")
    writes = browser_posts(page)
    web_env[1].grants = [replace(grant, scopes=grant.scopes - {"agent.turns:cancel"})
                         if grant.kind == "user" else grant for grant in web_env[1].grants]
    refresh(page)
    expect(preview(page)).to_have_count(0)
    assert "private preview before context change" not in page.locator("body").inner_text()
    assert "private draft before context change" not in page.locator("textarea").evaluate_all("fields => fields.map(field => field.value)")
    page.reload()
    expect(editor(page)).to_have_value("")
    start_reuse(page).fill("private preview before revocation")
    stale = preview(page).get_by_role("button", name="加入当前草稿", exact=True).element_handle()
    web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
    refresh(page)
    expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
    expect(preview(page)).to_have_count(0)
    expect(editor(page)).to_have_count(0)
    stale.evaluate("button => button.click()")
    assert "private preview before revocation" not in page.locator("body").inner_text()
    assert writes == [] and len(runtimes[0].prompts) == 1


@pytest.mark.parametrize("boundary", ["hidden", "pagehide"])
def test_visibility_boundaries_discard_preview_and_refuse_old_apply(task_page, boundary):
    page, _, _, runtimes = task_page
    prepared_task(task_page)
    editor(page).fill("unsent editor survives only ordinary hidden state")
    start_reuse(page).fill("private boundary preview")
    stale = preview(page).get_by_role("button", name="加入当前草稿", exact=True).element_handle()
    writes = browser_posts(page)
    if boundary == "hidden":
        page.evaluate("""() => {
          Object.defineProperty(document, 'hidden', {configurable: true, value: true});
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
    else:
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    expect(preview(page)).to_have_count(0)
    stale.evaluate("button => button.click()")
    if boundary == "hidden":
        page.evaluate("""() => {
          delete document.hidden; document.dispatchEvent(new Event('visibilitychange'));
        }""")
    else:
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    expect(reuse_button(page)).to_be_enabled()
    expect(preview(page)).to_have_count(0)
    expect(editor(page)).to_have_value("unsent editor survives only ordinary hidden state" if boundary == "hidden" else "")
    assert writes == [] and len(runtimes[0].prompts) == 1


def test_unknown_submission_disables_reuse_without_changing_original_body_or_key(task_page):
    page, context, store, runtimes = task_page
    session, first = prepared_task(task_page)
    start_reuse(page).fill("never substitute this preview for the pending action")
    sent = []

    def lose_first(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            assert route.fetch().status == 202
            route.abort("failed")
        else:
            route.continue_()

    context.route("**/web/api/sessions/" + session + "/turns", lose_first)
    original = "submitted once with its original request identity"
    turn = send_prompt(page, store, runtimes, session, original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    expect(preview(page)).to_have_count(0)
    complete_turn(runtimes[0], "unknown response still executed once")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    refresh(page)
    fixed_link(page, session, first)
    expect(reuse_button(page)).to_be_disabled()
    expect(editor(page)).to_have_value(original)
    assert len(sent) == 1 and runtimes[0].prompts.count(original) == 1
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts.count(original) == 1
