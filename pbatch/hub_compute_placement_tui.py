"""Read-only placement page, fenced to its original task and credential context."""

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .hub_compute_placement_client import describe, failure, placement, visible


class ComputePlacement(ModalScreen):
    DEFAULT_CSS = """
    ComputePlacement { align: center middle; }
    #placement-dialog { width: 100; max-width: 95%; height: 90%; padding: 1 2;
                        border: round $accent; background: $surface; }
    #placement-title { height: auto; }
    #placement-detail { height: 1fr; overflow-y: auto; }
    #placement-status { height: auto; max-height: 4; }
    #placement-actions { height: 3; }
    #placement-actions Button { width: 1fr; min-width: 10; }
    """

    def __init__(self, host):
        super().__init__()
        self.host, self.client = host, host.client
        self.task_id, self.session_id = host._selected_task, host._selected_session
        self.selection = host._placement_selection_seq
        self.result = None
        self.next_cursor = ""
        self.auth_context = None
        self.busy = False
        self.closed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="placement-dialog"):
            yield Static("Placement diagnostics · " + visible(self.task_id),
                         id="placement-title", markup=False)
            yield Static("Loading placement diagnostics…", id="placement-detail", markup=False)
            yield Static("", id="placement-status", markup=False)
            with Horizontal(id="placement-actions"):
                yield Button("Refresh", id="placement-refresh")
                yield Button("Next page", id="placement-next")
                yield Button("Done", id="placement-done")

    def on_mount(self):
        self.run_worker(self.load_page())

    def on_unmount(self):
        self.closed = True

    def key_escape(self):
        self.closed = True
        self.dismiss()

    def _live(self):
        return (self.is_mounted and not self.closed
                and self.host._selected_task == self.task_id
                and self.host._selected_session == self.session_id
                and self.host._placement_selection_seq == self.selection)

    def _status(self, message):
        if self.is_mounted and not self.closed:
            self.query_one("#placement-status", Static).update(message[:512])

    def _controls(self):
        if self.is_mounted and not self.closed:
            disabled = self.busy or not self._live()
            self.query_one("#placement-refresh", Button).disabled = disabled
            self.query_one("#placement-next", Button).disabled = disabled or not self.next_cursor

    def _clear(self, message):
        self.result, self.next_cursor, self.auth_context = None, "", None
        if self.is_mounted and not self.closed:
            self.query_one("#placement-detail", Static).update("")
            self._status(message)

    def _selected(self):
        if self._live():
            return True
        self._clear("Task selection changed. Close this view and reopen diagnostics for the selected task.")
        return False

    def invalidate_selection(self):
        self._selected()
        self._controls()

    async def _auth(self):
        return await asyncio.to_thread(self.client.client._auth_context)

    async def _same_auth(self, context):
        if not self._selected():
            return False
        current = await self._auth()
        if current != context:
            self._clear("Sign-in changed. Refresh to load diagnostics for the current account.")
            return False
        return self._selected()

    async def _error(self, exc, context):
        if not self._selected():
            return
        try:
            if context is not None and not await self._same_auth(context):
                return
        except Exception:
            self._clear("Sign-in unavailable. Sign in, then refresh diagnostics.")
            return
        if context is None or getattr(exc, "status", 0) in {401, 403}:
            self._clear(failure(exc))
        else:
            self._status(failure(exc))

    async def load_page(self, after=""):
        if self.busy or not self._selected():
            return
        context = None
        self.busy = True
        self._controls()
        self._status("Loading placement diagnostics…")
        try:
            context = await self._auth()
            if self.auth_context is not None and context != self.auth_context:
                self._clear("Sign-in changed. Refresh to load diagnostics for the current account.")
                if after:
                    return
            result = await asyncio.to_thread(self.client.placement, self.task_id, after=after, limit=20)
            result = placement(result, task_id=self.task_id, after=after, limit=20)
            if await self._same_auth(context):
                self.result, self.next_cursor, self.auth_context = result, result["next_cursor"], context
                self.query_one("#placement-detail", Static).update(describe(result))
                self._status("One page loaded. Refresh starts again; Next page reads only the following page.")
        except Exception as exc:
            await self._error(exc, context)
        finally:
            self.busy = False
            self._controls()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "placement-done":
            self.key_escape()
        elif event.button.id == "placement-refresh":
            self.run_worker(self.load_page())
        elif event.button.id == "placement-next" and self.next_cursor:
            self.run_worker(self.load_page(self.next_cursor))
