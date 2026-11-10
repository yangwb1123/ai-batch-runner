"""Real Chrome/Hub/Gateway recap reads; issuer/provider and large ledger seeds are fixtures."""

import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import expect

from pbatch.hub_store_helpers import append_event_tx
from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, until
from browser_hub_web_follow import browser_posts, external_finish, fixed_link, mode
from browser_hub_web_history import history_panel, history_rows, query_of, search_history
from test_hub_auth import signed
from test_hub_web import web_env


def finish_recap_turn(task_page, session, prompt, reply):
    page, _, store, runtimes = task_page
    turn = send_prompt(page, store, runtimes, session, prompt)
    runtime = runtimes[-1]
    if reply:
        runtime.emit("message.delta", {"item_id": "same-provider-item", "text": reply[:12]})
        runtime.emit("message.completed", {"item_id": "same-provider-item", "text": reply})
    runtime.emit("turn.completed")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    return turn


def real_rounds(task_page, count=2):
    page, _, store, _ = task_page
    session = create_task(page, store, "multi-round-recap-task")
    rows = []
    for index in range(count):
        prompt = f"recap request {index}: " + "完整要求与边界 " * 24 + f"instruction-tail-{index}"
        reply = f"recap reply {index}: completed fixture response with unique tail-{index}"
        turn = finish_recap_turn(task_page, session, prompt, reply)
        rows.append((turn, prompt, reply))
    return session, rows


def recap(page):
    page.get_by_role("tab", name="多轮回顾", exact=True).click()
    panel = page.get_by_role("region", name="多轮回顾工作区", exact=True)
    expect(panel).to_be_visible()
    return panel


def cards(panel):
    return panel.get_by_role("region", name=re.compile(r"^回顾轮次 "))


def assert_recap_rows(panel, rows):
    expect(cards(panel)).to_have_count(len(rows))
    assert cards(panel).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))") == [
        f"回顾轮次 {turn}" for turn, _, _ in rows]
    for turn, prompt, reply in rows:
        card = panel.get_by_role("region", name=f"回顾轮次 {turn}", exact=True)
        expect(card.get_by_text(prompt, exact=True)).to_be_visible()
        expect(card.get_by_text(reply, exact=True)).to_be_visible()
        expect(card).to_contain_text(re.compile("已完成|执行完成"))
        for other, _, other_reply in rows:
            if other != turn:
                expect(card).not_to_contain_text(other_reply)


def test_recap_entry_reads_only_after_explicit_tab_selection(task_page):
    page, _, _, runtimes = task_page
    pages = []
    page.on("request", lambda request: pages.append(request.url)
            if "/turns?" in request.url and query_of(request.url).get("limit") == "5" else None)
    _, rows = real_rounds(task_page, count=1)
    writes = browser_posts(page)
    expect(page.get_by_role("tab", name="多轮回顾", exact=True)).to_be_visible()
    assert pages == []
    panel = recap(page)
    assert_recap_rows(panel, rows)
    expect(panel).to_contain_text("回顾读取于")
    expect(panel).to_contain_text("第 1 页")
    expect(panel).to_contain_text("未独立验证")
    assert len(pages) == 1 and writes == [] and len(runtimes[0].prompts) == 1


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_six_executed_turns_paginate_without_cross_round_reply_pairing(task_page, tmp_path):
    page, _, _, runtimes = task_page
    _, rows = real_rounds(task_page, count=6)
    writes = browser_posts(page)
    panel = recap(page)
    assert_recap_rows(panel, rows[1:])
    expect(panel.get_by_role("button", name="返回较新一页", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="更早的五轮", exact=True).click()
    assert_recap_rows(panel, rows[:1])
    expect(panel).to_contain_text("第 2 页")
    expect(panel.get_by_role("button", name="更早的五轮", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="返回较新一页", exact=True).click()
    assert_recap_rows(panel, rows[1:])
    panel.get_by_role("button", name="更早的五轮", exact=True).click()
    assert_recap_rows(panel, rows[:1])
    panel.get_by_role("button", name="最新五轮", exact=True).click()
    assert_recap_rows(panel, rows[1:])
    expect(panel).to_contain_text("第 1 页")
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    expect(page.locator(".task-detail-heading .session-status")).to_have_attribute("data-state", "idle")
    panel.locator(".recap-heading").evaluate("element => element.scrollIntoView({block: 'start'})")
    page.screenshot(path=str(target / f"recap-viewport-{page.viewport_size['width']}.png"))
    page.screenshot(path=str(target / f"recap-workspace-{page.viewport_size['width']}.png"), full_page=True)
    assert writes == [] and runtimes[0].prompts == [prompt for _, prompt, _ in rows]


def test_open_recap_turn_is_precise_fixed_and_preserves_history_filter_and_draft(task_page):
    page, _, _, runtimes = task_page
    session, rows = real_rounds(task_page)
    history = history_panel(page)
    search_history(page, history, "instruction-tail-0", "completed")
    expect(history_rows(history)).to_have_count(1)
    draft = "retain this original unsent acceptance scope"
    page.get_by_label("发送下一条指令").fill(draft)
    writes = browser_posts(page)
    panel = recap(page)
    assert_recap_rows(panel, rows)
    first = rows[0][0]
    panel.get_by_role("region", name=f"回顾轮次 {first}", exact=True).get_by_role(
        "button", name="打开此轮", exact=True).click()
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}/turns/{re.escape(first)}$"))
    expect(mode(page)).to_contain_text("固定查看此轮")
    page.get_by_role("tab", name="对话", exact=True).click()
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(rows[0][2])
    expect(page.get_by_label("任务对话", exact=True)).not_to_contain_text(rows[1][2])
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    history = history_panel(page)
    expect(history.get_by_label("搜索轮次", exact=True)).to_have_value("instruction-tail-0")
    expect(history.get_by_label("轮次状态", exact=True)).to_have_value("completed")
    expect(history_rows(history)).to_have_count(1)
    assert writes == [] and len(runtimes[0].prompts) == 2


def test_live_new_turn_does_not_replace_recap_snapshot_until_manual_refresh(task_page):
    page, _, _, runtimes = task_page
    session, rows = real_rounds(task_page, count=6)
    panel = recap(page)
    assert_recap_rows(panel, rows[1:])
    panel.get_by_role("button", name="更早的五轮", exact=True).click()
    assert_recap_rows(panel, rows[:1])
    snapshot = panel.inner_text()
    writes = browser_posts(page)
    newer = external_finish(task_page, session, "external recap refresh request", "external recap refresh reply")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("button", name="刷新任务现场", exact=True)).to_be_enabled()
    expect(page.get_by_role("tab", name="多轮回顾", exact=True)).to_have_attribute("aria-selected", "true")
    assert panel.inner_text() == snapshot
    panel.get_by_role("button", name="刷新本页回顾", exact=True).click()
    assert_recap_rows(panel, rows[:1])
    expect(panel).to_contain_text("第 2 页")
    panel.get_by_role("button", name="最新五轮", exact=True).click()
    assert_recap_rows(panel, rows[2:] + [(newer, "external recap refresh request", "external recap refresh reply")])
    assert writes == [] and len(runtimes[0].prompts) == 7


def test_unknown_write_can_read_recap_without_replacing_retry_body_or_key(task_page):
    page, context, store, runtimes = task_page
    session, rows = real_rounds(task_page, count=1)
    sent = []

    def drop_first(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            assert route.fetch().status == 202
            route.abort("failed")
        else:
            route.continue_()

    context.route("**/web/api/sessions/" + session + "/turns", drop_first)
    original = "unknown recap submission must retain exact bytes"
    turn = send_prompt(page, store, runtimes, session, original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    runtimes[0].emit("message.completed", {"item_id": "same-provider-item", "text": "executed unknown reply"})
    runtimes[0].emit("turn.completed")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(mode(page)).to_be_visible()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    panel = recap(page)
    assert_recap_rows(panel, rows + [(turn, original, "executed unknown reply")])
    panel.get_by_role("button", name="刷新本页回顾", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    assert len(sent) == 1 and len(runtimes[0].prompts) == 2
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts.count(original) == 1


def test_read_only_grant_can_read_and_open_recap_without_post(task_page, web_env):
    page, _, _, runtimes = task_page
    _, rows = real_rounds(task_page)
    web_env[1].grants = [replace(grant, scopes=grant.scopes - {"agent.sessions:write", "agent.turns:cancel"})
                         if grant.kind == "user" else grant for grant in web_env[1].grants]
    writes = browser_posts(page)
    page.reload()
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    panel = recap(page)
    assert_recap_rows(panel, rows)
    panel.get_by_role("region", name=f"回顾轮次 {rows[0][0]}", exact=True).get_by_role(
        "button", name="打开此轮", exact=True).click()
    expect(mode(page)).to_contain_text("固定查看此轮")
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    assert writes == [] and len(runtimes[0].prompts) == 2


def test_revocation_clears_already_loaded_recap_and_unsent_draft(task_page, web_env):
    page, _, _, runtimes = task_page
    _, rows = real_rounds(task_page)
    page.get_by_label("发送下一条指令").fill("private draft before recap authority revocation")
    panel = recap(page)
    assert_recap_rows(panel, rows)
    writes = browser_posts(page)
    web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
    panel.get_by_role("button", name="刷新本页回顾", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
    expect(cards(page)).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    body = page.locator("body").inner_text()
    assert all(reply not in body for _, _, reply in rows)
    assert "private draft before recap authority revocation" not in body
    assert writes == [] and len(runtimes[0].prompts) == 2


@pytest.mark.parametrize("failure", ["server", "malformed"])
def test_recap_read_failure_never_becomes_an_empty_success(task_page, failure):
    page, _, _, _ = task_page
    real_rounds(task_page, count=1)

    def reject(route):
        if query_of(route.request.url).get("limit") != "5":
            route.continue_()
        elif failure == "server":
            route.fulfill(status=500, content_type="application/json", body='{"error":{"code":"fixture"}}')
        else:
            route.fulfill(status=200, content_type="application/json", body='{"data":[],"next_cursor":17}')

    page.route("**/turns?*", reject)
    writes = browser_posts(page)
    page.get_by_role("tab", name="多轮回顾", exact=True).click()
    expect(page.get_by_role("alert")).to_be_visible()
    expect(cards(page)).to_have_count(0)
    expect(page.get_by_role("region", name="多轮回顾工作区", exact=True)).to_have_count(0)
    expect(page.locator("body")).not_to_contain_text("回顾读取于")
    assert writes == []


def delay_recap_event_delivery(page, session, turn):
    """Hold actual authenticated HTTP bytes after receipt, even if the read gets cancelled."""
    page.add_init_script("""(() => {
      const original = window.fetch.bind(window);
      const gate = window.recapReadGate = {enabled: false, ready: false, reads: 0, aborts: 0};
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
    expect(mode(page)).to_contain_text("跟随任务现场")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    page.evaluate("target => Object.assign(window.recapReadGate, target, {enabled: true})",
                  {"session": session, "turn": turn})


def wait_for_recap_gate(page):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if page.evaluate("window.recapReadGate.ready"):
            return
        page.wait_for_timeout(20)
    assert page.evaluate("window.recapReadGate.ready"), "Recap event read did not reach its five-second gate"


@pytest.mark.parametrize("leave", ["navigation", "revocation"])
def test_delayed_recap_bytes_cannot_restore_an_old_task_or_revoked_authority(task_page, web_env, leave):
    page, _, store, runtimes = task_page
    session, rows = real_rounds(task_page)
    other = create_task(page, store, "other-task-for-recap-navigation")
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(rows[-1][2])
    writes = browser_posts(page)
    delay_recap_event_delivery(page, session, rows[0][0])
    page.get_by_label("发送下一条指令").fill("private recap navigation draft")
    recap(page)
    try:
        wait_for_recap_gate(page)
        if leave == "navigation":
            page.evaluate("hash => { location.hash = hash }", f"tasks/{other}")
            expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(other)}$"))
            expect(page.get_by_label("发送下一条指令")).to_have_value("")
        else:
            web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
            page.get_by_role("button", name="刷新任务现场", exact=True).click()
            expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
            expect(page.get_by_label("发送下一条指令")).to_have_count(0)
        expect(page.get_by_role("region", name=f"回顾轮次 {rows[0][0]}", exact=True)).to_have_count(0)
        assert page.evaluate("window.recapReadGate.reads") == 1
        assert page.evaluate("window.recapReadGate.aborts") == 1
    finally:
        page.evaluate("window.recapReadGate.release?.()")
    page.wait_for_timeout(100)
    assert rows[0][2] not in page.locator("body").inner_text()
    assert "private recap navigation draft" not in page.locator("body").inner_text()
    assert writes == [] and sum(len(runtime.prompts) for runtime in runtimes) == 2


def append_fixture_event(conn, base, turn, index, payload, kind="message.delta"):
    """Read-only ledger fixture; this helper never represents additional provider execution."""
    canonical = json.dumps({"session_id": base["session_id"], "turn_id": turn, "kind": kind, "payload": payload})
    append_event_tx(conn, base["tenant"], base["instance_id"], base["generation"],
        f"recap-seed-{turn}-{index}", base["session_id"], turn, kind, payload, canonical, base["updated_at"])


def seed_dense_recap(store, reference, *, large=False):
    """Five read-only seeded turns, each with 105 events; actual execution remains one turn."""
    identifiers = []
    with store._write() as conn:
        base = conn.execute("SELECT * FROM hub_turns WHERE turn_id=?", (reference,)).fetchone()
        for index in range(5):
            turn, stamp = f"recap-budget-fixture-{index}", base["created_at"] + index + 1
            identifiers.append(turn)
            conn.execute("INSERT INTO hub_turns VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                turn, base["tenant"], base["session_id"], base["instance_id"], base["generation"],
                "recap-ledger-fixture", f"Read-only budget seed {index}", turn, "completed", stamp, stamp))
            for event in range(105):
                text = f"fixture-{index}-{event:03d} " + ("界" * 2600 if large else "activity only")
                payload = {"item_id": "same-seeded-item", "text": text}
                kind = "message.delta" if large or event == 104 else "command.started"
                append_fixture_event(conn, base, turn, event, payload, kind)
    return identifiers


def track_seed_event_reads(page, identifiers):
    reads = []

    def track(request):
        query = query_of(request.url)
        if "/events?" in request.url and query.get("turn_id") in identifiers:
            reads.append(query)

    page.on("request", track)
    return reads


def assert_two_pages_per_seed(reads, identifiers):
    assert len(reads) == 10
    for turn in identifiers:
        selected = [query for query in reads if query["turn_id"] == turn]
        assert len(selected) == 2 and all(query["limit"] == "50" for query in selected)
        assert selected[0].get("after", "0") == "0" and int(selected[1]["after"]) > 0


def test_recap_stops_at_one_hundred_events_per_turn_and_does_not_claim_no_reply(task_page):
    page, _, store, runtimes = task_page
    session, rows = real_rounds(task_page, count=1)
    fixed_link(page, session, rows[0][0])
    identifiers = seed_dense_recap(store, rows[0][0])
    reads, writes = track_seed_event_reads(page, identifiers), browser_posts(page)
    panel = recap(page)
    expect(cards(panel)).to_have_count(5)
    for turn in identifiers:
        card = panel.get_by_role("region", name=f"回顾轮次 {turn}", exact=True)
        expect(card).to_contain_text("尚有未读取事件")
        expect(card).to_contain_text("已读取片段尚未包含可显示的回复，不能据此判断本轮没有回复。")
        expect(card).not_to_contain_text("较早事件未保留")
    assert_two_pages_per_seed(reads, identifiers)
    assert writes == [] and len(runtimes[0].prompts) == 1


def test_global_recap_byte_budget_exposes_trimmed_earlier_events(task_page):
    page, _, store, runtimes = task_page
    session, rows = real_rounds(task_page, count=1)
    fixed_link(page, session, rows[0][0])
    identifiers = seed_dense_recap(store, rows[0][0], large=True)
    reads, writes = track_seed_event_reads(page, identifiers), browser_posts(page)
    panel = recap(page)
    expect(cards(panel)).to_have_count(5)
    expect(panel.get_by_text("本轮较早事件未保留在回顾窗口").first).to_be_visible()
    expect(panel.get_by_text("尚有未读取事件")).to_have_count(5)
    assert 0 < len(reads) <= 10
    for turn in identifiers:
        selected = [query for query in reads if query["turn_id"] == turn]
        assert 1 <= len(selected) <= 2 and all(query["limit"] == "50" for query in selected)
    assert len(panel.inner_text().encode("utf-8")) < 1024 * 1024 + 16384
    assert writes == [] and len(runtimes[0].prompts) == 1


def test_missing_message_chunks_are_partial_and_not_fabricated_empty_success(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "missing-recap-chunk-task")
    turn = finish_recap_turn(task_page, session, "inspect an incomplete recorded reply", "")
    with store._write() as conn:
        base = conn.execute("SELECT * FROM hub_turns WHERE turn_id=?", (turn,)).fetchone()
        append_fixture_event(conn, base, turn, "missing", {"item_id": "incomplete-seed",
            "text": "only the middle chunk is available", "chunk_index": 1, "chunk_count": 3}, "message.completed")
    writes = browser_posts(page)
    panel = recap(page)
    card = panel.get_by_role("region", name=f"回顾轮次 {turn}", exact=True)
    expect(card).to_contain_text("only the middle chunk is available")
    expect(card).to_contain_text(re.compile("不完整|缺片|截断"))
    expect(card).not_to_contain_text("尚有未读取事件")
    expect(card).not_to_contain_text("没有可显示的回复")
    expect(panel).to_contain_text("未独立验证")
    assert writes == [] and len(runtimes[0].prompts) == 1
