"""Real Chrome/TLS run_forever delivery; synthetic issuer/provider, no timing claims."""

import pytest
from playwright.sync_api import expect

from browser_hub_web_control import confirm_operation, conversation
from browser_hub_web_support import controlled_hub, task_page, create_task, send_prompt, complete_turn, until
from test_hub_auth import signed
from test_hub_web import web_env

pytestmark = pytest.mark.parametrize("controlled_hub", [{"gateway_run_forever": True}], indirect=True)


def burst(runtime):
    parts = [f"drain-part-{index:03d} " for index in range(72)]
    for part in parts:
        runtime.emit("message.delta", {"text": part})
    return "".join(parts)


def assert_delivery(store, session, turn, terminal):
    events = store.events("team", session, limit=100, turn_id=turn)
    assert len(events) > 64
    assert sum(row["kind"] == "message.delta" for row in events) == 72
    assert sum(row["kind"] == terminal for row in events) == 1
    assert events[-1]["kind"] == terminal
    assert all(row["turn_id"] == turn and row["session_id"] == session for row in events)
    assert len({row["event_id"] for row in events}) == len(events)


def test_run_forever_delivers_multi_batch_reply_and_terminal_to_selected_turn(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "production-loop-reply")
    prompt = "produce a complete multi-batch fixture reply"
    turn = send_prompt(page, store, runtimes, session, prompt)
    text = burst(runtimes[0]) + "界" * 6000 + "drain final marker"
    complete_turn(runtimes[0], text)
    until(lambda: store.turn("team", turn)["state"] == "completed")
    assert_delivery(store, session, turn, "turn.completed")
    expect(conversation(page)).to_contain_text("drain final marker")
    expect(conversation(page).locator(".turn-context")).to_contain_text(turn)
    expect(conversation(page).locator(".session-status")).to_have_attribute("data-state", "completed")
    expect(conversation(page).get_by_label("回复格式预览", exact=True)).to_have_text(text)
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    assert runtimes[0].prompts == [prompt]
    page.get_by_role("tab", name="执行事件", exact=True).click()
    expect(page.get_by_label("执行事件窗口", exact=True)).to_contain_text("turn.completed")


@pytest.mark.parametrize("task_page", [(390, 844)], indirect=True)
def test_run_forever_cancels_streaming_turn_and_allows_explicit_next_instruction(task_page):
    page, _, store, runtimes = task_page
    session = create_task(page, store, "production-loop-cancel")
    prompt = "stream until an explicit cancellation"
    turn = send_prompt(page, store, runtimes, session, prompt)
    text = burst(runtimes[0])
    confirm_operation(page, "请求取消活动轮次")
    until(lambda: store.turn("team", turn)["state"] == "interrupted")
    assert_delivery(store, session, turn, "turn.interrupted")
    expect(conversation(page).locator(".turn-context")).to_contain_text(turn)
    expect(conversation(page).locator(".session-status")).to_have_attribute("data-state", "interrupted")
    expect(conversation(page).get_by_label("回复格式预览", exact=True)).to_have_text(text)
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    following = send_prompt(page, store, runtimes, session, "continue explicitly after cancellation")
    assert following != turn
    complete_turn(runtimes[0], "next-turn-only fixture reply")
    until(lambda: store.turn("team", following)["state"] == "completed")
    expect(conversation(page)).to_contain_text("next-turn-only fixture reply")
    expect(conversation(page).locator(".turn-context")).to_contain_text(following)
    expect(conversation(page).locator(".session-status")).to_have_attribute("data-state", "completed")
    expect(conversation(page)).not_to_contain_text("drain-part-000")
    assert runtimes[0].prompts == [prompt, "continue explicitly after cancellation"]
