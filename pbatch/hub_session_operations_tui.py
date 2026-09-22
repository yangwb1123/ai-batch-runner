"""Read-only, single-page request history with explicit guarded navigation."""

import asyncio

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from .hub_auth_feedback import error_status
from .hub_session_operations_client import history_error, page, refreshed_record, visible as _visible


def _key(row):
    return row["operation"] + ":" + row["request_id"]


class SessionOperations(ModalScreen):
    DEFAULT_CSS = """
    SessionOperations { align: center middle; }
    #operations-dialog { width: 100; max-width: 95%; height: 90%; padding: 1 2;
                         border: round $accent; background: $surface; }
    #operations-title { height: auto; }
    #operations-detail { height: 1fr; overflow-y: auto; }
    #operations-status { height: auto; max-height: 4; }
    .operations-actions { height: 3; }
    .operations-actions Button { width: 1fr; min-width: 10; }
    """

    def __init__(self, host, instance_id):
        super().__init__()
        self.host, self.client, self.instance_id = host, host.client, instance_id
        self.rows = {}
        self.next_cursor = ""
        self.busy = False
        self.closed = False
        self.auth_context = None
        self.selected = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="operations-dialog"):
            yield Static("Session requests · " + _visible(self.instance_id), id="operations-title",
                         markup=False)
            with Horizontal(classes="operations-actions"):
                yield Button("Latest", id="operations-latest")
                yield Button("Older", id="operations-older")
                yield Button("Refresh request", id="operations-refresh")
            yield Select([], prompt="Accepted request", id="operations-select")
            yield Static("Loading accepted requests…", id="operations-detail", markup=False)
            yield Static("", id="operations-status", markup=False)
            with Horizontal(classes="operations-actions"):
                yield Button("Open session", id="operations-open", variant="primary")
                yield Button("Done", id="operations-done")

    def on_mount(self):
        self.run_worker(self.load_page())

    def on_unmount(self):
        self.closed = True

    def key_escape(self):
        self.closed = True
        self.dismiss()

    def _live(self):
        return self.is_mounted and not self.closed and self.host._selected_instance == self.instance_id

    async def _auth(self):
        read = getattr(self.client, "_auth_context", lambda: None)
        return await asyncio.to_thread(read)

    def _status(self, text):
        if self.is_mounted and not self.closed:
            self.query_one("#operations-status", Static).update(text[:512])

    def _controls(self):
        if not self.is_mounted or self.closed:
            return
        row = self.rows.get(self.selected, {})
        disabled = self.busy or not self._live()
        for name, condition in (("latest", False), ("older", not self.next_cursor),
                                ("refresh", not row), ("open", not row.get("session_id"))):
            self.query_one("#operations-" + name, Button).disabled = disabled or condition
        self.query_one("#operations-select", Select).disabled = disabled

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        action = event.button.id
        if action == "operations-done":
            self.key_escape()
        elif action == "operations-latest":
            self.run_worker(self.load_page())
        elif action == "operations-older" and self.next_cursor:
            self.run_worker(self.load_page(self.next_cursor))
        elif action == "operations-refresh":
            self.run_worker(self.refresh_request())
        elif action == "operations-open":
            self.run_worker(self.open_session())

    def on_select_changed(self, event: Select.Changed):
        if event.select.id != "operations-select":
            return
        event.stop()
        self.selected = event.value if isinstance(event.value, str) else ""
        self._detail()
        self._controls()

    def _detail(self):
        row = self.rows.get(self.selected)
        if row is None:
            text = "No accepted session requests on this page." if not self.rows else "Select a request."
        else:
            text = "\n".join(f"{key}: {_visible(row[key])}" for key in (
                "operation", "status", "name", "request_id", "session_id", "error_code",
                "created_at", "updated_at"))
        self.query_one("#operations-detail", Static).update(text)

    def _replace(self, result):
        self.rows = {_key(row): row for row in result["data"]}
        self.next_cursor = result["next_cursor"]
        self.selected = next(iter(self.rows), "")
        self._options()
        self._detail()

    def _options(self):
        selector = self.query_one("#operations-select", Select)
        selector.set_options([(_visible(f"{row['operation']} · {row['status']} · "
                                        f"{row['name'] or row['session_id']} · {row['request_id']}"), key)
                              for key, row in self.rows.items()])
        if self.selected:
            selector.value = self.selected

    def _clear_auth(self):
        self.auth_context = None
        if self.is_mounted and not self.closed:
            self._replace({"data": [], "next_cursor": ""})

    async def _same_context(self, context):
        if not self._live():
            return False
        try:
            current = await self._auth()
        except Exception:
            self._clear_auth()
            raise
        if context != current:
            self._clear_auth()
            self._status("Sign-in changed. Use Latest to reload requests for the current account.")
            return False
        return self._live()

    async def load_page(self, before=""):
        if self.busy or not self._live():
            return
        self.busy = True
        self._controls()
        self._status("Loading accepted requests…")
        try:
            context = await self._auth()
            if self.auth_context is not None and self.auth_context != context:
                self._clear_auth()
                if before:
                    self._status("Sign-in changed. Use Latest to reload requests for the current account.")
                    return
            result = await asyncio.to_thread(self.client.list_session_operations, self.instance_id,
                                             before=before, limit=20)
            result = page(result, instance_id=self.instance_id, before=before, limit=20)
            if await self._same_context(context):
                self.auth_context = context
                self._replace(result)
                self._status("Select a request to refresh it or explicitly open its session.")
        except Exception as exc:
            if self._live():
                if getattr(exc, "status", 0) in {401, 403}:
                    self._clear_auth()
                self._status(history_error(exc))
        finally:
            self.busy = False
            self._controls()

    async def refresh_request(self):
        prior = self.rows.get(self.selected)
        if self.busy or not self._live() or not prior:
            return
        self.busy = True
        self._controls()
        try:
            if not await self._same_context(self.auth_context):
                return
            read = self.client.session_request if prior["operation"] == "create" else self.client.session_close_request
            detail = await asyncio.to_thread(read, prior["request_id"])
            current = refreshed_record(prior, detail)
            if await self._same_context(self.auth_context) and self.selected == _key(prior):
                self.rows[self.selected] = current
                self._options()
                self._detail()
                self._status("Request status refreshed. Opening its session remains an explicit action.")
        except Exception as exc:
            if self._live():
                if getattr(exc, "status", 0) in {401, 403}:
                    self._clear_auth()
                self._status(error_status("Request details unavailable; refresh to retry", exc))
        finally:
            self.busy = False
            self._controls()

    async def open_session(self):
        prior = self.rows.get(self.selected)
        if self.busy or not self._live() or not prior or not prior["session_id"]:
            return
        selection = self.host._selection_generation
        self.busy = True
        self._controls()
        try:
            if not await self._same_context(self.auth_context):
                return
            session = await asyncio.to_thread(self.client.get_session, prior["session_id"])
            if (not isinstance(session, dict) or session.get("session_id") != prior["session_id"]
                    or session.get("instance_id") != self.instance_id):
                raise ValueError("session response identity changed")
            if (await self._same_context(self.auth_context) and self.selected == _key(prior)
                    and selection == self.host._selection_generation):
                self.closed = True
                self.dismiss()
                await _open(self.host, session)
        except Exception as exc:
            if self._live():
                if getattr(exc, "status", 0) in {401, 403}:
                    self._clear_auth()
                self._status(error_status("Session unavailable; try Open session again", exc))
        finally:
            self.busy = False
            self._controls()


async def _open(host, session):
    from .hub_tui import _session_option, _ALL_INSTANCES
    session_id = session["session_id"]
    host._sessions[session_id] = session
    selector = host.query_one("#session", Select)
    selector.set_options([_session_option(row, host._selected_instance == _ALL_INSTANCES)
                          for key, row in host._sessions.items()])
    host._selection_generation += 1
    host._selected_session = session_id
    selector.value = session_id
    host._update_controls()
    await host._refresh_events()
