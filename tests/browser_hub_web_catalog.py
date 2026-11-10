"""Task catalog acceptance in real Chrome/Hub; only issuer/provider are fixtures."""

import re
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import (
    controlled_hub, task_page, create_task, send_prompt, complete_turn, until,
)
from test_hub_auth import signed
from test_hub_web import web_env
from test_hub_web_control import SCOPES


def catalog(page):
    return page.get_by_role("complementary", name="项目与任务", exact=True)


def task_rows(page):
    return catalog(page).get_by_role("list", name="Agent 任务列表", exact=True)


def query_of(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def search_tasks(page, text, *, keyboard=False):
    search = catalog(page).get_by_role("searchbox", name="搜索任务", exact=True)
    search.fill(text)
    with page.expect_response(lambda response: "/web/api/task-sessions?" in response.url
                             and query_of(response.url).get("q", "").casefold() == text.casefold()
                             and response.status == 200):
        if keyboard:
            search.press("Enter")
        else:
            catalog(page).get_by_role("button", name="搜索任务", exact=True).click()


def list_names(page):
    return task_rows(page).get_by_role("button").evaluate_all(
        "nodes => nodes.map(node => node.getAttribute('aria-label'))")


def seed_catalog(store, auth, count=125, *, names=None):
    """Register read-only ledger records; they are not executing Gateway sessions."""
    auth.grants[0] = replace(auth.grants[0], instances=frozenset({"alpha", "catalog"}))
    instance = store.register_instance("team", "catalog", {
        "boot_id": "catalog-fixture", "project_id": "catalog-project", "name": "Catalog project",
    })
    labels = names if names is not None else [f"catalog-task-{index:03d}" for index in range(count)]
    return [store.upsert_session("team", "catalog", instance["generation"], {
        "local_session_id": f"catalog-record-{index}", "name": name,
        "project_id": "catalog-project", "status": "idle", "controllable": False,
    }) for index, name in enumerate(labels)]


def refresh_all_projects(page):
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    catalog(page).get_by_label("项目实例", exact=True).select_option("")
    expect(catalog(page).get_by_role("button", name="下一页", exact=True)).to_be_enabled()


def walk_catalog_pages(page, expected):
    """Read bounded windows, never accumulating task rows in the product DOM."""
    seen = []
    page_count = (len(expected) + 19) // 20
    for index in range(page_count):
        expect(catalog(page)).to_contain_text(re.compile(rf"第\s*{index + 1}\s*页"))
        expect(task_rows(page).get_by_role("button")).to_have_count(min(20, len(expected) - len(seen)))
        current = list_names(page)
        assert not set(current).intersection(seen)
        seen.extend(current)
        next_page = catalog(page).get_by_role("button", name="下一页", exact=True)
        if index + 1 < page_count:
            next_page.click()
        else:
            expect(next_page).to_be_disabled()
    assert set(seen) == set(expected)


def test_failed_idle_session_remains_actionable_until_a_later_success(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "recover-failed-catalog-task")
    first = send_prompt(page, store, runtimes, session, "fail this catalog task")
    runtimes[0].emit("turn.failed", {"reason": "catalog fixture failure"})
    until(lambda: store.turn("team", first)["state"] == "failed")
    assert store.session("team", session)["status"] == "idle"
    catalog(page).get_by_role("button", name="需处理", exact=True).click()
    row = task_rows(page).get_by_role("button", name="recover-failed-catalog-task", exact=True)
    expect(row).to_be_visible()
    expect(row).to_contain_text("空闲")
    expect(row).to_contain_text("最近执行")
    expect(row).to_contain_text("失败")
    row.click()
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("catalog fixture failure")
    second = send_prompt(page, store, runtimes, session, "explicitly recover the catalog task")
    complete_turn(runtimes[0], "catalog task recovered")
    until(lambda: store.turn("team", second)["state"] == "completed")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("catalog task recovered")
    expect(row).to_have_count(0)
    catalog(page).get_by_role("button", name="已完成", exact=True).click()
    expect(row).to_be_visible()
    expect(row).to_contain_text("最近执行")
    expect(row).to_contain_text("完成")
    assert store.session("team", session)["status"] == "idle"
    assert runtimes[0].prompts == ["fail this catalog task", "explicitly recover the catalog task"]


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_catalog_search_and_paging_find_beyond_100_without_losing_draft(task_page, web_env):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "selected-task-keeps-draft")
    draft = "preserve this instruction while browsing other catalog pages"
    page.get_by_label("发送下一条指令").fill(draft)
    records = seed_catalog(store, web_env[1])
    beyond = sorted(records, key=lambda row: row["session_id"])[110]
    assert beyond["session_id"] not in {
        row["session_id"] for row in store.sessions("team", limit=100)
    }
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    refresh_all_projects(page)
    search_tasks(page, beyond["name"], keyboard=True)
    expect(task_rows(page).get_by_role("button")).to_have_count(1)
    expect(task_rows(page)).to_contain_text(beyond["name"])
    expect(catalog(page)).to_contain_text(re.compile(r"第\s*1\s*页"))
    search_tasks(page, "")
    expected = [row["name"] for row in records] + ["selected-task-keeps-draft"]
    walk_catalog_pages(page, expected)
    catalog(page).get_by_role("button", name="上一页", exact=True).click()
    expect(catalog(page)).to_contain_text(re.compile(r"第\s*6\s*页"))
    catalog(page).get_by_role("button", name="首页", exact=True).click()
    expect(catalog(page)).to_contain_text(re.compile(r"第\s*1\s*页"))
    expect(catalog(page).get_by_role("button", name="上一页", exact=True)).to_be_disabled()
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    assert runtimes[0].prompts == [] and writes == []


def test_catalog_search_is_literal_unicode_and_instance_scoped(task_page, web_env):
    page, _, store, _ = task_page
    names = ["literal_%_target", "literal_AX_target", "Straße-需求检查"]
    seed_catalog(store, web_env[1], names=names)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    search_tasks(page, "_%_")
    expect(task_rows(page).get_by_role("button")).to_have_count(1)
    expect(task_rows(page)).to_contain_text("literal_%_target")
    search_tasks(page, "STRASSE-需求", keyboard=True)
    expect(task_rows(page).get_by_role("button")).to_have_count(1)
    expect(task_rows(page)).to_contain_text("Straße-需求检查")
    catalog(page).get_by_label("项目实例", exact=True).select_option("alpha")
    expect(task_rows(page).get_by_role("button")).to_have_count(0)
    catalog(page).get_by_label("项目实例", exact=True).select_option("catalog")
    expect(task_rows(page).get_by_role("button")).to_have_count(1)
    expect(task_rows(page)).to_contain_text("Straße-需求检查")


def test_revocation_during_real_page_response_clears_catalog_detail_and_draft(task_page, web_env):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "private-selected-task")
    seed_catalog(store, web_env[1], count=25)
    refresh_all_projects(page)
    page.get_by_label("发送下一条指令").fill("private draft before catalog revocation")
    original = list(web_env[1].grants)
    returned = []

    def revoke_after_read(route):
        if not query_of(route.request.url).get("after") or returned:
            route.continue_()
            return
        response = route.fetch()
        assert response.status == 200
        returned.extend(row["name"] for row in response.json()["data"])
        web_env[1].grants = [grant for grant in original if grant.kind == "gateway"]
        route.fulfill(response=response)

    pattern = "**/web/api/task-sessions?*"
    context.route(pattern, revoke_after_read)
    catalog(page).get_by_role("button", name="下一页", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("权限")
    expect(task_rows(page).get_by_role("button")).to_have_count(0)
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    assert returned and not any(name in page.locator("body").inner_text() for name in returned)
    assert "private-selected-task" not in page.locator("body").inner_text()
    assert runtimes[0].prompts == []
    context.unroute(pattern, revoke_after_read)
    web_env[1].grants = original
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    search_tasks(page, "private-selected-task")
    task_rows(page).get_by_role("button", name="private-selected-task", exact=True).click()
    expect(page.get_by_label("发送下一条指令")).to_have_value("")
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))


def test_relogin_clears_catalog_query_and_old_account_selection(task_page, web_env, signed):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "alice-private-task")
    seed_catalog(store, web_env[1], names=["bob-catalog-task"])
    page.get_by_label("发送下一条指令").fill("alice private unsent instruction")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    search_tasks(page, "alice-private")
    page.get_by_role("button", name="退出本地 abr 会话").click()
    auth, transport = web_env[1:3]
    auth.grants.append(replace(auth.grants[0], subject="bob", instances=frozenset({"catalog"})))
    transport.token = lambda: signed[1](sub="bob", scope=" ".join(sorted(SCOPES)),
                                       exp=int(time.time()) + 120)
    page.get_by_role("button", name="使用 Snaplink 登录").click()
    page.get_by_role("button", name="Synthetic issuer approval").click()
    page.get_by_role("button", name="Agent 任务", exact=True).click()
    expect(task_rows(page)).to_contain_text("bob-catalog-task")
    expect(catalog(page).get_by_role("searchbox", name="搜索任务", exact=True)).to_have_value("")
    expect(catalog(page).get_by_role("button", name="全部", exact=True)).to_have_attribute("aria-pressed", "true")
    assert "alice-private-task" not in page.locator("body").inner_text()
    assert "alice private unsent instruction" not in page.locator("body").inner_text()
    assert page.get_by_label("发送下一条指令").count() == 0
    assert session not in page.locator("body").inner_text()
    assert runtimes[0].prompts == []


def test_missing_task_link_keeps_catalog_actionable_and_existing_draft(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "valid-task-after-missing-link")
    page.get_by_label("发送下一条指令").fill("retain this unsent instruction")
    page.evaluate("location.hash = 'tasks/missing-session'")
    expect(page.get_by_role("alert")).to_contain_text("不存在")
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    task_rows(page).get_by_role("button", name="valid-task-after-missing-link", exact=True).click()
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_have_value("retain this unsent instruction")
    assert runtimes[0].prompts == []
