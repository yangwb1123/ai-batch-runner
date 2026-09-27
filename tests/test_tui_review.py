"""TUI read-only review command contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import queue

from textual.widgets import Input, Markdown

from pbatch import review
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_changes import TUIChangesScreen
from pbatch.tui_completion import CompletionFact, CompletionSnapshot
from pbatch.tui_findings import ReviewFinding, findings_snapshot
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.user_dirs import project_runtime_dir
from pbatch.tui_review import (
    ReviewRuntimeScope,
    collect_review_fix_evidence,
    new_review_fix_context,
    render_review_fix_evidence,
    review_fix_output_path,
    write_review_fix_evidence,
    parse_review_args,
)
from pbatch.tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle
from pbatch.tui_review_findings import TUIReviewFindingsScreen


class _Backend:
    def __init__(self, emit, **options):
        self.emit = emit
        self.cwd = str(Path(options.get("cwd") or ".").resolve())
        self.agent = "pi"
        self.model = ""
        self.provider = ""
        self.memory_mode = "off"
        self.prompts = []

    def set_emit(self, emit):
        self.emit = emit

    def new_session(self, name=""):
        return {"session_name": name or "tui"}

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def cancel(self):
        return True

    def workspace_status(self):
        return {"root": self.cwd, "files": []}

    def workspace_diff(self, path=""):
        return {"path": path, "patch": "", "files": []}


class _AppServerReviewRuntime:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.session = RuntimeSession(
            "ses-review", "thread-review", "ws-review", "now", "ACTIVE")
        self.queue: queue.Queue[RuntimeEvent] = queue.Queue()
        self.requests = []
        self._info = RuntimeInfo(
            "codex-app-server", 1,
            RuntimeCapabilities(runtime="codex-app-server", capabilities={"review": True}),
        )

    def runtime_info(self):
        return self._info

    def initialize(self, _request):
        return self._info

    def start_session(self, _request):
        return self.session

    def start_review(self, request):
        self.requests.append(dict(request))
        handle = TurnHandle(
            self.session.session_id, request["run_id"], request["turn_id"],
            "agent_main", "now")
        scope = {
            "workspace_id": self.session.workspace_id,
            "session_id": self.session.session_id,
            "run_id": handle.run_id,
            "turn_id": handle.turn_id,
            "actor_id": handle.actor_id,
        }
        values = [
            ("turn.started", {"state": TurnState.RUNNING}),
            ("message.completed", {
                "item_id": "review-message",
                "text": ('```json\\n[{"severity":"high","file":"src/a.py",'
                         '"line":7,"problem":"unsafe input"}]\\n```'),
            }),
            ("review.finding", {
                "severity": "high", "file": "src/a.py", "line": 7,
                "problem": "unsafe input",
            }),
            ("turn.completed", {"state": TurnState.SUCCEEDED}),
        ]
        for index, (kind, payload) in enumerate(values, 1):
            self.queue.put(RuntimeEvent(
                event_id=f"evt_review_{index}", seq=index, occurred_at="now",
                payload=payload, kind=kind, **scope))
        handle.state = TurnState.SUCCEEDED
        handle._done.set()
        return handle

    def start_turn(self, request):
        run_id, turn_id = request["run_id"], request["turn_id"]
        handle = TurnHandle(
            self.session.session_id, run_id, turn_id, "agent_main", "now")
        scope = {
            "workspace_id": self.session.workspace_id,
            "session_id": self.session.session_id,
            "run_id": run_id, "turn_id": turn_id, "actor_id": handle.actor_id,
        }
        values = [
            ("turn.started", {"state": TurnState.RUNNING}),
            ("validation.started", {"item_id": "pytest", "validator": "pytest"}),
            ("validation.completed", {
                "item_id": "pytest", "validator": "pytest", "success": True}),
            ("gate.verdict", {"gate": "quality", "verdict": "PASS"}),
            ("turn.completed", {"state": TurnState.SUCCEEDED}),
            ("workspace.snapshot", {
                "phase": "after", "delta": {
                    "changed": True, "dirty_files": 1, "paths": ["src/a.py"]}}),
        ]
        for index, (kind, payload) in enumerate(values, 1):
            extensions = {"authoritative": True} if kind == "workspace.snapshot" else {}
            self.queue.put(RuntimeEvent(
                event_id=f"evt_fix_{index}", seq=index, occurred_at="now",
                payload=payload, kind=kind, extensions=extensions, **scope))
        handle.state = TurnState.SUCCEEDED
        handle._done.set()
        return handle

    def events(self):
        return self

    def get(self, timeout=None):
        return self.queue.get(timeout=timeout)

    def close(self):
        return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def test_review_scope_parser_is_explicit_and_bounded():
    assert parse_review_args(()).scope == "default"
    assert parse_review_args(("uncommitted",)).scope_argv == ("--uncommitted",)
    assert parse_review_args(("base", "main")).scope == "base:main"
    assert parse_review_args(("custom", "check", "auth")).scope_argv == ("check auth",)


def test_review_fix_evidence_requires_exact_terminal_runtime_identity(tmp_path):
    finding = ReviewFinding(
        "high", "src/app.py", 7, "unsafe input", "typed evidence", "validate")
    source = ReviewRuntimeScope("ws", "session", "review-run", "review-turn", "agent")
    context = new_review_fix_context(
        finding, source_scope=source, source_report="docs/reviews/review.md")
    fix_scope = ReviewRuntimeScope("ws", "session", "fix-run", "fix-turn", "agent")
    completion = CompletionSnapshot(
        source="runtime events", state=TurnState.SUCCEEDED,
        run_id="fix-run", turn_id="fix-turn", actor_id="agent",
        validations=(CompletionFact("pytest", "passed"),),
        gates=(CompletionFact("quality", "passed"),))

    evidence = collect_review_fix_evidence(
        context, fix_scope=fix_scope, completion=completion)
    assert evidence.associated and evidence.source_associated
    assert evidence.completion is completion
    rendered = render_review_fix_evidence(evidence)
    assert "Exact fix-turn association: yes" in rendered
    assert "Finding resolution: not claimed" in rendered
    assert "pytest" in rendered and "quality" in rendered

    path = review_fix_output_path(tmp_path, context.fix_id)
    write_review_fix_evidence(path, evidence)
    assert path.is_relative_to(project_runtime_dir(str(tmp_path)))
    assert "runtime events" in path.read_text(encoding="utf-8")

    mismatch = collect_review_fix_evidence(
        context, fix_scope=ReviewRuntimeScope(
            "ws", "session", "other-run", "fix-turn", "agent"),
        completion=completion)
    assert not mismatch.associated and mismatch.completion is None
    cross_session = collect_review_fix_evidence(
        context, fix_scope=ReviewRuntimeScope(
            "other-ws", "other-session", "fix-run", "fix-turn", "agent"),
        completion=completion)
    assert not cross_session.associated and not cross_session.source_associated


def test_appserver_review_uses_runtime_events_and_associates_findings(tmp_path):
    runtime = None

    def factory(cwd):
        nonlocal runtime
        runtime = _AppServerReviewRuntime(cwd)
        return runtime

    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), runtime="app-server"),
            backend_factory=_Backend, appserver_factory=factory)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/review uncommitted"
            await pilot.press("enter")
            for _ in range(600):
                if not app._busy:
                    break
                await pilot.pause(0.01)
            assert not app._busy and not app._review_busy
            assert runtime is not None and len(runtime.requests) == 1
            assert runtime.requests[0]["scope"] == "uncommitted"
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED
            assert app._runtime_view.run_id.startswith("review-run-")
            assert len(app._runtime_view.findings.findings) == 1
            assert app._runtime_view.findings.findings[0].file == "src/a.py"
            reports = list((project_runtime_dir(str(tmp_path)) /
                            "reviews").glob("review-*.md"))
            assert len(reports) == 1
            report = reports[0].read_text(encoding="utf-8")
            assert "src/a.py:7" in report
            assert "Runtime run_id:" in report
            app._slash_findings(())
            await pilot.pause()
            await pilot.press("f")
            for _ in range(600):
                if not app._busy:
                    break
                await pilot.pause(0.01)
            assert not app._busy
            assert app._review_fix_evidence is not None
            assert app._review_fix_evidence.associated
            assert app._review_fix_evidence.source_associated
            assert len(app._review_fix_evidence.completion.validations) == 1
            fix_reports = list((project_runtime_dir(str(tmp_path)) /
                               "reviews").glob("review-fix-*.md"))
            assert len(fix_reports) == 1
            fix_report = fix_reports[0].read_text(encoding="utf-8")
            assert "Source review association: yes" in fix_report
            assert "pytest" in fix_report
            app._prepare_runtime_session_switch()
            snapshot = app._event_controller.snapshot()
            assert snapshot.workspace.changed
            assert not app._runtime_blocked_reason

    _run(scenario())


def test_findings_picker_jumps_to_bounded_changes_location(tmp_path):
    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=_Backend)
        app._review_findings = findings_snapshot([{
            "severity": "high", "file": "src/app.py", "line": "7",
            "problem": "unsafe input",
        }])
        async with app.run_test(size=(100, 30)) as pilot:
            app._slash_findings(())
            await pilot.pause()
            assert isinstance(app.screen, TUIReviewFindingsScreen)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TUIChangesScreen)
            assert app.screen.snapshot.request.path == "src/app.py"
            assert app.screen.snapshot.request.line == 7

    _run(scenario())


def test_findings_picker_fix_starts_an_ordinary_turn(tmp_path):
    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=_Backend)
        app._review_findings = findings_snapshot([{
            "severity": "high", "file": "src/app.py", "line": 7,
            "problem": "unsafe input", "fix_hint": "validate it",
        }])
        async with app.run_test(size=(100, 30)) as pilot:
            app._slash_findings(())
            await pilot.pause()
            await pilot.press("f")
            for _ in range(600):
                if not app._busy:
                    break
                await pilot.pause(0.01)

            assert len(app._backend.prompts) == 1
            prompt = app._backend.prompts[0]
            assert "untrusted review data" in prompt
            assert "file: src/app.py" in prompt
            assert "line: 7" in prompt
            assert not app._review_busy
            assert app._review_fix_evidence is not None
            assert app._review_fix_evidence.associated
            reports = list((project_runtime_dir(str(tmp_path)) /
                            "reviews").glob("review-fix-*.md"))
            assert len(reports) == 1
            assert "Finding resolution: not claimed" in reports[0].read_text(
                encoding="utf-8")

    _run(scenario())


def test_tui_review_runs_read_only_and_persists_report(tmp_path):
    calls = []

    def runner(argv, cwd, timeout, capture):
        calls.append((list(argv), cwd, timeout))
        capture.consume_jsonl(json.dumps({
            "type": "turn.started", "item": {"type": "status"}}))
        capture.consume_jsonl(json.dumps({
            "type": "item.completed", "item": {
                "type": "agent_message",
                "text": '```json\n[{"severity":"high","file":"a.py",'
                        '"line":"7","problem":"unsafe path",'
                        '"evidence":"test","fix_hint":"validate"}]\n```',
            }}))
        capture.consume_jsonl(json.dumps({"type": "turn.completed"}))
        return review.RunOutcome(0, 12)

    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path)),
            backend_factory=_Backend,
            review_runner=runner,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/review uncommitted"
            await pilot.press("enter")
            for _ in range(100):
                if not app._busy:
                    break
                await pilot.pause(0.01)
            assert not app._busy
            source = app.query_one("#conversation", Markdown).source
            assert "unsafe path" in source
            assert "Report:" in source
            reports = list((project_runtime_dir(str(tmp_path)) /
                            "reviews").glob("review-*.md"))
            assert len(reports) == 1
            assert "unsafe path" in reports[0].read_text(encoding="utf-8")

    _run(scenario())
    assert calls and calls[0][0][-1] == "--uncommitted"
