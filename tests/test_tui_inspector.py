"""Headless contracts for the independent Plan and Changes inspectors."""

from __future__ import annotations

import asyncio
import json

from textual.widgets import Input, Markdown, OptionList, Static

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_changes import (
    TUIChangesScreen,
    diff_hunks,
    parse_changes_args,
)
from pbatch.tui_inspector import (
    TUIInspectorScreen,
    render_changes_inspector,
    render_plan_inspector,
)
from pbatch.tui_shell import SLASH_HELP
from pbatch.tui_timeline import TimelineLog


class InspectorBackend:
    def __init__(self, emit, **_options):
        self.emit = emit
        self.agent = "fake"
        self.diff_paths: list[str] = []
        self.diff_scopes: list[tuple[str, str, str]] = []

    def run_turn(self, prompt):
        return {"success": True, "stdout": prompt}

    def snapshot(self):
        return {
            "workspace": {"available": True, "branch": "main", "changed": 3},
            "last_turn_workspace": {
                "changed": True, "dirty_files": 3,
                "paths": ["src/app.py"], "head_changed": False,
            },
        }

    def workspace_status(self, _refresh=True):
        return {
            "available": True, "branch": "main", "changed": 3,
            "staged": 1, "unstaged": 1, "untracked": 1,
            "files": [
                {"status": " M", "path": "src/app.py"},
                {"status": "A ", "path": "src/staged.py"},
                {"status": "??", "path": "notes.txt"},
            ],
        }

    def workspace_diff(self, path=""):
        self.diff_paths.append(path)
        return {
            "path": path,
            "patch": "diff --git a/src/app.py b/src/app.py\n+after\n",
            "truncated": False, "bytes": 48, "error": "",
        }

    def workspace_diff_scope(self, scope="all", path="", target=""):
        self.diff_scopes.append((scope, path, target))
        rows = {
            "staged": [{"status": "A ", "path": "src/staged.py"}],
            "unstaged": [{"status": " M", "path": "src/app.py"}],
            "untracked": [{"status": "??", "path": "notes.txt"}],
        }.get(scope, self.workspace_status()["files"])
        selected = path or (rows[0]["path"] if rows else "")
        patch = (
            f"diff --git a/{selected} b/{selected}\n"
            "@@ -1 +1 @@ first\n-before\n+after\n"
            "@@ -5 +5 @@ second\n-old\n+new\n"
            if selected else "No diff for the selected workspace scope."
        )
        return {"scope": scope, "target": target, "path": path,
                "patch": patch, "files": rows, "files_truncated": False,
                "truncated": False, "bytes": len(patch), "error": ""}

    def new_session(self, name=""):
        return {"session_name": name or "new"}

    def cancel(self):
        return True


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(
                *pending, return_exceptions=True))
        loop.close()


def test_plan_renderer_uses_full_structured_projection_not_timeline_preview():
    timeline = TimelineLog()
    items = [{"text": f"step-{index}-" + "x" * 120, "status": "pending"}
             for index in range(30)]
    timeline.consume("plan", json.dumps({
        "item_id": "provider-plan", "status": "in_progress", "items": items,
    }), 1)

    document = render_plan_inspector(timeline.entries)

    assert len(timeline.entries[0].detail.encode("utf-8")) <= 2 * 1024
    assert "step-29-" not in timeline.entries[0].detail
    assert "step-29-" in document
    assert "in_progress" in document


def test_plan_renderer_has_explicit_empty_state():
    assert "No plan has been emitted" in render_plan_inspector(())


def test_plan_renderer_shows_explicit_execution_boundaries():
    timeline = TimelineLog()
    timeline.consume("plan", json.dumps({
        "item_id": "plan", "goal": "ship safely", "source": "provider",
        "read_scope": ["src"], "write_scope": ["src/app.py"],
        "validators": ["quality"], "gates": ["review"],
        "budget": {"attempts": 2}, "unknowns": ["deployment"],
        "items": [{"text": "implement", "status": "pending"}],
    }), 1)

    document = render_plan_inspector(timeline.entries)

    assert "Goal:" in document and "ship safely" in document
    assert "Read scope:" in document and "src" in document
    assert "Write scope:" in document and "src/app.py" in document
    assert "Validators:" in document and "quality" in document
    assert "Budget:" in document and "attempts: 2" in document
    assert "Unknowns:" in document and "deployment" in document


def test_plan_renderer_bounds_independent_plan_cards():
    timeline = TimelineLog(max_entries=32)
    for index in range(20):
        timeline.consume("plan", json.dumps({
            "item_id": f"plan-{index}",
            "items": [{"text": f"visible-step-{index}"}],
        }), index)

    document = render_plan_inspector(timeline.entries)

    assert "4 older plan(s) omitted" in document
    assert "visible-step-0" not in document
    assert "visible-step-19" in document


def test_changes_parser_supports_local_target_and_legacy_path_scopes():
    assert parse_changes_args(()).scope == "all"
    assert parse_changes_args(("staged", "src/a.py")).path == "src/a.py"
    assert parse_changes_args(("worktree",)).scope == "unstaged"
    assert parse_changes_args(("base", "main", "src/a.py")).target == "main"
    assert parse_changes_args(("commit", "HEAD")).scope == "commit"
    assert parse_changes_args(("src/a.py",)).path == "src/a.py"
    assert "`/changes [all|unstaged|staged|untracked|last-turn] [path]`" in SLASH_HELP
    assert "`/changes base|commit <ref> [path]`" in SLASH_HELP


def test_diff_hunks_support_deterministic_navigation_units():
    hunks = diff_hunks(
        "diff --git a/a b/a\n@@ -1 +1 @@ first\n-a\n+b\n"
        "@@ -8 +8 @@ second\n-c\n+d\n")

    assert len(hunks) == 2
    assert "first" in hunks[0].label and "+b" in hunks[0].text
    assert "second" in hunks[1].label and "+d" in hunks[1].text


def test_changes_renderer_keeps_last_turn_status_and_diff_separate():
    document = render_changes_inspector(
        {"available": True, "branch": "main", "changed": 0, "files": []},
        {"path": "", "patch": "No diff.", "truncated": False, "error": ""},
        {"changed": True, "dirty_files": 1, "paths": ["one.py"]},
    )

    assert "## Last turn" in document and "one.py" in document
    assert "## Workspace files" in document and "## Diff" in document


def test_plan_slash_opens_overlay_without_polluting_conversation():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=InspectorBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._timeline.consume("plan", json.dumps({
                "item_id": "roadmap", "items": [
                    {"text": "inspect", "completed": True},
                    {"text": "implement", "status": "in_progress"},
                ],
            }), 1)
            conversation = app.query_one("#conversation", Markdown)
            before = conversation.source
            composer = app.query_one("#composer", Input)
            composer.value = "/plan"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TUIInspectorScreen)
            assert app.screen.inspector_kind == "plan"
            source = app.screen.query_one("#inspector-content", Markdown).source
            assert "✓ completed · inspect" in source
            assert conversation.source == before

            app._structured_backend = True
            app._apply_backend_emit("plan", json.dumps({
                "item_id": "roadmap", "items": [
                    {"text": "inspect", "completed": True},
                    {"text": "implement", "completed": True},
                ],
            }))
            await pilot.pause()
            source = app.screen.query_one("#inspector-content", Markdown).source
            assert "2/2 completed" in source
            assert "✓ completed · implement" in source

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, TUIInspectorScreen)

    _run(scenario())


def test_changes_slash_and_ctrl_g_open_scoped_read_only_overlay():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=InspectorBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            conversation = app.query_one("#conversation", Markdown)
            before = conversation.source
            composer = app.query_one("#composer", Input)
            composer.value = "/changes src/app.py"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TUIChangesScreen)
            assert app.screen.inspector_kind == "changes"
            source = app.screen.query_one("#changes-content", Markdown).source
            assert "src/app.py" in source and "first" in source
            assert "second" not in source
            assert app.screen.query_one(
                "#changes-files", OptionList).option_count == 3
            assert app._backend.diff_scopes == [("all", "src/app.py", "")]
            assert conversation.source == before

            await pilot.press("n")
            await pilot.pause()
            source = app.screen.query_one("#changes-content", Markdown).source
            assert "second" in source and "+new" in source
            assert "hunk 2/2" in app.screen.query_one(
                "#changes-heading", Static).content

            await pilot.press("3")
            await pilot.pause()
            assert app.screen.snapshot.request.scope == "staged"
            assert app.screen.snapshot.request.path == "src/staged.py"
            assert app.screen.query_one(
                "#changes-files", OptionList).option_count == 1
            assert conversation.source == before

            await pilot.press("1")
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.screen.snapshot.request.path == "src/staged.py"

            await pilot.press("5")
            await pilot.pause()
            assert app.screen.snapshot.request.scope == "last-turn"
            assert app.screen.snapshot.request.path == "src/app.py"
            assert app.screen.query_one(
                "#changes-files", OptionList).option_count == 1
            call_count = len(app._backend.diff_scopes)
            blocked = app._load_changes_snapshot(parse_changes_args(
                ("last-turn", "notes.txt")))
            assert "not part of the authoritative" in blocked.diff["error"]
            assert len(app._backend.diff_scopes) == call_count

            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert isinstance(app.screen, TUIChangesScreen)
            assert app.screen.snapshot.request.scope == "all"
            assert app.screen.snapshot.request.path == "src/app.py"

    _run(scenario())
