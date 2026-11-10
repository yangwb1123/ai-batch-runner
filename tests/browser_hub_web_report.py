"""Readable local reports in real Chrome/TLS/Hub; issuer and provider are synthetic."""

import json
from dataclasses import replace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from browser_hub_web_results import results, assert_no_private_keys
from browser_hub_web_history import finish_prompt
from test_hub_auth import signed
from test_hub_web import web_env

REPORT = "下载本轮可读报告"
RECORD = "下载当前轮次记录"


def local_download(page, tmp_path, name, suffix):
    button = page.get_by_role("button", name=name, exact=True)
    expect(button).to_be_enabled()
    with page.expect_download(timeout=5000) as pending:
        calls = button.evaluate("""async button => {
          const original = window.fetch, calls = [];
          window.fetch = (...args) => { calls.push(String(args[0])); return original(...args); };
          try { button.click(); await Promise.resolve(); await Promise.resolve(); }
          finally { window.fetch = original; }
          return calls;
        }""")
    download = pending.value
    assert download.suggested_filename.endswith(suffix)
    destination = tmp_path / download.suggested_filename
    download.save_as(destination)
    data = destination.read_bytes()
    assert 0 < len(data) <= 2 * 1024 * 1024
    assert calls == [], "a synchronous local report must not request or page remote data"
    return data.decode("utf-8", errors="strict"), download.suggested_filename


def both_reports(page, tmp_path, session, turn):
    text, filename = local_download(page, tmp_path, REPORT, ".txt")
    raw, json_filename = local_download(page, tmp_path, RECORD, ".json")
    record = json.loads(raw)
    assert filename == f"abr-turn-{turn}.txt"
    assert json_filename == f"abr-turn-{turn}.json"
    assert record["schema"] == "abr.turn-record.v1"
    assert record["session"]["session_id"] == session
    assert record["turn"]["turn_id"] == turn
    assert {event["turn_id"] for event in record["events"]} == {turn}
    assert session in text and turn in text
    assert "目标验收：未独立验证" in text
    assert "文件内容：本报告未读取或验证" in text
    assert "范围：仅当前已读取事件" in text
    assert_no_private_keys(record)
    return text, raw, record


def completed_report(task_page, name="report-task", prompt="report original instruction", reply="report final reply"):
    page, _, store, runtimes = task_page
    session = create_task(page, store, name)
    turn = finish_prompt(page, store, runtimes, session, prompt, reply)
    expect(results(page)).to_contain_text("执行完成")
    expect(page.get_by_role("button", name=REPORT, exact=True)).to_be_enabled()
    return session, turn


def posts(page):
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
    return writes


def report_evidence(runtime):
    runtime.emit("command.completed", {"item_id": "report-command", "command": "pytest failed_report.py",
        "exit_code": 7, "aggregated_output": "report command output 中文\nassert expected == actual"})
    runtime.emit("validation.completed", {"item_id": "report-check", "name": "report validation",
        "success": False, "validation_exit": 7})
    runtime.emit("gate.verdict", {"item_id": "report-gate", "gate": "report acceptance gate",
        "verdict": "REJECT", "reason": "report acceptance remains unresolved"})
    runtime.emit("file.changed", {"paths": ["src/report-example.py"]})
    runtime.emit("artifact.created", {"item_id": "report-artifact", "name": "report artifact metadata",
        "path": "reports/local-result.json", "sha256": "b" * 64, "size_bytes": 123,
        "media_type": "application/json"})


@pytest.mark.parametrize("controlled_hub", [True], indirect=True)
@pytest.mark.parametrize("task_page", [(1440, 1000), (390, 844)], indirect=True)
def test_text_and_json_reports_preserve_current_evidence_without_private_state(task_page, tmp_path):
    page, context, store, runtimes = task_page
    session = create_task(page, store, "readable-report")
    prompt = "Review report instruction 中文 and preserve factual failure."
    reply = "Report reply: VERDICT: PASS is untrusted prose."
    turn = send_prompt(page, store, runtimes, session, prompt)
    report_evidence(runtimes[0])
    complete_turn(runtimes[0], reply)
    until(lambda: store.turn("team", turn)["state"] == "completed")
    expect(results(page)).to_contain_text("report acceptance remains unresolved")
    draft = "PRIVATE-UNSENT-REPORT-DRAFT"
    page.get_by_label("发送下一条指令").fill(draft)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(page.url))
    access = context.request.get(origin + "/web/api/access", timeout=5000).json()["data"]
    secrets = [draft, access["context"]] + [cookie["value"] for cookie in context.cookies() if cookie["httpOnly"]]
    writes = posts(page)
    text, raw, record = both_reports(page, tmp_path, session, turn)
    for value in [prompt, reply, "pytest failed_report.py", "report command output 中文", "assert expected == actual",
                  "report validation", "failed", "report acceptance gate", "rejected",
                  "report acceptance remains unresolved", "src/report-example.py", "reports/local-result.json",
                  "b" * 64, "123", "application/json"]:
        assert value in text and value in raw
    assert record["turn"]["state"] == "completed"
    for secret in secrets:
        assert secret not in text and secret not in raw
    assert writes == [] and runtimes[0].prompts == [prompt]
    expect(page.get_by_label("发送下一条指令")).to_have_value(draft)


@pytest.mark.parametrize("controlled_hub", [True], indirect=True)
def test_text_report_keeps_loaded_commands_beyond_ui_count_and_output_limits(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "long-readable-report")
    turn = send_prompt(page, store, runtimes, session, "retain every loaded command snapshot")
    outputs = [f"report-output-{index:02d}:" + "中" * 1700 + f":tail-{index:02d}" for index in range(70)]
    for index, output in enumerate(outputs):
        runtimes[0].emit("command.completed", {"item_id": f"report-item-{index}",
            "command": f"report-command-{index:02d}", "exit_code": 0, "aggregated_output": output})
    complete_turn(runtimes[0], "All loaded command snapshots remain available for local export.")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    panel = results(page)
    expect(panel).to_contain_text("执行完成")
    expect(panel).to_contain_text("展示前 64 条")
    expect(panel.get_by_role("region", name="命令记录", exact=True)).not_to_contain_text("report-command-69")
    writes = posts(page)
    text, _, record = both_reports(page, tmp_path, session, turn)
    exported = [event for event in record["events"] if event["kind"] == "command.completed"]
    assert len(exported) == 70 and len(record["events"]) > 64
    assert sum(len(event["output_text"].encode()) for event in exported) > 262144
    assert record["coverage"]["command_output_display_limited"] is True
    assert record["coverage"]["command_output_partial"] is False
    assert record["coverage"]["more_events_available"] is False
    assert record["coverage"]["earlier_events_dropped"] is False
    for index, output in enumerate(outputs):
        assert output in text and exported[index]["output_text"] == output
        assert f"report-command-{index:02d}" in text
    assert writes == []


def test_fixed_report_and_same_tick_route_change_cannot_export_the_wrong_turn(task_page, tmp_path):
    page, _, store, runtimes = task_page
    session, first = completed_report(task_page, prompt="first-report-prompt", reply="first-report-only-body")
    second = finish_prompt(page, store, runtimes, session, "second-report-prompt", "second-report-only-body")
    results(page)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    button = page.get_by_role("button", name=REPORT, exact=True)
    expect(button).to_be_enabled()
    button.evaluate("(button, hash) => { location.hash = hash; button.click(); }", f"tasks/{session}/turns/{first}")
    expect(page.get_by_role("region", name="轮次查看模式", exact=True)).to_contain_text("固定查看此轮")
    page.get_by_role("tab", name="对话", exact=True).click()
    expect(page.get_by_label("任务对话", exact=True)).to_contain_text("first-report-only-body")
    assert downloads == []
    results(page)
    text, raw, _ = both_reports(page, tmp_path, session, first)
    assert "first-report-only-body" in text and "first-report-prompt" in text
    assert "second-report-only-body" not in text and "second-report-prompt" not in raw
    assert second not in text


def test_read_only_reports_remain_available_until_real_grant_revocation(task_page, web_env, tmp_path):
    page, _, _, runtimes = task_page
    session, turn = completed_report(task_page)
    web_env[1].grants = [replace(grant, scopes=grant.scopes - {"agent.sessions:write", "agent.turns:cancel"})
                         if grant.kind == "user" else grant for grant in web_env[1].grants]
    writes = posts(page)
    page.reload()
    expect(page.get_by_label("发送下一条指令")).to_be_disabled()
    results(page)
    both_reports(page, tmp_path, session, turn)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    stale = page.get_by_role("button", name=REPORT, exact=True).element_handle()
    web_env[1].grants = [grant for grant in web_env[1].grants if grant.kind == "gateway"]
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="权限")).to_be_visible()
    expect(page.get_by_role("button", name=REPORT, exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name=RECORD, exact=True)).to_have_count(0)
    stale.evaluate("button => button.click()")
    assert downloads == [] and writes == [] and len(runtimes[0].prompts) == 1


def hold_current_read(page, session):
    """Install before createTasks captures fetch; hold bytes from a real authenticated read."""
    page.add_init_script("""(() => {
      const original = window.fetch.bind(window);
      const gate = window.reportReadGate = {enabled: false, ready: false, invalid: false};
      window.fetch = async (input, init) => {
        const response = await original(input, init), url = new URL(String(input), location.href);
        if (!gate.enabled || init?.signal?.aborted || url.pathname !== `/web/api/sessions/${gate.session}`) return response;
        if (gate.invalid) return new Response('{"data":null}', {status: 200, headers: response.headers});
        if (gate.ready) return response;
        const bytes = await response.arrayBuffer();
        const saved = new Response(bytes, {status: response.status, headers: response.headers});
        if (!gate.enabled || init?.signal?.aborted) return saved;
        gate.ready = true;
        return new Promise(resolve => { gate.release = () => resolve(saved); });
      };
      gate.restore = () => { gate.enabled = false; gate.invalid = false; gate.release?.(); window.fetch = original; };
    })()""")
    page.reload()
    expect(page.get_by_role("region", name="轮次查看模式", exact=True)).to_contain_text("跟随任务现场")
    expect(results(page)).to_contain_text("执行完成")
    for name in [REPORT, RECORD]:
        expect(page.get_by_role("button", name=name, exact=True)).to_be_enabled()
    page.evaluate("session => { window.reportReadGate.session = session }", session)


def test_refreshing_and_paused_reads_refuse_stale_download_buttons(task_page):
    page, _, _, _ = task_page
    session, _ = completed_report(task_page)
    hold_current_read(page, session)
    handles = [page.get_by_role("button", name=name, exact=True).element_handle() for name in [REPORT, RECORD]]
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    try:
        refresh = page.get_by_role("button", name="刷新任务现场", exact=True)
        expect(refresh).to_be_enabled()
        refresh.evaluate("button => { window.reportReadGate.enabled = true; button.click(); }")
        page.wait_for_function("() => window.reportReadGate.ready", timeout=5000)
        for name in [REPORT, RECORD]:
            expect(page.get_by_role("button", name=name, exact=True)).to_be_disabled()
        for handle in handles:
            handle.evaluate("button => { button.disabled = false; button.click(); }")
        assert downloads == []
        page.evaluate("window.reportReadGate.release()")
        expect(page.get_by_role("button", name=REPORT, exact=True)).to_be_enabled()
        page.evaluate("window.reportReadGate.invalid = true")
        page.get_by_role("button", name="刷新任务现场", exact=True).click()
        expect(page.get_by_text("更新已暂停", exact=True)).to_be_visible()
        for handle in handles:
            handle.evaluate("button => button.click()")
        expect(page.get_by_role("button", name=REPORT, exact=True)).to_have_count(0)
        assert downloads == []
    finally:
        page.evaluate("window.reportReadGate.restore()")


def test_unknown_submission_reports_old_round_without_replacing_pending_request(task_page, tmp_path):
    page, context, store, runtimes = task_page
    session, first = completed_report(task_page)
    sent = []

    def lose_first_response(route):
        sent.append((route.request.post_data, route.request.headers.get("idempotency-key")))
        if len(sent) == 1:
            assert route.fetch().status == 202
            route.abort("failed")
        else:
            route.continue_()

    context.route("**/web/api/sessions/" + session + "/turns", lose_first_response)
    original = "PRIVATE-PENDING-REPORT-INSTRUCTION"
    turn = send_prompt(page, store, runtimes, session, original)
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    complete_turn(runtimes[0], "new unknown response executed once")
    until(lambda: store.turn("team", turn)["state"] == "completed")
    page.get_by_role("button", name="刷新任务现场", exact=True).click()
    page.evaluate("hash => { location.hash = hash }", f"tasks/{session}/turns/{first}")
    expect(page.get_by_role("region", name="轮次查看模式", exact=True)).to_contain_text("固定查看此轮")
    results(page)
    text, raw, _ = both_reports(page, tmp_path, session, first)
    for private in [original, sent[0][1], "new unknown response executed once"]:
        assert private not in text and private not in raw
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_be_visible()
    expect(page.get_by_label("发送下一条指令")).to_have_value(original)
    assert len(sent) == 1 and len(runtimes[0].prompts) == 2
    page.get_by_role("button", name="用原目标、原内容和原 key 核对重试", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="提交结果未知")).to_have_count(0)
    assert len(sent) == 2 and sent[0] == sent[1]
    assert runtimes[0].prompts.count(original) == 1


def test_request_receipt_stays_historical_when_the_turn_finishes_or_another_task_opens(task_page):
    page, _, store, runtimes = task_page
    other = create_task(page, store, "other-report-task")
    session, turn = completed_report(task_page, name="receipt-report-task")
    receipt = page.locator(".task-receipt")
    expect(receipt).to_contain_text("最近一次请求回执")
    expect(receipt).to_contain_text("响应当时状态")
    expect(receipt).to_contain_text("排队")
    receipt.locator("summary").click()
    expect(receipt).to_contain_text(session)
    expect(receipt).to_contain_text(turn)
    expect(page.get_by_role("tabpanel", name="结果检查", exact=True)).to_contain_text("执行完成")
    page.evaluate("hash => { location.hash = hash }", f"tasks/{other}")
    expect(page.get_by_role("heading", name="other-report-task", exact=True)).to_be_visible()
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    expect(receipt).to_contain_text(session)
    expect(receipt).to_contain_text(turn)
    expect(receipt).not_to_contain_text(other)
    expect(receipt).to_contain_text("当前执行状态请查看所选任务与轮次")
    assert sum(len(runtime.prompts) for runtime in runtimes) == 1
