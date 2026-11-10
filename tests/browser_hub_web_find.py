"""Find loaded conversation text in real Chrome/HTTPS; issuer and provider are fixtures."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from browser_hub_web_history import finish_prompt
from browser_hub_web_follow import browser_posts, fixed_link
from browser_hub_web_recap import recap
from browser_hub_web_results import results, download_record
from test_hub_auth import signed
from test_hub_web import web_env

MISSING_END = "未收到回复结束记录，完整性未知。"


def conversation(page):
    return page.get_by_label("任务对话", exact=True)


def finder(page):
    return page.get_by_role("search", name="本轮对话查找", exact=True)


def current_match(page):
    return conversation(page).locator('mark[data-conversation-match][aria-current="true"]')


def install_request_probe(page):
    """Wrap fetch before createTasks captures it; observe local actions, not background polls."""
    page.add_init_script("""(() => {
      const original = window.fetch.bind(window);
      const probe = window.findRequestProbe = {active: false, calls: []};
      window.fetch = (...args) => {
        if (probe.active) probe.calls.push(String(args[0]));
        return original(...args);
      };
    })()""")
    page.reload()
    expect(page.get_by_role("button", name="新建任务", exact=True)).to_be_enabled()


def local_action(page, label):
    button = finder(page).get_by_role("button", name=label, exact=True)
    expect(button).to_be_enabled()
    before = page.url
    calls = button.evaluate("""async button => {
      const probe = window.findRequestProbe;
      probe.calls = []; probe.active = true;
      try { button.click(); await Promise.resolve(); await Promise.resolve(); }
      finally { probe.active = false; }
      return probe.calls;
    }""")
    assert calls == [], "local search/navigation must not read, page or write remote data"
    assert page.url == before
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")


def find_text(page, query):
    field = finder(page).get_by_label("查找本轮对话", exact=True)
    expect(field).to_be_visible()
    field.fill(query)
    local_action(page, "查找")


def assert_located(page, text):
    selected = current_match(page)
    expect(selected).to_have_count(1)
    expect(selected).to_have_text(text)
    assert selected.evaluate("""mark => {
      const outer = mark.closest('.task-conversation').getBoundingClientRect();
      const inner = mark.getBoundingClientRect();
      return inner.top >= outer.top && inner.bottom <= outer.bottom;
    }"""), "the selected text mark must be inside the conversation viewport"
    expect(page.get_by_role("button", name="已暂停跟随输出", exact=True)).to_have_attribute("aria-pressed", "false")


def finished_task(task_page, prompt, reply, name="find-conversation"):
    page, _, store, runtimes = task_page
    install_request_probe(page)
    session = create_task(page, store, name)
    turn = finish_prompt(page, store, runtimes, session, prompt, reply)
    expect(conversation(page).locator('.turn-context .session-status')).to_have_attribute("data-state", "completed")
    return session, turn


def assert_reply_format(page, reply, paragraphs):
    """Check both normal preview and the existing 256-block raw fallback contract."""
    panel = conversation(page)
    heading = panel.get_by_role("heading", name="Find fixture", exact=True)
    copy_code = panel.get_by_role("button", name="复制代码", exact=True)
    if paragraphs == 80:
        expect(panel.get_by_label("回复格式预览", exact=True)).to_be_visible()
        expect(heading).to_be_visible()
        expect(copy_code).to_be_visible()
        expect(panel.locator(".reply-code code")).to_have_text("print('safe code')")
    else:
        expect(panel).to_contain_text("此回复超出格式预览预算，已显示完整的当前原文。")
        expect(heading).to_have_count(0)
        expect(copy_code).to_have_count(0)
        expect(panel.locator(".reply-original")).to_have_text(reply)
        assert panel.locator(".reply-original").text_content() == reply


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
@pytest.mark.parametrize("paragraphs", [80, 160])
def test_find_locates_middle_text_and_clear_restores_formatted_reply(task_page, tmp_path, paragraphs):
    page, _, _, runtimes = task_page
    lines = [f"paragraph {index:03d}: review the exact requirement" for index in range(paragraphs)]
    lines[paragraphs // 2], lines[paragraphs * 7 // 8] = "middle locate-anchor text", "later locate-anchor text"
    reply = "# Find fixture\n\n" + "\n\n".join(lines) + "\n\n```python\nprint('safe code')\n```"
    finished_task(task_page, "inspect prompt-anchor without executing a search", reply)
    assert_reply_format(page, reply, paragraphs)
    draft = "unsent draft must survive conversation lookup"
    page.get_by_label("发送下一条指令").fill(draft)
    writes = browser_posts(page)
    find_text(page, "prompt-anchor")
    assert_located(page, "prompt-anchor")
    find_text(page, "locate-anchor")
    expect(finder(page)).to_contain_text("第 1 / 2 处")
    assert_located(page, "locate-anchor")
    first = conversation(page).evaluate("element => element.scrollTop")
    outer_scroll = page.evaluate("window.scrollY")
    assert first > 500
    local_action(page, "下一处")
    expect(finder(page)).to_contain_text("第 2 / 2 处")
    assert_located(page, "locate-anchor")
    assert conversation(page).evaluate("element => element.scrollTop") > first
    assert abs(page.evaluate("window.scrollY") - outer_scroll) <= 1
    local_action(page, "上一处")
    expect(finder(page)).to_contain_text("第 1 / 2 处")
    assert abs(page.evaluate("window.scrollY") - outer_scroll) <= 1
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target / f"find-active-{paragraphs}-{page.viewport_size['width']}.png"), full_page=True)
    local_action(page, "清除查找")
    expect(conversation(page).locator("mark")).to_have_count(0)
    expect(finder(page).get_by_label("查找本轮对话", exact=True)).to_have_value("")
    assert_reply_format(page, reply, paragraphs)
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    assert writes == [] and len(runtimes[0].prompts) == 1


def test_literal_unicode_find_matches_across_real_completed_chunks(task_page):
    page, _, store, _ = task_page
    requests = []
    page.on("request", lambda request: requests.append(request.url) if "probe.invalid" in request.url else None)
    reply = "pad " * 1023 + " " + "BOUNDARY ΔÉ [a+b].* (x) <img src=https://probe.invalid/find>"
    session, turn = finished_task(task_page, "find literal text in the loaded response", reply)
    events = [row for row in store.events("team", session, limit=100, turn_id=turn) if row["kind"] == "message.completed"]
    assert len(events) >= 2
    assert events[0]["payload"]["text"].endswith("BOU")
    assert events[1]["payload"]["text"].startswith("NDARY")
    for query, expected in [("boundary", "BOUNDARY"), ("δé", "ΔÉ"), ("[a+b].*", "[a+b].*")]:
        find_text(page, query)
        expect(finder(page)).to_contain_text("第 1 / 1 处")
        assert_located(page, expected)
    assert conversation(page).locator("img, script, iframe").count() == 0
    assert requests == []


def test_find_caps_matches_and_discloses_the_bounded_reply_projection(task_page):
    page, _, store, runtimes = task_page
    install_request_probe(page)
    session = create_task(page, store, "bounded-find")
    turn = send_prompt(page, store, runtimes, session, "inspect bounded search coverage")
    runtimes[0].emit("message.completed", {"item_id": "many-matches", "text": "cap-hit " * 201})
    for index in range(1, 65):
        runtimes[0].emit("message.completed", {"item_id": f"item-{index}",
            "text": "outside-display-only" if index == 64 else f"visible reply {index}"})
    runtimes[0].emit("turn.completed")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(conversation(page).locator('.turn-context .session-status')).to_have_attribute("data-state", "completed")
    expect(conversation(page)).to_contain_text("部分内容未完整保留")
    find_text(page, "cap-hit")
    expect(conversation(page).locator("mark[data-conversation-match]")).to_have_count(200)
    expect(finder(page)).to_contain_text("最多显示前 200 处匹配")
    find_text(page, "outside-display-only")
    expect(conversation(page).locator("mark")).to_have_count(0)
    expect(finder(page)).to_contain_text("未读取部分是否包含该词未知")
    expect(finder(page)).to_contain_text("仅查找当前已显示的指令与回复")
    expect(conversation(page)).to_contain_text("部分内容未完整保留")


def test_stream_updates_keep_located_scroll_and_terminal_delta_warns(task_page):
    page, _, store, runtimes = task_page
    install_request_probe(page)
    session = create_task(page, store, "stream-find")
    turn = send_prompt(page, store, runtimes, session, "stream while I inspect the middle")
    body = "\n".join(f"stream line {index:03d}" for index in range(160))
    body = body.replace("stream line 080", "stream-locate-anchor")
    runtimes[0].emit("message.delta", {"item_id": "streaming", "text": body})
    expect(conversation(page)).to_contain_text("stream line 159")
    expect(conversation(page)).not_to_contain_text(MISSING_END)
    find_text(page, "stream-locate-anchor")
    assert_located(page, "stream-locate-anchor")
    before = conversation(page).evaluate("element => element.scrollTop")
    runtimes[0].emit("message.delta", {"item_id": "streaming", "text": "\n" + "later line\n" * 60 + "new-stream-tail"})
    expect(conversation(page)).to_contain_text("new-stream-tail")
    assert abs(conversation(page).evaluate("element => element.scrollTop") - before) <= 2
    expect(current_match(page)).to_have_text("stream-locate-anchor")
    replacement = "\n".join(f"revised line {index:03d}" for index in range(200))
    replacement = replacement.replace("revised line 120", "stream-locate-anchor")
    runtimes[0].emit("message.completed", {"item_id": "streaming", "text": replacement})
    expect(conversation(page)).to_contain_text("revised line 199")
    expect(finder(page).get_by_label("查找本轮对话", exact=True)).to_have_value("stream-locate-anchor")
    expect(current_match(page)).to_have_count(0)
    expect(finder(page)).to_contain_text("内容已更新，原定位已失效")
    expect(finder(page)).to_contain_text("找到 1 处，尚未定位")
    expect(page.get_by_role("button", name="已暂停跟随输出", exact=True)).to_have_attribute("aria-pressed", "false")
    expect(conversation(page)).not_to_contain_text(MISSING_END)
    local_action(page, "下一处")
    assert_located(page, "stream-locate-anchor")
    assert current_match(page).evaluate("""mark => {
      const range = document.createRange();
      range.setStart(mark.parentNode, 0); range.setEndBefore(mark);
      return range.toString().length;
    }""") == replacement.index("stream-locate-anchor")
    runtimes[0].emit("message.delta", {"item_id": "streaming", "text": "\nunfinished-after-replacement"})
    expect(conversation(page)).to_contain_text("unfinished-after-replacement")
    runtimes[0].emit("turn.completed")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(conversation(page)).to_contain_text(MISSING_END)


def test_find_clears_on_turn_task_pagehide_and_revocation(task_page, web_env):
    page, _, store, runtimes = task_page
    session, _ = finished_task(task_page, "first find request", "first-only-needle")
    find_text(page, "first-only-needle")
    finish_prompt(page, store, runtimes, session, "second find request", "second-only-needle")
    expect(finder(page).get_by_label("查找本轮对话", exact=True)).to_have_value("")
    expect(conversation(page).locator("mark")).to_have_count(0)
    find_text(page, "second-only-needle")
    other = create_task(page, store, "another-find-task")
    finish_prompt(page, store, runtimes, other, "other request", "other-only-needle")
    expect(finder(page).get_by_label("查找本轮对话", exact=True)).to_have_value("")
    find_text(page, "other-only-needle")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    expect(finder(page)).to_have_count(0)
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    expect(conversation(page)).to_contain_text("other-only-needle")
    expect(finder(page).get_by_label("查找本轮对话", exact=True)).to_have_value("")
    expect(conversation(page).locator("mark")).to_have_count(0)
    find_text(page, "other-only-needle")
    web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
    expect(finder(page)).to_have_count(0)
    assert "other-only-needle" not in page.locator("body").inner_text()


def test_find_keeps_unknown_submission_draft_and_original_retry_key(task_page):
    page, context, store, runtimes = task_page
    session, first = finished_task(task_page, "known instruction", "old-round-findable")
    sent = []

    def lose_first_response(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            assert route.fetch().status == 202
            route.abort("failed")
        else:
            route.continue_()

    context.route("**/web/api/sessions/" + session + "/turns", lose_first_response)
    original = "pending instruction must stay unchanged"
    turn = send_prompt(page, store, runtimes, session, original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    complete_turn(runtimes[0], "unknown write completed once")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    fixed_link(page, session, first)
    expect(conversation(page)).to_contain_text("old-round-findable")
    find_text(page, "old-round-findable")
    assert_located(page, "old-round-findable")
    find_text(page, original)
    expect(conversation(page).locator("mark")).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    assert len(sent) == 1 and len(runtimes[0].prompts) == 2
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1] and runtimes[0].prompts.count(original) == 1


def test_recap_uses_each_rounds_terminal_reply_completeness(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "mixed-reply-completeness")
    turns = []
    for kind, text in [("message.delta", "terminal delta body"), ("message.started", ""),
                       ("message.completed", "fully completed body")]:
        turn = send_prompt(page, store, runtimes, session, "check " + kind)
        runtimes[0].emit(kind, {"item_id": "same-item-across-rounds", "text": text})
        runtimes[0].emit("turn.completed")
        until(lambda: store.turn("team", turn)["state"] == "completed")
        expect(page.get_by_label("发送下一条指令")).to_be_enabled()
        turns.append(turn)
    running = send_prompt(page, store, runtimes, session, "keep generating without an end marker")
    runtimes[0].emit("message.delta", {"item_id": "same-item-across-rounds", "text": "running delta body"})
    expect(conversation(page)).to_contain_text("running delta body")
    expect(conversation(page)).not_to_contain_text(MISSING_END)
    panel = recap(page)
    for turn in turns[:2]:
        expect(panel.get_by_role("region", name=f"回顾轮次 {turn}", exact=True)).to_contain_text(MISSING_END)
    for turn in [turns[2], running]:
        expect(panel.get_by_role("region", name=f"回顾轮次 {turn}", exact=True)).not_to_contain_text(MISSING_END)
    panel.get_by_role("region", name=f"回顾轮次 {turns[0]}", exact=True).get_by_role("button", name="打开此轮", exact=True).click()
    expect(conversation(page)).to_contain_text(MISSING_END)
    results(page)
    record, _ = download_record(page, tmp_path)
    assert record["turn"]["turn_id"] == turns[0] and record["coverage"]["reply_completion_missing"] is True
    complete_turn(runtimes[0], "running round then completed normally")
