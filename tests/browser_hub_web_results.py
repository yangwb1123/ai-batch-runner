"""Result inspection in real Chrome; only issuer and provider are synthetic."""

import json
import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from test_hub_auth import signed
from test_hub_web import web_env


def results(page):
    tab = page.get_by_role("tab", name="结果检查", exact=True)
    expect(tab).to_be_visible()
    tab.click()
    return page.get_by_role("tabpanel", name="结果检查", exact=True)


def download_record(page, tmp_path):
    with page.expect_download(timeout=5000) as pending:
        page.get_by_role("button", name="下载当前轮次记录", exact=True).click()
    download = pending.value
    assert download.suggested_filename.endswith(".json")
    path = tmp_path / download.suggested_filename
    download.save_as(path)
    raw = path.read_text(encoding="utf-8")
    assert len(raw.encode("utf-8")) <= 2 * 1024 * 1024
    record = json.loads(raw)
    assert record["schema"] == "abr.turn-record.v1"
    assert {"session", "turn", "events", "replies", "coverage", "verification"} <= set(record)
    assert record["coverage"]["scope"] == "currently_loaded_events"
    assert record["verification"]["goal_acceptance"] == "not_independently_verified"
    assert record["verification"]["file_contents"] == "not_read_or_verified"
    return record, raw


def assert_no_private_keys(value):
    forbidden = {"access", "context", "token", "access_token", "refresh_token", "cookie",
                 "authorization", "pending", "draft", "drafts", "idempotency_key"}
    if isinstance(value, dict):
        assert not forbidden.intersection(key.lower() for key in value)
        for nested in value.values():
            assert_no_private_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_no_private_keys(nested)


def emit_result_events(runtime):
    command = "python -m pytest tests/test_example.py"
    runtime.emit("command.started", {"item_id": "command-one", "command": command})
    runtime.emit("command.completed", {"item_id": "command-one", "command": command, "exit_code": 0})
    runtime.emit("file.changed", {"item_id": "file-one", "changes": [{"path": "src/example.py"}]})
    runtime.emit("validation.started", {"item_id": "validation-one", "name": "unit-tests"})
    runtime.emit("validation.completed", {"item_id": "validation-one", "name": "unit-tests",
                                           "success": True, "validation_exit": 0})
    runtime.emit("gate.verdict", {"item_id": "gate-one", "gate": "review-gate", "verdict": "REJECT",
                                  "reason": "fixture acceptance remains unresolved"})
    runtime.emit("artifact.created", {"item_id": "artifact-one", "name": "fixture report",
                                      "path": "reports/result.json", "sha256": "a" * 64,
                                      "size_bytes": 123, "media_type": "application/json"})


@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_result_inspection_and_download_use_current_gateway_turn(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "inspect-results")
    turn = send_prompt(page, store, runtimes, session, "inspect typed execution evidence")
    emit_result_events(runtimes[0])
    complete_turn(runtimes[0], "Fixture reply: VERDICT: PASS is only model prose.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    panel = results(page)
    expect(panel).to_contain_text("python -m pytest tests/test_example.py")
    expect(panel).to_contain_text("src/example.py")
    expect(panel).to_contain_text("unit-tests")
    expect(panel).to_contain_text("fixture acceptance remains unresolved")
    expect(panel).to_contain_text("reports/result.json")
    commands = panel.get_by_role("region", name="命令记录", exact=True)
    expect(commands.get_by_text("python -m pytest tests/test_example.py", exact=True)).to_have_count(1)
    expect(commands).to_contain_text("退出码 0")
    expect(panel.get_by_role("region", name="门禁记录", exact=True)).to_contain_text("报告拒绝")
    assert panel.locator('a[href*="src/example.py"], a[href*="reports/result.json"]').count() == 0
    page.get_by_label("发送下一条指令").fill("unsent-private-draft-must-not-be-exported")
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    record, raw = download_record(page, tmp_path)
    assert record["session"]["session_id"] == session
    assert record["turn"]["turn_id"] == turn
    assert record["turn"]["state"] == "completed"
    assert {row["turn_id"] for row in record["events"]} == {turn}
    assert any(row["kind"] == "gate.verdict" and row["state"] == "rejected" for row in record["events"])
    assert "unsent-private-draft-must-not-be-exported" not in raw
    assert "VERDICT: PASS is only model prose" in raw
    assert_no_private_keys(record)
    assert writes == []


def test_results_keep_unknown_exit_and_missing_verification_distinct_from_prose(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "prose-is-not-evidence")
    turn = send_prompt(page, store, runtimes, session, "report commands without validator events")
    runtime = runtimes[0]
    runtime.emit("command.started", {"item_id": "unknown-code", "command": "unknown-code-command"})
    runtime.emit("command.completed", {"item_id": "unknown-code", "command": "unknown-code-command"})
    runtime.emit("command.completed", {"item_id": "failed-code", "command": "failed-command", "exit_code": 7})
    complete_turn(runtime, "All tests passed. VERDICT: PASS - this remains untrusted prose.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    panel = results(page)
    expect(panel).to_contain_text("unknown-code-command")
    expect(panel).to_contain_text("failed-command")
    expect(panel.get_by_role("region", name="命令记录", exact=True)).to_contain_text("退出码未提供")
    expect(panel.get_by_role("region", name="验证记录", exact=True)).to_contain_text("未提供验证记录")
    record, _ = download_record(page, tmp_path)
    completed = [row for row in record["events"] if row["kind"] == "command.completed"]
    assert sorted(row["exit_code"] for row in completed if row["exit_code"] is not None) == [7]
    assert sum(row["exit_code"] is None for row in completed) == 1
    assert not any(row["kind"].startswith("validation.") or row["kind"] == "gate.verdict"
                   for row in record["events"])


def test_result_download_and_keyboard_tabs_remain_bound_to_selected_history(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "history-results")
    first = send_prompt(page, store, runtimes, session, "first result instruction")
    complete_turn(runtimes[0], "first result body")
    until(lambda: store.turn("team", first)["state"] == "completed")
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    second = send_prompt(page, store, runtimes, session, "second result instruction")
    complete_turn(runtimes[0], "second result body")
    until(lambda: store.turn("team", second)["state"] == "completed")
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    page.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    conversation = page.get_by_role("tab", name="对话", exact=True)
    conversation.focus()
    page.keyboard.press("End")
    expect(page.get_by_role("tab", name="执行事件", exact=True)).to_be_focused()
    page.keyboard.press("Home")
    expect(conversation).to_be_focused()
    page.keyboard.press("ArrowRight")
    result_tab = page.get_by_role("tab", name="结果检查", exact=True)
    expect(result_tab).to_be_focused()
    expect(result_tab).to_have_attribute("aria-selected", "true")
    expect(conversation).to_have_attribute("tabindex", "-1")
    record, raw = download_record(page, tmp_path)
    assert record["turn"]["turn_id"] == first
    assert {row["turn_id"] for row in record["events"]} == {first}
    assert "first result body" in raw and "second result body" not in raw
    assert "first result instruction" in raw and "second result instruction" not in raw


def test_reply_code_copy_preserves_text_without_rendering_html_or_fetching_images(task_page):
    page, context, store, runtimes = task_page
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    session = create_task(page, store, "formatted-reply")
    turn = send_prompt(page, store, runtimes, session, "return formatted text safely")
    requested = []
    page.on("request", lambda request: requested.append(request.url) if "probe.invalid" in request.url else None)
    code = "print('exact code')\nprint('<safe>')\n"
    output = ("# Fixture summary\n\n- First item\n- Second item\n\n```python\n" + code +
              "```\n\n<img src=https://probe.invalid/pixel onerror=window.compromised=1>\n"
              "<script>window.compromised=2</script>\nIRIS_CODE_BLOCK_0_END")
    complete_turn(runtimes[0], output)
    until(lambda: store.turn("team", turn)["state"] == "completed")
    conversation = page.get_by_label("任务对话", exact=True)
    expect(conversation.get_by_role("heading", name="Fixture summary", exact=True)).to_be_visible()
    conversation.get_by_role("button", name="复制代码", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == code
    assert conversation.locator("img, script, iframe").count() == 0
    assert page.evaluate("window.compromised === undefined")
    assert requested == []
    conversation.get_by_role("button", name="查看原文", exact=True).click()
    expect(conversation).to_contain_text(output)
    conversation.get_by_role("button", name="复制回复", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == output
    conversation.get_by_role("button", name="格式预览", exact=True).click()
    expect(conversation.get_by_role("heading", name="Fixture summary", exact=True)).to_be_visible()


def test_permission_loss_removes_results_and_download_without_replaying_a_task(task_page):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "revoked-results")
    turn = send_prompt(page, store, runtimes, session, "inspect private execution results")
    runtimes[0].emit("file.changed", {"paths": ["private-result-path.txt"]})
    complete_turn(runtimes[0], "private result body")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(results(page)).to_contain_text("private-result-path.txt")
    expect(page.get_by_role("button", name="下载当前轮次记录", exact=True)).to_be_enabled()
    pattern = "**/web/api/**"
    context.route(pattern, lambda route: route.fulfill(status=403, content_type="application/json",
                                                       body='{"error":{"code":"forbidden"}}'))
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("权限")
    expect(page.get_by_role("tabpanel", name="结果检查", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="下载当前轮次记录", exact=True)).to_have_count(0)
    assert "private-result-path.txt" not in page.locator("body").inner_text()
    assert "private result body" not in page.locator("body").inner_text()
    assert runtimes[0].prompts == ["inspect private execution results"]


@pytest.mark.parametrize("controlled_hub", [True], indirect=True)
@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_opted_in_terminal_command_output_is_readable_copyable_and_exported(task_page, tmp_path):
    page, context, store, runtimes = task_page
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    session = create_task(page, store, "terminal-command-output")
    turn = send_prompt(page, store, runtimes, session, "diagnose command output")
    runtime = runtimes[0]
    runtime.emit("command.started", {"item_id": "codex", "command": "pytest failed-example.py"})
    runtime.emit("command.output", {"item_id": "codex", "delta": "DO-NOT-PUBLISH-DELTA"})
    text = "AssertionError: expected 2, got 1\npassword=OUTPUT-SECRET\n<script>window.outputUnsafe=1</script>"
    runtime.emit("command.output", {"item_id": "codex", "aggregated_output": text})
    runtime.emit("command.completed", {"item_id": "codex", "command": "pytest failed-example.py", "exit_code": 7})
    runtime.emit("command.completed", {"item_id": "appserver", "command": "verify build", "exit_code": 0,
                                       "aggregated_output": "Build finished successfully"})
    runtime.emit("command.completed", {"item_id": "empty", "command": "quiet command", "exit_code": 0,
                                       "aggregated_output": ""})
    runtime.emit("command.completed", {"item_id": "large", "command": "large output", "exit_code": 1,
                                       "aggregated_output": "日志" * 6000})
    complete_turn(runtime, "Inspect the failed command before continuing.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    panel = results(page)
    commands = panel.get_by_role("region", name="命令记录", exact=True)
    failed = commands.locator("li").filter(has=page.get_by_text("pytest failed-example.py", exact=True))
    expect(failed).to_contain_text("退出码 7")
    failed.locator("summary").click()
    expect(failed.get_by_label("命令输出正文", exact=True)).to_contain_text("AssertionError: expected 2, got 1")
    expected = "AssertionError: expected 2, got 1\npassword=[REDACTED]\n<script>window.outputUnsafe=1</script>"
    expect(failed.get_by_label("命令输出正文", exact=True)).to_have_text(expected)
    assert failed.locator("script, img, iframe").count() == 0
    assert page.evaluate("window.outputUnsafe === undefined")
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    failed.get_by_role("button", name="复制命令输出", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == expected
    empty = commands.locator("li").filter(has=page.get_by_text("quiet command", exact=True))
    empty.locator("summary").click()
    expect(empty).to_contain_text("执行端返回了空文本输出")
    large = commands.locator("li").filter(has=page.get_by_text("large output", exact=True))
    expect(large.locator("summary")).to_contain_text("不完整")
    record, raw = download_record(page, tmp_path)
    assert any(event.get("output_text") == expected for event in record["events"])
    assert any(event.get("output_text") == "Build finished successfully" for event in record["events"])
    assert record["coverage"]["command_output_partial"] is True
    assert "OUTPUT-SECRET" not in raw and "DO-NOT-PUBLISH-DELTA" not in raw
    assert writes == []
    failed.scroll_into_view_if_needed()


def test_command_output_remains_private_without_gateway_opt_in(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "private-command-output")
    turn = send_prompt(page, store, runtimes, session, "keep output private")
    runtimes[0].emit("command.output", {"item_id": "one", "aggregated_output": "PRIVATE-OUTPUT-SNAPSHOT"})
    runtimes[0].emit("command.completed", {"item_id": "one", "command": "run", "exit_code": 0,
                                           "aggregated_output": "PRIVATE-OUTPUT-COMPLETED"})
    complete_turn(runtimes[0], "Command finished.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    panel = results(page)
    expect(panel).to_contain_text("执行端未提供命令输出")
    assert panel.locator(".command-output").count() == 0
    _, raw = download_record(page, tmp_path)
    assert "PRIVATE-OUTPUT" not in raw


@pytest.mark.parametrize("controlled_hub", [True], indirect=True)
def test_command_output_isolated_between_turns_and_cleared_on_revocation(task_page):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "output-history")
    first = send_prompt(page, store, runtimes, session, "first output")
    runtimes[0].emit("command.completed", {"item_id": "same-id", "command": "first command",
                                           "exit_code": 1, "aggregated_output": "first-private-output"})
    complete_turn(runtimes[0], "First complete.")
    until(lambda: store.turn("team", first)["state"] == "completed")
    second = send_prompt(page, store, runtimes, session, "second output")
    runtimes[0].emit("command.completed", {"item_id": "same-id", "command": "second command",
                                           "exit_code": 0, "aggregated_output": "second-private-output"})
    complete_turn(runtimes[0], "Second complete.")
    until(lambda: store.turn("team", second)["state"] == "completed")
    panel = results(page)
    panel.locator("summary").click()
    expect(panel).to_contain_text("second-private-output")
    expect(panel).not_to_contain_text("first-private-output")
    page.get_by_role("tab", name="轮次记录", exact=True).click()
    page.get_by_role("button", name=f"查看轮次 {first}", exact=True).click()
    panel = results(page)
    panel.locator("summary").click()
    expect(panel).to_contain_text("first-private-output")
    expect(panel).not_to_contain_text("second-private-output")
    context.route("**/web/api/**", lambda route: route.fulfill(status=403, content_type="application/json",
                                                             body='{"error":{"code":"forbidden"}}'))
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("权限")
    assert "private-output" not in page.locator("body").inner_text()
    assert runtimes[0].prompts == ["first output", "second output"]
