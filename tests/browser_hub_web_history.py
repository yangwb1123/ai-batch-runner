"""Turn search and durable links in Chrome/Hub; issuer/provider and seed rows are fixtures."""

import os
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, TimeoutError as BrowserTimeout

from pbatch.hub_file_snapshot_store import FileSnapshotStore
from browser_hub_web_support import (
    controlled_hub, task_page, create_task, send_prompt, complete_turn, until,
)
from browser_hub_web_results import results
from test_hub_auth import signed
from test_hub_web import web_env

OLD_PROMPT = ("archive-needle literal_%_target " + "完整历史要求 " * 30
              + "\n<script>window.historyScriptExecuted = true</script>"
              + "\npassword=SYNTHETIC-HISTORY-SECRET\nfull prompt tail 🔍")
OLD_DISPLAY = OLD_PROMPT.replace("SYNTHETIC-HISTORY-SECRET", "[REDACTED]")
OLD_REPLY = "old historical response still belongs to original round"
NEW_REPLY = "latest response for current round"


def history_panel(page):
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    return page.get_by_role("tabpanel", name="轮次记录", exact=True)


def history_rows(panel):
    return panel.get_by_role("list", name="轮次记录", exact=True).get_by_role("button")


def query_of(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def finish_prompt(page, store, runtimes, session, prompt, reply):
    turn = send_prompt(page, store, runtimes, session, prompt)
    complete_turn(runtimes[-1], reply)
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    return turn


def actual_history_pair(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "searchable-history-task")
    first = finish_prompt(page, store, runtimes, session, OLD_PROMPT, OLD_REPLY)
    second = finish_prompt(page, store, runtimes, session, "latest instruction", NEW_REPLY)
    return session, first, second


def seed_history(store, first, second, count=50):
    """Read-only ledger fixtures between actual turns; no synthetic claim of agent execution."""
    with store._write() as conn:
        base = conn.execute("SELECT * FROM hub_turns WHERE turn_id=?", (first,)).fetchone()
        latest = conn.execute("SELECT created_at FROM hub_turns WHERE turn_id=?", (second,)).fetchone()[0]
        for index in range(count):
            stamp = base["created_at"] + (latest - base["created_at"]) * (index + 1) / (count + 1)
            conn.execute("INSERT INTO hub_turns VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                f"history-fixture-{index:04d}", base["tenant"], base["session_id"], base["instance_id"],
                base["generation"], "history-fixture", f"History fixture searchable group {index}",
                f"history-{index}", "failed" if index % 2 else "completed", stamp, stamp))


def search_history(page, panel, query, state="", keyboard=False):
    field = panel.get_by_label("搜索轮次", exact=True)
    field.fill(query)
    panel.get_by_label("轮次状态", exact=True).select_option(state)
    with page.expect_response(lambda response: "/turns?" in response.url
                             and query_of(response.url).get("q", "") == query
                             and query_of(response.url).get("state", "") == state):
        if keyboard:
            field.press("Enter")
        else:
            panel.get_by_role("button", name="搜索轮次", exact=True).click()


def assert_old_conversation(page):
    conversation = page.get_by_label("任务对话", exact=True)
    expect(conversation.locator(".user-message pre")).to_have_text(OLD_DISPLAY)
    expect(conversation).to_contain_text(OLD_REPLY)
    expect(conversation).not_to_contain_text(NEW_REPLY)
    assert conversation.locator("script, img, iframe").count() == 0
    assert page.evaluate("window.historyScriptExecuted === undefined")


def screenshot_history(page, panel, tmp_path):
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    panel.scroll_into_view_if_needed()
    page.screenshot(path=str(target / f"history-search-{page.viewport_size['width']}.png"), full_page=True)


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_history_search_precise_link_back_forward_and_reload_keep_the_old_turn(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, first, second = actual_history_pair(task_page)
    seed_history(store, first, second)
    draft = "keep this unsent instruction while revisiting earlier rounds"
    page.get_by_label("发送下一条指令").fill(draft)
    writes, searches = [], []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.on("request", lambda request: searches.append(query_of(request.url).get("q", ""))
            if "/turns?" in request.url else None)
    panel = history_panel(page)
    panel.get_by_role("button", name="最新轮次", exact=True).click()
    expect(history_rows(panel)).to_have_count(20)
    expect(panel.get_by_role("button", name=f"查看轮次 {first}", exact=True)).to_have_count(0)
    field = panel.get_by_label("搜索轮次", exact=True)
    field.fill("archive-needle")
    page.wait_for_timeout(350)
    assert "archive-needle" not in searches
    search_history(page, panel, "archive-needle", keyboard=True)
    expect(history_rows(panel)).to_have_count(1)
    screenshot_history(page, panel, tmp_path)
    panel.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}/turns/{re.escape(first)}$"))
    assert_old_conversation(page)
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    precise = page.url
    assert "archive-needle" not in precise and "password" not in precise
    page.go_back()
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}$"))
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text(NEW_REPLY)
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    page.go_forward()
    expect(page).to_have_url(precise)
    assert_old_conversation(page)
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    page.get_by_role("button", name="工作台", exact=True).click()
    page.get_by_role("button", name="Agent 任务", exact=True).click()
    expect(page).to_have_url(precise)
    assert_old_conversation(page)
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    page.reload()
    expect(page).to_have_url(precise)
    assert_old_conversation(page)
    assert writes == [] and len(runtimes[0].prompts) == 2


def test_history_state_filter_paginates_and_latest_resets_without_writing(task_page):
    page, _, store, _ = task_page
    _, first, second = actual_history_pair(task_page)
    seed_history(store, first, second)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    panel = history_panel(page)
    search_history(page, panel, "History fixture", "completed")
    expect(history_rows(panel)).to_have_count(20)
    first_page = history_rows(panel).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))")
    assert all(int(label.rsplit("-", 1)[1]) % 2 == 0 for label in first_page)
    panel.get_by_role("button", name="查看更早轮次", exact=True).click()
    expect(history_rows(panel)).to_have_count(5)
    second_page = history_rows(panel).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))")
    assert not set(first_page).intersection(second_page)
    expect(panel.get_by_role("button", name="查看更早轮次", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="返回上一页", exact=True).click()
    expect(history_rows(panel)).to_have_count(20)
    assert history_rows(panel).evaluate_all("rows => rows.map(row => row.getAttribute('aria-label'))") == first_page
    panel.get_by_role("button", name="最新轮次", exact=True).click()
    expect(panel.get_by_label("搜索轮次", exact=True)).to_have_value("")
    expect(panel.get_by_label("轮次状态", exact=True)).to_have_value("")
    expect(panel.get_by_role("button", name=f"查看轮次 {second}", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name="返回上一页", exact=True)).to_be_disabled()
    assert writes == []


def test_sending_from_an_old_link_does_not_restore_that_link_after_navigation(task_page):
    page, _, store, runtimes = task_page
    session, first, _ = actual_history_pair(task_page)
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{first}")
    assert_old_conversation(page)
    latest = finish_prompt(page, store, runtimes, session, "new instruction after history review", "new reviewed result")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("new reviewed result")
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}$"))
    page.get_by_role("button", name="工作台", exact=True).click()
    page.get_by_role("button", name="Agent 任务", exact=True).click()
    expect(page).to_have_url(re.compile(rf"#tasks/{re.escape(session)}$"))
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("new reviewed result")
    page.reload()
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("new reviewed result")
    expect(page.get_by_label("任务对话", exact=True)).not_to_contain_text(OLD_REPLY)
    assert store.turn("team", latest)["state"] == "completed"
    assert len(runtimes[0].prompts) == 3


def test_history_search_matches_visible_redaction_and_literal_wildcards_only(task_page):
    page, _, _, _ = task_page
    _, first, _ = actual_history_pair(task_page)
    panel = history_panel(page)
    search_history(page, panel, "SYNTHETIC-HISTORY-SECRET")
    expect(history_rows(panel)).to_have_count(0)
    search_history(page, panel, "[REDACTED]")
    expect(history_rows(panel)).to_have_count(1)
    expect(panel.get_by_role("button", name=f"查看轮次 {first}", exact=True)).to_be_visible()
    search_history(page, panel, "_%_")
    expect(history_rows(panel)).to_have_count(1)
    panel.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    assert_old_conversation(page)
    assert "SYNTHETIC-HISTORY-SECRET" not in page.locator("body").inner_text()


@pytest.mark.parametrize("invalid", ["missing", "wrong-session"])
def test_unknown_or_foreign_turn_link_never_falls_back_to_latest(task_page, invalid):
    page, _, store, runtimes = task_page
    session, first, _ = actual_history_pair(task_page)
    target = "nonexistent-turn" if invalid == "missing" else first
    if invalid == "wrong-session":
        session = create_task(page, store, "other-session-for-link-binding")
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{target}")
    expect(page.get_by_role("alert")).to_contain_text(re.compile("不存在|无效"))
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    assert OLD_REPLY not in page.locator("body").inner_text()
    assert NEW_REPLY not in page.locator("body").inner_text()
    assert writes == [] and len(runtimes[0].prompts) == 2


@pytest.mark.parametrize("failure", ["server", "malformed"])
def test_history_read_failure_is_not_an_empty_success(task_page, failure):
    page, _, _, _ = task_page
    actual_history_pair(task_page)
    panel = history_panel(page)

    def reject(route):
        if query_of(route.request.url).get("q") != "broken-history":
            route.continue_()
        elif failure == "server":
            route.fulfill(status=500, content_type="application/json", body='{"error":{"code":"fixture"}}')
        else:
            route.fulfill(status=200, content_type="application/json", body='{"data":"invalid","next_cursor":""}')

    page.route("**/turns?*", reject)
    search_history(page, panel, "broken-history")
    expect(page.get_by_role("alert")).to_be_visible()
    expect(history_rows(panel)).to_have_count(0)
    assert "没有匹配的轮次" not in page.locator("body").inner_text()


def test_history_instance_revocation_clears_results_query_and_unsent_draft(task_page, web_env):
    page, _, _, _ = task_page
    actual_history_pair(task_page)
    panel = history_panel(page)
    search_history(page, panel, "archive-needle")
    page.get_by_label("发送下一条指令").fill("private draft before history revocation")
    auth = web_env[1]
    original = list(auth.grants)
    auth.grants = [replace(grant, instances=frozenset({"other-instance"}))
                   if grant.kind == "user" else grant for grant in auth.grants]
    panel.get_by_role("button", name="搜索轮次", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("权限")
    expect(page.get_by_label("搜索轮次", exact=True)).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    assert OLD_REPLY not in page.locator("body").inner_text()
    auth.grants = original
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    page.get_by_role("button", name="searchable-history-task", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value("")
    panel = history_panel(page)
    expect(panel.get_by_label("搜索轮次", exact=True)).to_have_value("")


@pytest.mark.parametrize("controlled_hub", [{"share_files": ("history.txt",)}], indirect=True)
def test_comparison_search_finds_baseline_beyond_640_without_replaying_work(task_page, tmp_path):
    page, _, store, runtimes = task_page
    project = tmp_path / "project"
    (project / "history.txt").write_text("original file version\n")
    session = create_task(page, store, "searchable-file-baseline")
    first = finish_prompt(page, store, runtimes, session, "old shared version", "first snapshot")
    snapshots = FileSnapshotStore(store)
    until(lambda: snapshots.read("team", session, first)["state"] == "ready")
    (project / "history.txt").write_text("changed file version\n")
    second = finish_prompt(page, store, runtimes, session, "current shared version", "second snapshot")
    until(lambda: snapshots.read("team", session, second)["state"] == "ready")
    seed_history(store, first, second, count=650)
    results(page)
    panel = page.get_by_role("region", name="文件版本对比", exact=True)
    panel.get_by_role("button", name="选择对比轮次", exact=True).click()
    expect(panel.get_by_label("基准轮次", exact=True).locator(f'option[value="{first}"]')).to_have_count(0)
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    page.get_by_label("发送下一条指令").fill("preserve draft across baseline search")
    panel.get_by_label("搜索基准轮次", exact=True).fill(first)
    with page.expect_response(lambda response: "/turns?" in response.url
                             and query_of(response.url).get("q") == first and response.status == 200):
        panel.get_by_role("button", name="搜索基准轮次", exact=True).click()
    panel.get_by_label("基准轮次", exact=True).select_option(first)
    panel.get_by_role("button", name="比较 history.txt", exact=True).click()
    output = panel.get_by_role("region", name="文件差异", exact=True)
    expect(output).to_contain_text("original file version")
    expect(output).to_contain_text("changed file version")
    expect(output).to_contain_text("字节不同")
    expect(page.get_by_label("发送下一条指令")).to_have_value("preserve draft across baseline search")
    assert writes == [] and len(runtimes[0].prompts) == 2


def hold_read(page, pattern):
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    page.route(pattern, hold)
    return held


def wait_for_held_read(page, reads):
    for _ in range(250):
        if reads:
            return
        page.wait_for_timeout(20)
    raise AssertionError("The controlled read did not reach its fixture gate")


@pytest.mark.parametrize("controlled_hub", [{"share_files": ("report.html", "empty.txt", "binary.bin", "missing.txt")}], indirect=True)
def test_navigation_cancels_old_export_even_while_main_refresh_is_pending(task_page, tmp_path):
    from browser_hub_web_diff import comparison_pair, select_baseline, compare_file
    page, _, _, _ = task_page
    session, first, second, panel = comparison_pair(task_page, tmp_path)
    select_baseline(panel, first)
    expect(compare_file(panel)).to_contain_text("字节不同")
    file_pattern = f"**/turns/{second}/file-snapshots/*"
    file_reads = hold_read(page, file_pattern)
    panel.get_by_role("button", name="下载差异记录", exact=True).click()
    expect(panel.get_by_role("status")).to_contain_text("正在读取")
    detail_pattern = f"**/web/api/sessions/{session}"
    detail_reads = hold_read(page, detail_pattern)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("button", name="刷新任务现场", exact=True)).to_be_disabled()
    wait_for_held_read(page, detail_reads)
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{first}")
    assert len(file_reads) == len(detail_reads) == 1
    stale_download = False
    try:
        with page.expect_download(timeout=1500):
            file_reads[0][0].fulfill(response=file_reads[0][1])
        stale_download = True
    except BrowserTimeout:
        pass
    assert not stale_download, "Old comparison exported after navigation was requested"
    expect(panel).to_have_count(0)
    page.unroute(detail_pattern)
    detail_reads[0][0].fulfill(response=detail_reads[0][1])
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("first file version")
    expect(page).to_have_url(re.compile(rf"/turns/{re.escape(first)}$"))
