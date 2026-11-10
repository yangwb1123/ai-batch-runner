"""Local draft files in real Chrome/Hub; identity and provider are synthetic fixtures."""

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from browser_hub_web_control import confirm_operation
from test_hub_auth import signed
from test_hub_web import web_env


TEXT = ("目标：只核对本次失败并补充回归。\n允许范围：现有项目。\n验收：保留真实结果。\n"
        "<script>window.draftScriptExecuted = true</script>\n"
        "<img src=https://probe.invalid/draft onerror=window.draftScriptExecuted=true>\n"
        "user-written-private-fixture-text\n")


def draft_files(page):
    summary = page.locator("summary").filter(has_text=re.compile(r"^草稿文件$"))
    expect(summary).to_be_visible()
    if summary.locator("..").get_attribute("open") is None:
        summary.click()
    return summary.locator("..")


def choose_text(page, text, name="draft.txt"):
    panel = draft_files(page)
    chooser = panel.get_by_label("选择草稿文本文件", exact=True)
    expect(chooser).to_be_enabled()
    chooser.set_input_files(
        {"name": name, "mimeType": "text/plain", "buffer": text.encode("utf-8")})
    preview = page.get_by_label("待加入的草稿文本", exact=True)
    expect(preview).to_be_visible()
    assert preview.text_content() == text
    return preview


def save_text(page, tmp_path, filename="saved-draft.txt"):
    with page.expect_download(timeout=5000) as pending:
        draft_files(page).get_by_role("button", name="保存草稿文本", exact=True).click()
    download = pending.value
    assert download.suggested_filename.endswith(".txt")
    target = tmp_path / filename
    download.save_as(target)
    return target


def collect_writes(page):
    writes = []
    page.on("request", lambda request: writes.append((request.url, request.post_data))
            if request.method == "POST" else None)
    return writes


def screenshot_drafts(page, tmp_path):
    target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    draft_files(page).scroll_into_view_if_needed()
    page.screenshot(path=str(target / f"draft-files-{page.viewport_size['width']}.png"))


def finish_explicit_send(page, store, runtimes, session, text):
    page.get_by_role("button", name="发送指令", exact=True).click()
    until(lambda: runtimes[0].prompts == [text] and bool(store.session("team", session)["active_turn_id"]))
    turn = store.session("team", session)["active_turn_id"]
    complete_turn(runtimes[0], "explicit imported draft request completed")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("explicit imported draft request completed")


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_draft_backup_reload_preview_append_and_explicit_send(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "draft-file-roundtrip")
    composer = page.get_by_label("发送下一条指令")
    composer.fill(TEXT)
    writes, external = collect_writes(page), []
    page.on("request", lambda request: external.append(request.url) if "probe.invalid" in request.url else None)
    backup = save_text(page, tmp_path)
    assert backup.read_bytes() == TEXT.encode("utf-8")  # Only the editor text, without added metadata.
    page.reload()
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))
    expect(composer).to_be_enabled()
    expect(composer).to_have_value("")
    chooser = draft_files(page).get_by_label("选择草稿文本文件", exact=True)
    expect(chooser).to_be_enabled()
    chooser.set_input_files(backup)
    preview = page.get_by_label("待加入的草稿文本", exact=True)
    expect(preview).to_be_visible()
    assert preview.text_content() == TEXT
    latest = "Keep the newer manual scope and acceptance criteria."
    composer.fill(latest)
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("button", name="刷新任务现场", exact=True)).to_be_enabled()
    assert preview.text_content() == TEXT
    assert preview.locator("script, img, iframe").count() == 0
    assert page.evaluate("window.draftScriptExecuted === undefined") and external == []
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    screenshot_drafts(page, tmp_path)
    page.get_by_role("button", name="加入当前草稿", exact=True).evaluate("button => { button.click(); button.click() }")
    expected = latest + "\n\n" + TEXT
    expect(composer).to_have_value(expected)
    expect(composer).to_be_focused()
    expect(preview).to_have_count(0)
    expect(page.get_by_role("status").filter(has_text="草稿文本已加入当前草稿")).to_be_visible()
    assert writes == [] and runtimes[0].prompts == []
    finish_explicit_send(page, store, runtimes, session, expected)
    assert len(writes) == 1 and writes[0][0].endswith(f"/sessions/{session}/turns")
    assert runtimes[0].prompts == [expected]


def test_full_draft_capacity_roundtrips_bom_and_crlf_but_still_cannot_send(task_page, tmp_path):
    page, _, store, runtimes = task_page
    create_task(page, store, "large-draft-file")
    raw = b"\xef\xbb\xbf" + b"A" * 65529 + b"\r\nB\n"
    assert len(raw) == 65536
    text = raw.decode("utf-8")
    writes = collect_writes(page)
    choose_text(page, text)
    page.get_by_role("button", name="加入当前草稿", exact=True).click()
    composer = page.get_by_label("发送下一条指令")
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    assert save_text(page, tmp_path).read_bytes() == raw
    composer.press("Control+End")
    composer.press("Backspace")
    composer.press("Z")
    expected = text.replace("\r\n", "\n")[:-1] + "Z"
    assert composer.input_value() == expected
    assert save_text(page, tmp_path, "edited-draft.txt").read_bytes() == expected.encode("utf-8")
    expect(page.get_by_role("button", name="发送指令", exact=True)).to_be_disabled()
    composer.press("Control+Enter")
    assert writes == [] and runtimes[0].prompts == []


def test_invalid_local_draft_files_preserve_existing_text_without_uploading(task_page):
    page, _, store, runtimes = task_page
    create_task(page, store, "invalid-draft-files")
    original = "preserve this current scope"
    page.get_by_label("发送下一条指令").fill(original)
    writes = collect_writes(page)
    files = [("oversize.txt", b"x" * 65537, "65536"), ("invalid.txt", b"\xff", "UTF-8"),
             ("nul.txt", b"not\x00text", "控制"), ("bidi.txt", "left\u202eright".encode("utf-8"), "控制"),
             ("empty.txt", b"", "非空")]
    for name, content, message in files:
        chooser = draft_files(page).get_by_label("选择草稿文本文件", exact=True)
        expect(chooser).to_be_enabled()
        chooser.set_input_files(
            {"name": name, "mimeType": "text/plain", "buffer": content})
        expect(page.get_by_role("alert").filter(has_text=message)).to_be_visible()
        expect(page.get_by_label("待加入的草稿文本", exact=True)).to_have_count(0)
        expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    assert writes == [] and runtimes[0].prompts == []


def test_import_append_over_capacity_preserves_both_texts_and_can_be_retried(task_page):
    page, _, store, runtimes = task_page
    create_task(page, store, "draft-append-budget")
    original = "x" * 65535
    composer = page.get_by_label("发送下一条指令")
    composer.fill(original)
    writes = collect_writes(page)
    preview = choose_text(page, "y")
    page.get_by_role("button", name="加入当前草稿", exact=True).click()
    expect(draft_files(page).get_by_role("alert").filter(has_text="65536")).to_be_visible()
    expect(composer).to_have_value(original)
    assert preview.text_content() == "y"
    composer.fill("")
    page.get_by_role("button", name="加入当前草稿", exact=True).click()
    expect(composer).to_have_value("y")
    expect(preview).to_have_count(0)
    assert writes == [] and runtimes[0].prompts == []


def hold_local_file_read(page):
    """Delay delivery from a real File stream; keep the original bytes, size and reader cancellation."""
    page.evaluate("""() => {
      const stream = File.prototype.stream;
      const gate = window.draftReadGate = { held: [], cancelled: 0, released: 0, reasons: [] };
      gate.restore = () => { File.prototype.stream = stream };
      File.prototype.stream = function() {
        const reader = stream.call(this).getReader();
        return { getReader: () => ({
          read: () => reader.read().then(result => new Promise(resolve => {
            gate.held.push(() => { gate.released++; resolve(result) });
          })),
          cancel: reason => { gate.cancelled++; gate.reasons.push(String(reason)); return reader.cancel(reason) },
          releaseLock: () => reader.releaseLock(),
        }) };
      };
    }""")
    chooser = draft_files(page).get_by_label("选择草稿文本文件", exact=True)
    expect(chooser).to_be_enabled()
    chooser.set_input_files(
        {"name": "delayed.txt", "mimeType": "text/plain", "buffer": b"private late draft file"})
    page.wait_for_function("window.draftReadGate.held.length === 1", timeout=5000)


def release_local_file_read(page):
    assert page.evaluate("window.draftReadGate.cancelled") == 1
    assert page.evaluate("window.draftReadGate.reasons") == ["Error: 草稿文件读取已取消。"]
    page.evaluate("""async () => {
      const gate = window.draftReadGate;
      gate.restore();
      for (const release of gate.held.splice(0)) release();
      await Promise.resolve(); await Promise.resolve();
    }""")
    assert page.evaluate("window.draftReadGate.released") == 1
    expect(page.get_by_label("待加入的草稿文本", exact=True)).to_have_count(0)
    assert "private late draft file" not in page.locator("body").inner_text()


def test_navigation_cancels_late_local_read_without_crossing_session_drafts(task_page):
    page, _, store, runtimes = task_page
    first = create_task(page, store, "draft-import-first")
    page.get_by_label("发送下一条指令").fill("first existing draft")
    second = create_task(page, store, "draft-import-second")
    page.get_by_label("发送下一条指令").fill("second existing draft")
    page.evaluate("hash => { location.hash = hash }", f"tasks/{first}")
    expect(page.get_by_label("发送下一条指令")).to_have_value("first existing draft")
    writes = collect_writes(page)
    hold_local_file_read(page)
    page.evaluate("hash => { location.hash = hash }", f"tasks/{second}")
    expect(page.get_by_label("发送下一条指令")).to_have_value("second existing draft")
    release_local_file_read(page)
    expect(page.get_by_label("发送下一条指令")).to_have_value("second existing draft")
    page.evaluate("hash => { location.hash = hash }", f"tasks/{first}")
    expect(page.get_by_label("发送下一条指令")).to_have_value("first existing draft")
    assert writes == [] and all(runtime.prompts == [] for runtime in runtimes)


@pytest.mark.parametrize("boundary", ["revocation", "pagehide"])
def test_identity_boundaries_discard_late_file_read_and_editor_content(task_page, web_env, boundary):
    page, _, store, runtimes = task_page
    create_task(page, store, "draft-import-boundary")
    page.get_by_label("发送下一条指令").fill("private original editor text")
    writes = collect_writes(page)
    hold_local_file_read(page)
    if boundary == "revocation":
        web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
        page.get_by_role("button", name="刷新任务现场", exact=True).click()
        expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
    else:
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    expect(page.get_by_label("发送下一条指令")).to_have_count(0)
    release_local_file_read(page)
    assert "private original editor text" not in page.locator("body").inner_text()
    if boundary == "pagehide":
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        expect(page.get_by_label("发送下一条指令")).to_have_value("")
        expect(page.get_by_label("待加入的草稿文本", exact=True)).to_have_count(0)
    assert writes == [] and runtimes[0].prompts == []


def test_unknown_submission_disables_files_and_preserves_original_body_and_key(task_page):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "unknown-draft-import")
    choose_text(page, "unsubmitted file text")
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
    original = "submit exactly this current instruction once"
    turn = send_prompt(page, store, runtimes, session, original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    panel = draft_files(page)
    expect(panel.get_by_role("button", name="保存草稿文本", exact=True)).to_be_disabled()
    expect(panel.get_by_label("选择草稿文本文件", exact=True)).to_be_disabled()
    apply = panel.get_by_role("button", name="加入当前草稿", exact=True)
    if apply.count():
        expect(apply).to_be_disabled()
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    complete_turn(runtimes[0], "unknown request executed once")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts == [original]


def test_closed_session_can_back_up_retained_editor_but_cannot_import(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "closed-draft-backup")
    original = "keep this unsent text after intentionally closing the session"
    page.get_by_label("发送下一条指令").fill(original)
    confirm_operation(page, "关闭空闲会话")
    until(lambda: store.session("team", session)["status"] == "closed")
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    writes = collect_writes(page)
    panel = draft_files(page)
    expect(panel.get_by_role("button", name="保存草稿文本", exact=True)).to_be_enabled()
    expect(panel.get_by_label("选择草稿文本文件", exact=True)).to_be_disabled()
    assert save_text(page, tmp_path).read_bytes() == original.encode("utf-8")
    assert writes == [] and runtimes[0].prompts == []
