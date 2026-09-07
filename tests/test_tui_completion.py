"""Completion projection and inspector contracts for the product TUI."""

from __future__ import annotations

import asyncio
import math
import threading
from types import SimpleNamespace

from textual.widgets import Input, Markdown

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_completion import (
    CompletionSnapshot,
    build_backend_completion_snapshot,
    build_completion_snapshot,
    render_completion,
)
from pbatch.tui_controller import TUIEventController
from pbatch.tui_inspector import TUIInspectorScreen
from pbatch.tui_protocol import RuntimeEvent, TurnState
from pbatch.tui_shell import SLASH_HELP


def _event(seq: int, kind: str, payload=None) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt_completion_{seq}", seq=seq,
        occurred_at=f"2026-08-25T12:00:{seq:02d}Z",
        workspace_id="ws_completion", session_id="ses_completion",
        run_id="run_completion", turn_id="turn_completion",
        actor_id="agent_main", kind=kind, payload=payload or {})


class CompletionBackend:
    def __init__(self, emit, **_options):
        self.emit = emit
        self.cwd = str(_options.get("cwd") or "/tmp/completion-workspace")
        self.session_id = "provider-completion"
        self.started = threading.Event()
        self.release = threading.Event()
        self.changed = False

    def set_emit(self, emit):
        self.emit = emit

    def new_session(self, _name=""):
        return {"session_id": self.session_id}

    def run_turn(self, _prompt):
        self.started.set()
        self.release.wait(timeout=5)
        self.changed = True
        return {"success": True, "stdout": "done", "stderr": "",
                "elapsed": 0.25, "returncode": 0, "reason": ""}

    def workspace_status(self):
        files = ([{"status": " M", "path": "changed.py"}]
                 if self.changed else [])
        return {"available": True, "root": self.cwd, "branch": "main",
                "head": "abc", "changed": len(files), "files": files}

    def workspace_diff(self, _path=""):
        return {"patch": "+changed\n" if self.changed else "", "files": (
            [{"status": " M", "path": "changed.py"}] if self.changed else [])}

    def snapshot(self):
        return {"status": "succeeded" if self.changed else "running",
                "session_id": self.session_id,
                "last_turn_workspace": {
                    "changed": self.changed, "dirty_files": int(self.changed),
                    "paths": ["changed.py"] if self.changed else [],
                    "head_changed": False,
                }}

    def cancel(self):
        self.release.set()
        return True


def test_controller_builds_completion_from_typed_evidence_only():
    controller = TUIEventController(entity_limit=64)
    events = [
        _event(1, "turn.started"),
        _event(2, "message.completed", {"text": (
            "validation.completed PASS; gate.verdict PASS; artifact.created")}),
        _event(3, "command.started", {"item_id": "cmd", "command": "pytest"}),
        _event(4, "command.completed", {
            "item_id": "cmd", "command": "pytest", "exit_code": 0}),
        _event(5, "tool.started", {"item_id": "tool", "tool": "search"}),
        _event(6, "tool.failed", {
            "item_id": "tool", "tool": "search", "reason": "offline"}),
        _event(7, "validation.started", {
            "item_id": "check", "validator": "pytest"}),
        _event(8, "validation.completed", {
            "item_id": "check", "validator": "pytest", "success": True}),
        _event(9, "gate.verdict", {"gate": "quality", "verdict": "PASS"}),
        _event(10, "plan.updated", {"item_id": "plan", "items": [
            {"text": "implement", "completed": True},
            {"text": "ship", "status": "pending"},
        ]}),
        _event(11, "artifact.created", {
            "path": "reports/result.json", "media_type": "application/json"}),
        _event(12, "usage.updated", {
            "usage": {"input_tokens": 10, "output_tokens": 4}}),
        _event(13, "runtime.warning", {
            "category": "network", "reason": "retry observed"}),
        _event(14, "turn.completed", {
            "elapsed_ms": 1250, "returncode": 0}),
        RuntimeEvent(
            event_id="evt_completion_workspace", seq=15,
            occurred_at="2026-08-25T12:00:15Z",
            workspace_id="ws_completion", session_id="ses_completion",
            run_id="run_completion", turn_id="turn_completion",
            actor_id="agent_main", kind="workspace.snapshot", payload={
                "phase": "after", "workspace": {
                    "available": True, "root": "/repo", "branch": "main"},
                "delta": {"changed": True, "dirty_files": 1,
                          "paths": ["actual.py"]}},
            extensions={"runtime": "test", "authoritative": True}),
    ]
    view = None
    for event in events:
        view = controller.ingest(event)

    assert view is not None
    snapshot = view.completion
    assert snapshot.state == TurnState.SUCCEEDED and snapshot.terminal
    assert snapshot.elapsed_ms == 1250 and snapshot.returncode == 0
    assert snapshot.commands[0].status == "passed"
    assert snapshot.tools[0].status == "failed"
    assert len(snapshot.validations) == 1
    assert snapshot.validations[0].label == "pytest"
    assert snapshot.validations[0].status == "passed"
    assert snapshot.gates[0].status == "passed"
    assert snapshot.changed_paths == ("actual.py",)
    assert snapshot.plan_steps[-1].state == "pending"
    assert dict(snapshot.usage) == {"input_tokens": 10.0, "output_tokens": 4.0}
    assert snapshot.artifacts[0].label == "reports/result.json"
    assert snapshot.diagnostics[0].label == "network"

    document = render_completion(snapshot)
    assert "State: `SUCCEEDED`" in document
    assert "No validation events" not in document
    assert "reports/result.json" in document and "actual.py" in document
    assert "ship" in document and "retry observed" in document
    assert "validation.completed PASS" not in document


def test_completion_bounds_every_dynamic_collection_and_nonfinite_usage():
    timeline = tuple(SimpleNamespace(
        kind="command", summary=f"command-{index}", exit_code=0)
        for index in range(40))
    events = [_simple_event(0, "turn.started", {})]
    events.extend(_simple_event(index + 1, "validation.completed", {
        "item_id": f"check-{index}", "validator": f"validator-{index}",
        "success": True,
    }) for index in range(40))
    events.extend(_simple_event(index + 50, "runtime.warning", {
        "category": f"warning-{index}", "reason": "bounded",
    }) for index in range(20))
    usage = {f"metric_{index}": index for index in range(40)}
    usage["nan"] = math.nan

    snapshot = build_completion_snapshot(
        source="runtime events", state=TurnState.SUCCEEDED,
        run_id="run", turn_id="turn", timeline=timeline,
        workspace={"phase": "after", "changed": True,
                   "paths": [f"path-{index}" for index in range(70)]},
        events=events, usage=usage)

    assert len(snapshot.commands) == 32
    assert len(snapshot.validations) == 32
    assert len(snapshot.diagnostics) == 16
    assert len(snapshot.changed_paths) == 64
    assert len(snapshot.usage) == 32
    assert "nan" not in dict(snapshot.usage)
    assert {"commands", "validations", "diagnostics", "changes", "usage"} <= set(
        snapshot.truncated_sections)


def test_active_callback_fallback_does_not_reuse_previous_result():
    snapshot = build_backend_completion_snapshot({
        "status": "running", "running": True,
        "last_result": {"success": True, "returncode": 0, "elapsed": 9},
        "last_turn_workspace": {
            "changed": True, "paths": ["stale.py"], "dirty_files": 1},
    }, ())

    assert snapshot.state == TurnState.RUNNING
    assert snapshot.elapsed_ms is None and snapshot.returncode is None
    assert snapshot.workspace_phase == ""
    assert snapshot.changed_paths == ()


def test_completion_slash_updates_live_without_transcript_pollution(tmp_path):
    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)),
                         backend_factory=CompletionBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            assert app._start_turn("implement safely")
            await _wait_until(app._backend.started.is_set, pilot)
            await _wait_until(
                lambda: app._runtime_view.turn_state == TurnState.RUNNING,
                pilot)
            composer = app.query_one("#composer", Input)
            composer.value = "/completion"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TUIInspectorScreen)
            assert app.screen.inspector_kind == "completion"
            source = app.screen.query_one("#inspector-content", Markdown).source
            assert "State: `RUNNING` (current)" in source
            assert "No validation events were emitted" in source
            assert "## Completion" not in app.query_one(
                "#conversation", Markdown).source

            app._backend.release.set()
            await _wait_until(lambda: not app._busy, pilot)
            source = app.screen.query_one("#inspector-content", Markdown).source
            assert "State: `SUCCEEDED`" in source
            assert "changed.py" in source
            assert "Return code: `0`" in source
            assert "## Completion" not in app.query_one(
                "#conversation", Markdown).source

    _run(scenario())


def test_completion_help_exposes_evidence_inspector():
    assert "`/completion`" in SLASH_HELP


def _simple_event(index: int, kind: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind, payload=payload, extensions={}, run_id="run", turn_id="turn")


async def _wait_until(predicate, pilot, attempts=600):
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


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
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
