"""Changes Inspector live-sampling contracts."""

from __future__ import annotations

import asyncio
import threading

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_changes import (
    ChangesRequest,
    ChangesSnapshot,
    ChangeFile,
    TUIChangesScreen,
    build_changes_snapshot,
    hunk_index_for_line,
    render_changes_detail,
)


class _Backend:
    def __init__(self, emit, **options):
        self.emit = emit
        self.cwd = options.get("cwd", ".")
        self.agent = "fake-agent"
        self.provider = ""
        self.model = ""
        self.calls = 0
        self.call_threads = []

    def set_emit(self, emit):
        self.emit = emit

    def snapshot(self):
        return {"session_id": "session", "workspace": {}}

    def workspace_status(self):
        self.calls += 1
        self.call_threads.append(threading.get_ident())
        return {"branch": "main", "changed": 1, "staged": 0,
                "unstaged": 1, "untracked": 0,
                "files": [{"path": "src/app.py", "status": " M"}]}

    def workspace_diff_scope(self, scope="all", path="", target=""):
        return {"scope": scope, "path": path, "target": target,
                "files": [{"path": "src/app.py", "status": " M"}],
                "patch": "@@ -1 +1 @@\n-old\n+new\n"}


def test_snapshot_keeps_selected_file_and_bounds_rows():
    request = ChangesRequest(scope="all", path="src/app.py")
    snapshot = build_changes_snapshot(
        request,
        {"files": [{"path": "src/app.py", "status": " M"}]},
        {"files": [{"path": "src/app.py", "status": " M"}], "patch": ""},
        None,
    )
    assert snapshot.files == (ChangeFile("src/app.py", "M"),)
    assert snapshot.request.path == "src/app.py"


def test_review_line_selects_nearest_unified_diff_hunk():
    patch = """diff --git a/src/app.py b/src/app.py
@@ -1,2 +1,3 @@
 old
+new
@@ -20,1 +21,2 @@
 old
+later
"""
    assert hunk_index_for_line(patch, 2) == 0
    assert hunk_index_for_line(patch, 21) == 1
    assert hunk_index_for_line(patch, 999) == 1
    assert hunk_index_for_line(patch, 0) == 0


def test_detail_shows_statistics_and_binary_boundary():
    snapshot = build_changes_snapshot(
        ChangesRequest(), {"files": []},
        {"files": [], "bytes": 41,
         "patch": "Binary files a.bin and b.bin differ"}, None)
    rendered = render_changes_detail(snapshot)
    assert "patch 41 bytes" in rendered
    assert "Binary content is present" in rendered


def test_live_changes_refresh_runs_off_ui_thread():
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(), backend_factory=_Backend,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            # Simulate a turn that is already running; opening a live changes
            # view must remain available while Git sampling is asynchronous.
            app._busy = True
            app._slash_changes(())
            screen = app.screen
            assert isinstance(screen, TUIChangesScreen)
            assert "LIVE" in screen._heading()
            screen.action_scope_last_turn()
            assert screen.snapshot.request.scope == "all"
            backend = app._backend
            initial = backend.calls
            await pilot.pause(1.0)
            assert backend.calls > initial
            assert any(thread_id != threading.get_ident()
                       for thread_id in backend.call_threads[1:])
            app._busy = False
            await pilot.press("escape")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
