"""Native comparison in real Chrome/Hub/Gateway; issuer and provider are fixtures."""

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import expect

from pbatch.hub_file_snapshot_store import FileSnapshotStore
from browser_hub_web_support import (
    controlled_hub, task_page, create_task, send_prompt, complete_turn, until,
)
from browser_hub_web_results import results, assert_no_private_keys
from test_hub_auth import signed
from test_hub_web import web_env

SHARED = {"share_files": ("report.html", "empty.txt", "binary.bin", "missing.txt")}
BEFORE = '\ufeff<script>window.diffScriptExecuted = true</script>\r\n\u202e\r\ntail'.encode()
AFTER = b'<script>window.diffScriptExecuted = true</script>\n\\u{202e}\ntail\n'
ENDINGS = {"LF": "\n", "CRLF": "\r\n", "CR": "\r", "none": ""}


def capture_turn(page, store, runtimes, session, project, content, label):
    (project / "report.html").write_bytes(content)
    turn = send_prompt(page, store, runtimes, session, label)
    complete_turn(runtimes[-1], "Fixture turn finished; file integrity is checked separately.")
    snapshots = FileSnapshotStore(store)
    until(lambda: snapshots.read("team", session, turn)["state"] in {"ready", "partial"})
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    return turn


def comparison_pair(task_page, tmp_path, before=BEFORE, after=AFTER):
    page, _, store, runtimes = task_page
    project = tmp_path / "project"
    (project / "empty.txt").write_bytes(b"")
    (project / "binary.bin").write_bytes(b"\x00\xff\x01")
    session = create_task(page, store, "native-file-comparison")
    first = capture_turn(page, store, runtimes, session, project, before, "first file version")
    (project / "empty.txt").write_bytes(b"now nonempty\n")
    (project / "binary.bin").write_bytes(b"\x00\xff\x02")
    (project / "missing.txt").write_bytes(b"")
    second = capture_turn(page, store, runtimes, session, project, after, "second file version")
    results(page)
    panel = page.get_by_role("region", name="文件版本对比", exact=True)
    expect(panel.get_by_role("button", name="选择对比轮次", exact=True)).to_be_enabled()
    return session, first, second, panel


def select_baseline(panel, first):
    panel.get_by_role("button", name="选择对比轮次", exact=True).click()
    panel.get_by_label("基准轮次", exact=True).select_option(first)
    expect(panel.get_by_role("button", name="比较 report.html", exact=True)).to_be_enabled()


def compare_file(panel, path="report.html"):
    panel.get_by_role("button", name="比较 " + path, exact=True).click()
    output = panel.get_by_role("region", name="文件差异", exact=True)
    expect(output).to_be_visible()
    return output


def comparison_record(page, panel, tmp_path):
    with page.expect_download(timeout=5000) as pending:
        panel.get_by_role("button", name="下载差异记录", exact=True).click()
    download = pending.value
    assert download.suggested_filename.endswith(".json")
    destination = tmp_path / "comparison-record.json"
    download.save_as(destination)
    raw = destination.read_bytes()
    assert len(raw) <= 2 * 1024 * 1024
    record = json.loads(raw)
    assert record["schema"] == "abr.file-comparison.v1"
    assert record["verification"] == "content_integrity_only"
    assert_no_private_keys(record)
    return record


def reconstruct(rows, side):
    excluded = "add" if side == "before" else "remove"
    return "".join(row["text"] + ENDINGS[row["ending"]] for row in rows
                   if row["kind"] != excluded).encode("utf-8")


def save_comparison_screenshot(page, output, tmp_path):
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    output.scroll_into_view_if_needed()
    page.screenshot(path=str(target / f"native-comparison-{page.viewport_size['width']}.png"), full_page=True)


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_native_diff_preserves_raw_bytes_and_exports_immutable_versions(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, first, second, panel = comparison_pair(task_page, tmp_path)
    select_baseline(panel, first)
    button = panel.get_by_role("button", name="比较 report.html", exact=True)
    button.focus()
    page.keyboard.press("Enter")
    output = panel.get_by_role("region", name="文件差异", exact=True)
    expect(output).to_contain_text("字节不同")
    expect(output).to_contain_text("CRLF")
    expect(output).to_contain_text("BOM")
    expect(output).to_contain_text("\\u{202e}")
    assert output.locator("script, img, iframe").count() == 0
    assert page.evaluate("window.diffScriptExecuted === undefined")
    (tmp_path / "project" / "report.html").write_text("changed after both immutable captures")
    page.get_by_label("发送下一条指令").fill("unsent-private-comparison-draft")
    reads, writes = [], []
    page.on("request", lambda request: reads.append(request.url)
            if "/file-snapshots/" in request.url else None)
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    record = comparison_record(page, panel, tmp_path)
    assert record["path"] == "report.html" and record["byte_equal"] is False
    assert record["before"]["turn_id"] == first and record["after"]["turn_id"] == second
    assert record["before"]["sha256"] == hashlib.sha256(BEFORE).hexdigest()
    assert record["after"]["sha256"] == hashlib.sha256(AFTER).hexdigest()
    assert reconstruct(record["result"]["rows"], "before") == BEFORE
    assert reconstruct(record["result"]["rows"], "after") == AFTER
    assert len(reads) == 2 and all(f"/sessions/{session}/" in url for url in reads)
    assert {first, second} == {url.split("/turns/")[1].split("/")[0] for url in reads}
    assert writes == [] and len(runtimes[0].prompts) == 2
    assert "unsent-private-comparison-draft" not in json.dumps(record)
    assert store.session("team", session)["active_turn_id"] == ""
    save_comparison_screenshot(page, output, tmp_path)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    expect(panel.get_by_role("region", name="文件差异", exact=True)).to_have_count(0)


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_empty_snapshot_is_distinct_from_missing_and_binary_diff(task_page, tmp_path):
    _, first, _, panel = comparison_pair(task_page, tmp_path, b"same\n", b"same\n")
    select_baseline(panel, first)
    expect(panel).to_contain_text("不能推断文件新增或删除")
    expect(panel.get_by_role("button", name="比较 missing.txt", exact=True)).to_be_disabled()
    expect(compare_file(panel)).to_contain_text("字节相同")
    expect(compare_file(panel, "empty.txt")).to_contain_text("增加 1 行")
    output = compare_file(panel, "binary.bin")
    expect(output).to_contain_text("二进制")
    expect(output).to_contain_text("字节不同")
    record = comparison_record(task_page[0], panel, tmp_path)
    assert record["byte_equal"] is False and record["path"] == "binary.bin"
    assert record["before"]["size_bytes"] == record["after"]["size_bytes"] == 3


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
@pytest.mark.parametrize("side", ["before", "after"])
def test_diff_rejects_tampered_bytes_from_either_turn(task_page, tmp_path, side):
    page, _, _, _ = task_page
    _, first, second, panel = comparison_pair(task_page, tmp_path)
    select_baseline(panel, first)
    target = first if side == "before" else second

    def corrupt(route):
        response = route.fetch()
        body = response.json()
        body["data"]["content_b64"] = "Zm9yZ2Vk"
        route.fulfill(response=response, json=body)

    page.route(f"**/turns/{target}/file-snapshots/*", corrupt)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    panel.get_by_role("button", name="比较 report.html", exact=True).click()
    expect(panel).to_contain_text("校验失败，已拒绝预览和下载")
    expect(panel.get_by_role("region", name="文件差异", exact=True)).to_have_count(0)
    expect(panel.get_by_role("button", name="下载差异记录", exact=True)).to_have_count(0)
    assert not downloads


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_instance_revocation_with_unchanged_scopes_blocks_cached_diff_export(task_page, tmp_path, web_env):
    page, _, _, _ = task_page
    _, first, _, panel = comparison_pair(task_page, tmp_path)
    select_baseline(panel, first)
    expect(compare_file(panel)).to_contain_text("字节不同")
    files = page.get_by_role("region", name="文件检查快照", exact=True)
    files.get_by_role("button", name="刷新文件快照", exact=True).click()
    files.get_by_role("button", name="检查 report.html", exact=True).click()
    expect(files.get_by_role("region", name="文件内容预览", exact=True)).to_be_visible()
    auth = web_env[1]
    old_user = next(grant for grant in auth.grants if grant.kind == "user")
    auth.grants = [replace(grant, instances=frozenset({"other-instance"}))
                   if grant.kind == "user" else grant for grant in auth.grants]
    assert next(grant for grant in auth.grants if grant.kind == "user").scopes == old_user.scopes
    access = page.request.get(page.url.split("#")[0] + "web/api/access")
    assert access.status == 200 and access.json()["data"]["file_snapshots"] is True
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    panel.get_by_role("button", name="下载差异记录", exact=True).click()
    expect(panel).to_contain_text("文件读取权限或登录已变化")
    expect(panel.get_by_role("region", name="文件差异", exact=True)).to_have_count(0)
    expect(files.get_by_role("region", name="文件内容预览", exact=True)).to_have_count(0)
    assert not downloads


def seed_intermediate_history(store, first, second):
    """Insert explicit read-only ledger fixtures, without running extra agent turns."""
    with store._write() as conn:
        base = conn.execute("SELECT * FROM hub_turns WHERE turn_id=?", (first,)).fetchone()
        latest = conn.execute("SELECT created_at FROM hub_turns WHERE turn_id=?", (second,)).fetchone()[0]
        for index in range(23):
            stamp = base["created_at"] + (latest - base["created_at"]) * (index + 1) / 24
            conn.execute("INSERT INTO hub_turns VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                f"history-fixture-{index:02d}", base["tenant"], base["session_id"], base["instance_id"],
                base["generation"], "history-fixture", "Fixture history only; no execution", f"history-{index}",
                "completed", stamp, stamp))


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_comparison_finds_baseline_beyond_twenty_without_changing_main_turn_or_draft(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, first, second, panel = comparison_pair(task_page, tmp_path)
    seed_intermediate_history(store, first, second)
    draft = "keep this instruction while comparing older files"
    page.get_by_label("发送下一条指令").fill(draft)
    panel.get_by_role("button", name="选择对比轮次", exact=True).click()
    choice = panel.get_by_label("基准轮次", exact=True)
    expect(choice.locator(f'option[value="{first}"]')).to_have_count(0)
    expect(choice.locator("option")).to_have_count(20)
    panel.get_by_role("button", name="较早一页", exact=True).click()
    choice.select_option(first)
    expect(compare_file(panel)).to_contain_text("字节不同")
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)
    record = comparison_record(page, panel, tmp_path)
    assert record["before"]["turn_id"] == first and record["after"]["turn_id"] == second
    panel.get_by_role("button", name="返回上一页", exact=True).click()
    expect(panel.get_by_role("region", name="文件差异", exact=True)).to_have_count(0)
    expect(choice).to_have_value("")
    assert len(runtimes[0].prompts) == 2


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_leaving_results_discards_inflight_comparison(task_page, tmp_path):
    page, _, _, _ = task_page
    _, first, second, panel = comparison_pair(task_page, tmp_path)
    select_baseline(panel, first)
    held = []

    def hold(route):
        held.append((route, route.fetch()))

    pattern = f"**/turns/{second}/file-snapshots/*"
    page.route(pattern, hold)
    with page.expect_request(lambda request: f"/turns/{second}/file-snapshots/" in request.url):
        panel.get_by_role("button", name="比较 report.html", exact=True).click()
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    page.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    assert len(held) == 1
    held[0][0].fulfill(response=held[0][1])
    results(page)
    expect(panel.get_by_role("region", name="文件差异", exact=True)).to_have_count(0)
    expect(panel.get_by_role("button", name="下载差异记录", exact=True)).to_have_count(0)
    expect(panel.get_by_label("基准轮次", exact=True)).to_have_count(0)


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_line_budget_reports_incomplete_diff_without_fabricating_equality(task_page, tmp_path):
    before = b"same\n" * 2001
    after = b"changed\n" + b"same\n" * 2000
    _, first, _, panel = comparison_pair(task_page, tmp_path, before, after)
    select_baseline(panel, first)
    output = compare_file(panel)
    expect(output).to_contain_text("字节不同")
    expect(output).to_contain_text("2000")
    record = comparison_record(task_page[0], panel, tmp_path)
    assert record["result"]["state"] == "limited"
    assert record["result"]["reason"] == "lines"
    assert record["result"]["added"] is None and record["result"]["removed"] is None
    assert record["result"]["rows"] == [] and record["byte_equal"] is False
