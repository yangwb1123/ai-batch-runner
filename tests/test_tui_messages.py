"""Item-aware, bounded assistant message projection contracts."""

import pytest

from pbatch.tui_messages import MessageProjection


def test_distinct_completed_items_are_preserved_in_order():
    messages = MessageProjection(1024)
    messages.consume("message.started", "a")
    messages.consume("message.delta", "a", "draft")
    messages.consume("message.completed", "a", "first")
    messages.consume("message.started", "b")
    view = messages.consume("message.completed", "b", "second")

    assert view.text == "first\n\nsecond"
    assert view.generation == 2


def test_late_completion_replaces_an_earlier_item_not_the_turn():
    messages = MessageProjection(1024)
    messages.consume("message.delta", "a", "draft")
    before = messages.consume("message.delta", "b", "second")

    view = messages.consume("message.completed", "a", "first")

    assert before.text == "draft\n\nsecond"
    assert view.text == "first\n\nsecond"
    assert view.generation > before.generation


def test_empty_item_started_events_create_distinct_implicit_items():
    messages = MessageProjection(1024)
    messages.consume("message.started")
    messages.consume("message.completed", text="first")
    messages.consume("message.started")
    view = messages.consume("message.completed", text="second")

    assert view.text == "first\n\nsecond"


def test_items_share_one_utf8_budget_and_keep_the_final_tail():
    messages = MessageProjection(64)
    messages.consume("message.completed", "a", "界" * 30)
    view = messages.consume("message.completed", "b", "FINAL_TAIL")

    assert len(view.text.encode("utf-8")) <= 64
    assert view.text.endswith("FINAL_TAIL")
    assert view.start > 0


def test_item_metadata_limit_fails_closed():
    messages = MessageProjection(128, max_items=1)
    first = messages.consume("message.completed", "a", "first")

    with pytest.raises(OverflowError, match="projection is full"):
        messages.consume("message.completed", "b", "second")

    assert messages.snapshot() == first


def test_failed_implicit_item_reservation_does_not_change_active_item():
    messages = MessageProjection(128, max_items=1)
    messages.consume("message.started")
    first = messages.consume("message.delta", text="first")

    with pytest.raises(OverflowError, match="projection is full"):
        messages.consume("message.started")
    view = messages.consume("message.completed", text="finished")

    assert first.text == "first"
    assert view.text == "finished"
