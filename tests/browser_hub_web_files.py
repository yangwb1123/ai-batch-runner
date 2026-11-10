"""Real Chrome/file capture/HTTP/SQLite; only provider and issuer are synthetic."""

from dataclasses import replace

import pytest
from playwright.sync_api import expect

from pbatch.hub_file_snapshot_store import FileSnapshotStore
from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from browser_hub_web_results import results
from test_hub_auth import signed
from test_hub_web import web_env

LONG_FILE = "report-" + "x" * 200 + ".txt"
SHARED = {"share_files": ("report.html", "empty.txt", "binary.bin", "missing.txt", LONG_FILE)}
CONTENT = b'<script>window.snapshotScriptExecuted = true</script>\nlocal file contents\n'


def completed_files(page, store, runtimes, tmp_path, name="file-check"):
    project = tmp_path / "project"
    (project / "report.html").write_bytes(CONTENT)
    (project / "empty.txt").write_bytes(b"")
    (project / "binary.bin").write_bytes(b"\x00\xff\x01")
    (project / LONG_FILE).write_text("long filename snapshot")
    session = create_task(page, store, name)
    turn = send_prompt(page, store, runtimes, session, "inspect declared files")
    complete_turn(runtimes[-1], "Fixture provider ended; inspect workspace bytes separately.")
    snapshots = FileSnapshotStore(store)
    until(lambda: snapshots.read("team", session, turn)["state"] == "partial")
    results(page)
    panel = page.get_by_role("region", name="文件检查快照", exact=True)
    expect(panel.get_by_role("button", name="刷新文件快照", exact=True)).to_be_enabled()
    panel.get_by_role("button", name="刷新文件快照", exact=True).click()
    expect(panel.get_by_role("button", name="检查 report.html", exact=True)).to_be_enabled()
    return session, turn, panel


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_files_preview_and_download_exact_bytes_without_executing_html(task_page, tmp_path):
    page, _, store, runtimes = task_page
    _, _, panel = completed_files(page, store, runtimes, tmp_path)
    panel.get_by_role("button", name="检查 report.html", exact=True).click()
    preview = panel.get_by_role("region", name="文件内容预览", exact=True)
    expect(preview).to_contain_text(CONTENT.decode())
    assert page.evaluate("window.snapshotScriptExecuted === undefined")
    button = panel.get_by_role("button", name="检查 " + LONG_FILE, exact=True)
    assert button.bounding_box()["width"] <= panel.bounding_box()["width"]
    expect(panel).to_contain_text("missing.txt：文件不存在")
    expect(preview).to_contain_text("目标验收仍未验证")
    (tmp_path / "project" / "report.html").write_text("changed after immutable capture")
    with page.expect_download(timeout=5000) as pending:
        panel.get_by_role("button", name="下载 report.html", exact=True).click()
    download = pending.value
    assert download.suggested_filename == "report.html"
    destination = tmp_path / "downloaded-report"
    download.save_as(destination)
    assert destination.read_bytes() == CONTENT
    assert page.evaluate("window.snapshotScriptExecuted === undefined")


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_empty_and_binary_files_and_historical_selection(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, first, panel = completed_files(page, store, runtimes, tmp_path)
    panel.get_by_role("button", name="检查 empty.txt", exact=True).click()
    expect(panel).to_contain_text("这是一个空文件（0 字节）")
    panel.get_by_role("button", name="检查 binary.bin", exact=True).click()
    expect(panel).to_contain_text("二进制文件仅提供原始字节下载")
    with page.expect_download(timeout=5000) as pending:
        panel.get_by_role("button", name="下载 binary.bin", exact=True).click()
    pending.value.save_as(tmp_path / "binary-download")
    assert (tmp_path / "binary-download").read_bytes() == b"\x00\xff\x01"
    (tmp_path / "project" / "report.html").write_text("second turn file")
    second = send_prompt(page, store, runtimes, session, "second file snapshot")
    complete_turn(runtimes[0], "second provider turn")
    until(lambda: FileSnapshotStore(store).read("team", session, second)["state"] == "partial")
    expect(panel.get_by_role("region", name="文件内容预览")).to_have_count(0)
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    page.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    results(page)
    panel.get_by_role("button", name="刷新文件快照", exact=True).click()
    panel.get_by_role("button", name="检查 report.html", exact=True).click()
    expect(panel.get_by_role("region", name="文件内容预览")).to_contain_text("local file contents")
    expect(panel.get_by_role("region", name="文件内容预览")).not_to_contain_text("second turn file")


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_real_scope_revocation_prevents_cached_file_download(task_page, tmp_path, web_env):
    page, _, store, runtimes = task_page
    _, _, panel = completed_files(page, store, runtimes, tmp_path)
    panel.get_by_role("button", name="检查 report.html", exact=True).click()
    expect(panel.get_by_role("region", name="文件内容预览")).to_contain_text("local file contents")
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    auth = web_env[1]
    auth.grants = [replace(grant, scopes=grant.scopes - {"agent.artifacts:read"}) for grant in auth.grants]
    panel.get_by_role("button", name="下载 report.html", exact=True).click()
    expect(panel).to_contain_text("文件读取权限或登录已变化")
    expect(panel.get_by_role("region", name="文件内容预览")).to_have_count(0)
    assert not downloads


@pytest.mark.parametrize("controlled_hub", [SHARED], indirect=True)
def test_tampered_response_is_never_previewed_or_downloaded(task_page, tmp_path):
    page, _, store, runtimes = task_page
    _, _, panel = completed_files(page, store, runtimes, tmp_path)

    def corrupt(route):
        response = route.fetch()
        body = response.json()
        body["data"]["content_b64"] = "Zm9yZ2Vk"
        route.fulfill(response=response, json=body)

    page.route("**/file-snapshots/*", corrupt)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    panel.get_by_role("button", name="下载 report.html", exact=True).click()
    expect(panel).to_contain_text("校验失败，已拒绝预览和下载")
    expect(panel.get_by_role("region", name="文件内容预览")).to_have_count(0)
    assert not downloads
