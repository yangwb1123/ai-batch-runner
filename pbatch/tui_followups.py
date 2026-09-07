"""Bounded follow-up queue and capability-driven active-turn steering."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .tui_command_specs import (
    COMPACTION_BUSY,
    REVIEW_BUSY,
    SETTING_BUSY,
    TURN_BUSY,
    command_allowed_while,
    command_spec,
)
from .tui_protocol import TurnState
from .tui_session_controls import session_compaction_active
from .tui_model_controls import runtime_setting_change_active
from .tui_view_support import clean_text, one_line, result_fields


FOLLOW_UP_MAX_ITEMS = 16
FOLLOW_UP_MAX_BYTES = 64 * 1024
FOLLOW_UP_ITEM_MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class FollowUpItem:
    """One immutable process-local prompt waiting for a later turn."""

    item_id: int
    text: str
    session_id: str
    queued_ns: int

    @property
    def size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


class FollowUpQueue:
    """A small FIFO with explicit count, per-item and aggregate limits."""

    def __init__(self, *, max_items: int = FOLLOW_UP_MAX_ITEMS,
                 max_bytes: int = FOLLOW_UP_MAX_BYTES,
                 item_max_bytes: int = FOLLOW_UP_ITEM_MAX_BYTES) -> None:
        _positive(max_items, "max_items")
        _positive(max_bytes, "max_bytes")
        _positive(item_max_bytes, "item_max_bytes")
        if item_max_bytes > max_bytes:
            raise ValueError("item_max_bytes must not exceed max_bytes")
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.item_max_bytes = item_max_bytes
        self._items: list[FollowUpItem] = []
        self._next_id = 1
        self._bytes = 0

    @property
    def items(self) -> tuple[FollowUpItem, ...]:
        return tuple(self._items)

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def enqueue(self, text: str, session_id: str = "") -> FollowUpItem:
        value = _prompt(text, self.item_max_bytes)
        size = len(value.encode("utf-8"))
        if len(self._items) >= self.max_items:
            raise OverflowError("follow-up queue item limit reached")
        if self._bytes + size > self.max_bytes:
            raise OverflowError("follow-up queue byte limit reached")
        item = FollowUpItem(
            self._next_id, value, str(session_id or ""), time.time_ns())
        self._next_id += 1
        self._items.append(item)
        self._bytes += size
        return item

    def edit(self, position: int, text: str) -> FollowUpItem:
        index = self._index(position)
        value = _prompt(text, self.item_max_bytes)
        item = self._items[index]
        size = self._bytes - item.size_bytes + len(value.encode("utf-8"))
        if size > self.max_bytes:
            raise OverflowError("follow-up queue byte limit reached")
        updated = replace(item, text=value)
        self._items[index] = updated
        self._bytes = size
        return updated

    def remove(self, position: int) -> FollowUpItem:
        item = self._items.pop(self._index(position))
        self._bytes -= item.size_bytes
        return item

    def discard(self, item_id: int) -> bool:
        for index, item in enumerate(self._items):
            if item.item_id == item_id:
                self.remove(index + 1)
                return True
        return False

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        self._bytes = 0
        return count

    def peek(self) -> Optional[FollowUpItem]:
        return self._items[0] if self._items else None

    def _index(self, position: int) -> int:
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError("queue position must be an integer")
        index = position - 1
        if index < 0 or index >= len(self._items):
            raise IndexError("follow-up queue position is out of range")
        return index


@dataclass(frozen=True)
class FollowUpScreenAction:
    operation: str
    position: int = 0


class TUIFollowUpScreen(ModalScreen[Optional[FollowUpScreenAction]]):
    """Read and prune queued prompts without adding transcript content."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("d", "drop", "Drop"),
        Binding("c", "clear", "Clear"),
        Binding("r", "run", "Run next"),
    ]
    CSS = """
    TUIFollowUpScreen { align: center middle; background: $background; }
    #follow-up-dialog {
        width: 84%; height: 78%; min-width: 38;
        background: $surface; border: round $accent;
    }
    #follow-up-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #follow-up-options { height: 1fr; padding: 0 1; }
    #follow-up-detail {
        height: 6; padding: 0 2; color: $text-muted;
        border-top: solid $panel-lighten-1;
    }
    #follow-up-hint { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, items: tuple[FollowUpItem, ...], *, busy: bool) -> None:
        super().__init__(id="follow-up-queue")
        self.items = items
        self.busy = busy

    def compose(self) -> ComposeResult:
        options = [Option(_item_label(index, item), id=f"follow-up-{item.item_id}")
                   for index, item in enumerate(self.items, 1)]
        with Vertical(id="follow-up-dialog"):
            yield Static(
                f"Follow-up queue ({len(self.items)})",
                id="follow-up-heading", markup=False)
            yield OptionList(*options, id="follow-up-options", markup=False)
            yield Static(self._detail(0), id="follow-up-detail", markup=False)
            yield Static(
                "d drop selected · c clear · r run next when idle · "
                "/queue edit N TEXT · Esc close",
                id="follow-up-hint", markup=False)

    def on_mount(self) -> None:
        options = self.query_one("#follow-up-options", OptionList)
        options.focus()
        self._update_detail(options.highlighted)

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        self._update_detail(event.option_index)

    def action_drop(self) -> None:
        position = self._selected_position()
        if position:
            self.dismiss(FollowUpScreenAction("drop", position))

    def action_clear(self) -> None:
        if not self.items:
            self.notify("The follow-up queue is already empty.")
            return
        self.dismiss(FollowUpScreenAction("clear"))

    def action_run(self) -> None:
        if self.busy:
            self.notify("The current turn must finish before running a follow-up.",
                        severity="warning")
            return
        if not self.items:
            self.notify("The follow-up queue is empty.")
            return
        self.dismiss(FollowUpScreenAction("run"))

    def action_close(self) -> None:
        self.dismiss(None)

    def _selected_position(self) -> int:
        index = self.query_one("#follow-up-options", OptionList).highlighted
        if index is None or index < 0 or index >= len(self.items):
            self.notify("No follow-up is selected.", severity="warning")
            return 0
        return index + 1

    def _update_detail(self, index: Optional[int]) -> None:
        self.query_one("#follow-up-detail", Static).update(self._detail(index))

    def _detail(self, index: Optional[int]) -> str:
        if index is None or index < 0 or index >= len(self.items):
            return "No follow-up prompts are queued."
        item = self.items[index]
        text = clean_text(item.text)
        encoded = text.encode("utf-8")
        if len(encoded) > 4_096:
            text = encoded[:4_093].decode("utf-8", "ignore") + "…"
        return (f"#{index + 1} · {item.size_bytes} bytes · process-local queue\n\n"
                f"{text}")


class TUIFollowUpMixin:
    """Add explicit queue/steer semantics to an active TUI turn."""

    def _init_follow_up_controls(self) -> None:
        self._follow_up_queue = FollowUpQueue()
        self._steer_pending = False

    def _handle_busy_submission(self, value: str, invocation: Any) -> bool:
        if runtime_setting_change_active(self):
            if (invocation is not None and command_allowed_while(
                    invocation.name, SETTING_BUSY)):
                self._dispatch_slash(invocation)
            else:
                self.notify("Wait for the runtime setting operation.",
                            severity="warning")
            return True
        if session_compaction_active(self):
            if (invocation is not None and command_allowed_while(
                    invocation.name, COMPACTION_BUSY)):
                self._dispatch_slash(invocation)
            else:
                self.notify(
                    "Wait for session compaction; `/session` remains available.",
                    severity="warning")
            return True
        if not self._busy:
            return False
        if getattr(self, "_pipeline_busy", False):
            return self._handle_pipeline_busy_submission(invocation)
        if self._review_busy:
            if (invocation is not None and command_allowed_while(
                    invocation.name, REVIEW_BUSY)):
                self._dispatch_slash(invocation)
            else:
                self.notify("Wait for the active review before submitting a prompt.",
                            severity="warning")
            return True
        if invocation is None:
            self._enqueue_follow_up(value)
            return True
        if command_allowed_while(invocation.name, TURN_BUSY):
            self._dispatch_slash(invocation)
        else:
            self.notify(
                "During a turn, queue text or use `/steer`, `/agents`, "
                "`/completion`, `/artifacts`, `/approvals`, or `/session`.",
                severity="warning")
        return True

    def _handle_pipeline_busy_submission(self, invocation: Any) -> bool:
        spec = (command_spec(invocation.name)
                if invocation is not None else None)
        if (spec is not None and spec.risk == "read"
                and command_allowed_while(invocation.name, TURN_BUSY)):
            self._dispatch_slash(invocation)
        else:
            self.notify(
                "Native Pipeline is running; only read-only inspectors are available.",
                severity="warning")
        return True

    def _slash_queue(self, args: tuple[str, ...]) -> None:
        if not args or args == ("list",):
            self._open_follow_up_queue()
            return
        command = args[0].lower()
        if command == "clear" and len(args) == 1:
            self._clear_follow_ups()
        elif command == "drop" and len(args) == 2:
            self._drop_follow_up(args[1])
        elif command == "edit" and len(args) >= 3:
            self._edit_follow_up(args[1], " ".join(args[2:]))
        elif command == "run" and len(args) == 1:
            self._run_next_follow_up()
        elif command == "add" and len(args) >= 2:
            self._enqueue_follow_up(" ".join(args[1:]))
        elif command in {"clear", "drop", "edit", "run", "add", "list"}:
            self._queue_usage()
        else:
            self._enqueue_follow_up(" ".join(args))

    def _slash_steer(self, args: tuple[str, ...]) -> None:
        if not args:
            self.notify("Usage: /steer <text>", severity="warning")
            return
        if not self._busy or self._review_busy:
            self._append_entry("error", "Steer requires an active agent turn.")
            return
        adapter = getattr(self, "_adapter", None)
        handle = getattr(self, "_turn_handle", None)
        if adapter is None or handle is None or getattr(handle, "done", False):
            self.notify("The active runtime has no steerable turn handle.",
                        severity="warning")
            return
        try:
            supported = self._active_runtime_supports("turn_steer")
        except Exception as exc:
            self.notify(f"Steer capability check failed: {exc}", severity="error")
            return
        if not supported:
            self.notify("The active runtime does not support turn steering.",
                        severity="warning")
            return
        if self._steer_pending:
            self.notify("A steer request is already pending.", severity="warning")
            return
        self._start_steer(adapter, handle, " ".join(args))

    def _active_runtime_supports(self, name: str) -> bool:
        adapter = getattr(self, "_adapter", None)
        if adapter is None:
            return False
        runtime_info = getattr(adapter, "runtime_info", None)
        info = runtime_info() if callable(runtime_info) else getattr(
            adapter, "_info", None)
        capabilities = getattr(info, "capabilities", None)
        supports = getattr(capabilities, "supports", None)
        return bool(supports(name)) if callable(supports) else False

    def _start_steer(self, adapter: Any, handle: Any, text: str) -> None:
        self._steer_pending = True
        self._last_status = "Sending steer request"
        self._refresh_status()
        self.run_worker(
            lambda: self._steer_worker(adapter, handle, text),
            name="turn-steer", group="turn-control", exit_on_error=False,
            exclusive=True, thread=True)

    def _steer_worker(self, adapter: Any, handle: Any, text: str) -> None:
        error: Optional[Exception] = None
        try:
            adapter.steer_turn({"turn_id": handle.turn_id, "text": text})
        except Exception as exc:
            error = exc
        try:
            self.call_from_thread(self._finish_steer, error)
        except RuntimeError:
            pass

    def _finish_steer(self, error: Optional[Exception]) -> None:
        self._steer_pending = False
        if error is None:
            self._last_status = "Steer accepted"
            self.notify("Steer request accepted by the active runtime.")
        else:
            self._last_status = "Steer failed"
            self.notify(f"Steer failed: {error}", severity="error")
        self._refresh_status()

    def _enqueue_follow_up(self, text: str) -> bool:
        try:
            item = self._follow_up_queue.enqueue(
                text, self._current_session_id())
        except (OverflowError, TypeError, ValueError) as exc:
            self.notify(f"Unable to queue follow-up: {exc}", severity="error")
            return False
        self._last_status = f"Follow-up queued (#{len(self._follow_up_queue.items)})"
        self.notify(f"Queued follow-up #{item.item_id} for the next turn.")
        self._refresh_follow_up_ui()
        if not self._busy:
            self.call_after_refresh(self._run_next_follow_up)
        return True

    def _drop_follow_up(self, value: str) -> bool:
        try:
            item = self._follow_up_queue.remove(_position(value))
        except (IndexError, ValueError) as exc:
            self.notify(f"Unable to drop follow-up: {exc}", severity="error")
            return False
        self.notify(f"Dropped queued follow-up #{item.item_id}.")
        self._refresh_follow_up_ui()
        return True

    def _edit_follow_up(self, value: str, text: str) -> bool:
        try:
            item = self._follow_up_queue.edit(_position(value), text)
        except (IndexError, OverflowError, ValueError) as exc:
            self.notify(f"Unable to edit follow-up: {exc}", severity="error")
            return False
        self.notify(f"Updated queued follow-up #{item.item_id}.")
        self._refresh_follow_up_ui()
        return True

    def _clear_follow_ups(self) -> int:
        count = self._follow_up_queue.clear()
        self.notify(f"Cleared {count} queued follow-up(s).")
        self._refresh_follow_up_ui()
        return count

    def _open_follow_up_queue(self) -> None:
        screen = TUIFollowUpScreen(
            self._follow_up_queue.items, busy=self._busy)
        self.push_screen(screen, self._handle_follow_up_screen_action)

    def _handle_follow_up_screen_action(self, action: Any) -> None:
        if action is None:
            return
        operation = getattr(action, "operation", "")
        if operation == "drop":
            self._drop_follow_up(str(getattr(action, "position", 0)))
            self.call_later(self._open_follow_up_queue)
        elif operation == "clear":
            self._clear_follow_ups()
            self.call_later(self._open_follow_up_queue)
        elif operation == "run":
            self._run_next_follow_up()

    def _schedule_follow_up_after_turn(
            self, result: Any, error: Optional[Exception]) -> None:
        if not self._follow_up_queue.items:
            return
        success = error is None and result_fields(result)[0]
        if success and getattr(self, "_projection_driven", False):
            view = getattr(self, "_runtime_view", None)
            success = getattr(view, "turn_state", "") == TurnState.SUCCEEDED
        if success:
            self.call_after_refresh(self._run_next_follow_up)
            return
        self._last_status = (
            f"Follow-up queue paused ({len(self._follow_up_queue.items)})")
        self.notify("Queued follow-ups were preserved after the failed turn.",
                    severity="warning")
        self._refresh_follow_up_ui()

    def _run_next_follow_up(self) -> bool:
        if self._busy or self._review_busy:
            self.notify("Wait for the active operation before running the queue.",
                        severity="warning")
            return False
        item = self._follow_up_queue.peek()
        if item is None:
            self.notify("The follow-up queue is empty.")
            return False
        current = self._current_session_id()
        if item.session_id and current and item.session_id != current:
            self.notify("Queued follow-up belongs to a different session.",
                        severity="error")
            return False
        if not self._start_turn(item.text):
            self.notify("Follow-up was preserved because the turn did not start.",
                        severity="error")
            return False
        self._follow_up_queue.discard(item.item_id)
        self._last_status = "Queued follow-up started"
        self._refresh_follow_up_ui()
        return True

    def _refresh_follow_up_ui(self) -> None:
        if not self.is_mounted:
            return
        self._refresh_status()
        composer = self.query_one("#composer")
        self._configure_follow_up_composer(composer, self._busy)

    def _configure_follow_up_composer(self, composer: Any, busy: bool) -> None:
        if busy and self._review_busy:
            composer.disabled = True
            composer.placeholder = "Review is running…"
            return
        composer.disabled = False
        if busy:
            count = len(self._follow_up_queue.items)
            composer.placeholder = (
                f"Queue follow-up ({count})… use /steer TEXT when supported")
        else:
            composer.placeholder = "Ask pi-batch…"

    def _follow_up_status_lines(self) -> list[str]:
        items = self._follow_up_queue.items
        if not items:
            return []
        preview = one_line(clean_text(items[0].text))[:20]
        return [f"queue     {len(items)} / {self._follow_up_queue.bytes_used} B",
                f"next      {preview}"]

    def _reset_follow_ups_for_session_switch(self) -> None:
        count = self._follow_up_queue.clear()
        if count:
            self.notify(
                f"Discarded {count} queued follow-up(s) from the previous session.",
                severity="warning")

    def _queue_usage(self) -> None:
        self.notify(
            "Usage: /queue [TEXT|add TEXT|list|run|clear|drop N|edit N TEXT]",
            severity="warning")

    def _palette_queue(self) -> None:
        self._open_follow_up_queue()


def _prompt(value: str, maximum: int) -> str:
    text = clean_text(value).strip()
    if not text or "\0" in text:
        raise ValueError("follow-up prompt must not be empty or contain NUL")
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"follow-up prompt exceeds {maximum} bytes")
    return text


def _position(value: str) -> int:
    try:
        position = int(str(value), 10)
    except (TypeError, ValueError) as exc:
        raise ValueError("queue position must be a positive integer") from exc
    if position <= 0:
        raise ValueError("queue position must be a positive integer")
    return position


def _item_label(position: int, item: FollowUpItem) -> str:
    preview = one_line(clean_text(item.text))
    if len(preview) > 96:
        preview = preview[:95] + "…"
    return f"{position}. {preview}  ·  {item.size_bytes} B"


def _positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
