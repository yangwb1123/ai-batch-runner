"""Independent, projection-backed Plan and Changes inspectors."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static

from .tui_artifacts import (
    ArtifactCatalogSnapshot,
    TUIArtifactScreen,
    parse_artifact_args,
)
from .tui_changes import (
    ChangesRequest,
    ChangesSnapshot,
    TUIChangesScreen,
    build_changes_snapshot,
    parse_changes_args,
)
from .tui_completion import (
    build_backend_completion_snapshot,
    render_completion,
)
from .tui_agents import render_workspace_diff, render_workspace_files
from .tui_view_support import clean_text, one_line


_PLAN_DISPLAY_LIMIT = 16
_DELTA_PATH_LIMIT = 20


class TUIInspectorScreen(ModalScreen[None]):
    """A bounded overlay that keeps inspection outside the conversation."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("a", "artifacts", "Artifacts", show=False),
    ]
    CSS = """
    TUIInspectorScreen {
        align: center middle;
        background: $background;
    }
    #inspector-dialog {
        width: 92%;
        height: 90%;
        min-width: 36;
        background: $surface;
        border: round $accent;
    }
    #inspector-heading {
        height: 3;
        padding: 1 2;
        text-style: bold;
        background: $boost;
    }
    #inspector-scroll {
        height: 1fr;
        padding: 0 2;
        scrollbar-gutter: stable;
    }
    #inspector-content { width: 100%; }
    #inspector-hint {
        height: 2;
        padding: 0 2;
        color: $text-muted;
    }
    """

    def __init__(self, heading: str, body: str, kind: str) -> None:
        super().__init__(id=f"{kind}-inspector")
        self.inspector_kind = kind
        self._heading = heading
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="inspector-dialog"):
            yield Static(self._heading, id="inspector-heading", markup=False)
            with VerticalScroll(id="inspector-scroll"):
                yield Markdown(self._body, id="inspector-content", open_links=False)
            yield Static(self._hint(),
                         id="inspector-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#inspector-scroll", VerticalScroll).focus()

    def update_document(self, heading: str, body: str) -> None:
        self._heading, self._body = heading, body
        if self.is_mounted:
            self.query_one("#inspector-heading", Static).update(heading)
            self.query_one("#inspector-content", Markdown).update(body)
            self.query_one("#inspector-hint", Static).update(self._hint())

    def _hint(self) -> str:
        hint = "Esc / q close · arrows and PgUp/PgDn scroll"
        return "a artifacts · " + hint if self.inspector_kind == "completion" else hint

    def action_close(self) -> None:
        self.dismiss(None)

    def action_artifacts(self) -> None:
        if self.inspector_kind != "completion":
            return
        opener = getattr(self.app, "_slash_artifacts", None)
        if callable(opener):
            opener(())


def render_plan_inspector(entries: Iterable[Any]) -> str:
    """Render current structured plan entries without parsing message text."""
    all_plans = [entry for entry in entries
                 if getattr(entry, "kind", "") == "plan"]
    plans = all_plans[-_PLAN_DISPLAY_LIMIT:]
    lines = [
        "# Plan", "",
        "Read-only current-turn projection from trusted `plan.updated` events.",
    ]
    if not plans:
        return "\n".join(lines + ["", "No plan has been emitted for this turn."])
    if len(all_plans) > len(plans):
        lines.extend(["", f"_{len(all_plans) - len(plans)} older plan(s) omitted._"])
    for index, entry in enumerate(plans, 1):
        lines.extend(_plan_section(entry, index, len(plans)))
    return "\n".join(lines)


def _plan_section(entry: Any, index: int, total: int) -> list[str]:
    plan = getattr(entry, "plan", None)
    heading = "Current plan" if total == 1 else f"Plan {index}"
    lines = ["", f"## {heading}"]
    if plan is None:
        return lines + _indented("Progress", getattr(entry, "summary", "")) + \
            _indented("Steps", getattr(entry, "detail", ""))
    if plan.status:
        lines.extend(_indented("Runtime status", plan.status))
    lines.extend(_plan_boundaries(plan))
    lines.extend(_indented("Progress", plan.summary))
    step_lines = "\n".join(step.render_line() for step in plan.steps)
    lines.extend(_indented("Steps", step_lines or "No steps were supplied."))
    if plan.truncated:
        lines.extend(["", "_Plan steps were truncated at the projection limit._"])
    return lines


def _plan_boundaries(plan: Any) -> list[str]:
    lines: list[str] = []
    for label, value in (
            ("Plan digest", plan.plan_digest), ("Goal", plan.goal),
            ("Source", plan.source), ("Version", plan.version),
            ("Read scope", plan.read_scope),
            ("Write scope", plan.write_scope), ("Commands", plan.commands),
            ("Network intent", plan.network_intent),
            ("External tools", plan.external_tools),
            ("Validators", plan.validators), ("Gates", plan.gates),
            ("Assumptions", plan.assumptions), ("Unknowns", plan.unknowns),
            ("Stop policy", plan.stop_policy)):
        rendered = _plan_boundary_value(value)
        if rendered:
            lines.extend(_indented(label, rendered))
    if plan.budget:
        lines.extend(_indented(
            "Budget", "\n".join(f"{key}: {value}" for key, value in plan.budget)))
    return lines


def _plan_boundary_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value if str(item))
    return str(value or "")


def render_changes_inspector(status: dict, diff: dict,
                             delta: Optional[dict] = None) -> str:
    """Compose existing Git projections into one read-only inspection view."""
    lines = [
        "# Changes", "",
        "Read-only workspace status and bounded unified diff.",
    ]
    lines.extend(_last_turn_section(delta))
    lines.extend(["", "---", "", render_workspace_files(status)])
    lines.extend(["", "---", "", render_workspace_diff(diff)])
    return "\n".join(lines)


def _last_turn_section(delta: Optional[dict]) -> list[str]:
    if not isinstance(delta, dict):
        return ["", "## Last turn", "", "No authoritative change snapshot is available."]
    if not delta.get("changed"):
        return ["", "## Last turn", "", "No workspace change was observed."]
    facts = [f"{delta.get('dirty_files', 0)} dirty file(s)"]
    if delta.get("head_changed"):
        facts.append("HEAD changed")
    raw_paths = delta.get("paths", ())
    raw_paths = raw_paths if isinstance(raw_paths, (list, tuple)) else ()
    paths = [_single_line(path) for path in raw_paths[:_DELTA_PATH_LIMIT]]
    body = ["", "## Last turn", ""] + [f"    {' · '.join(facts)}"]
    if paths:
        body.extend(["", "Observed paths:"])
        body.extend(f"    {path}" for path in paths)
    if delta.get("paths_truncated") or len(raw_paths) > len(paths):
        body.extend(["", "_Observed path list was truncated._"])
    return body


def _indented(label: str, value: Any) -> list[str]:
    lines = clean_text(value).splitlines() or [""]
    return ["", f"{label}:", ""] + [f"    {_single_line(line)}" for line in lines]


def _single_line(value: Any) -> str:
    return one_line(clean_text(value)) or "-"


class TUIInspectorMixin:
    """Open Plan and Changes views without adding transcript entries."""

    def _slash_plan(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("plan", args):
            return
        self._open_inspector(
            "Plan inspector", render_plan_inspector(self._active_timeline().entries),
            "plan")

    def _slash_completion(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("completion", args):
            return
        self._open_inspector(
            "Completion inspector",
            render_completion(self._completion_snapshot()), "completion")

    def _completion_snapshot(self):
        if self._projection_driven:
            return self._runtime_view.completion
        return build_backend_completion_snapshot(
            self._backend_snapshot(), self._active_timeline().entries)

    def _slash_artifacts(self, args: tuple[str, ...]) -> None:
        try:
            selected = parse_artifact_args(args)
        except ValueError as exc:
            self.notify(f"Usage: /artifacts [N] ({exc})", severity="warning")
            return
        catalog, root = self._artifact_catalog()
        if args and selected >= len(catalog.references):
            self.notify(
                f"Artifact {selected + 1} is not available in the current turn.",
                severity="warning")
            return
        current = self.screen
        if isinstance(current, TUIArtifactScreen):
            current.replace_snapshot(catalog, root, force=True)
            return
        self.push_screen(TUIArtifactScreen(
            catalog, root, self._artifact_catalog, selected))

    def _slash_artifact(self, args: tuple[str, ...]) -> None:
        self._slash_artifacts(args)

    def _artifact_catalog(self) -> tuple[ArtifactCatalogSnapshot, str]:
        if self._projection_driven:
            catalog = self._runtime_view.artifacts
        else:
            catalog = ArtifactCatalogSnapshot(
                source="typed runtime unavailable",
                diagnostics=(
                    "the active backend did not emit canonical artifact events",))
        backend_root = str(getattr(self._backend, "cwd", "") or "")
        option_root = str(getattr(self.options, "cwd", "") or "")
        return catalog, backend_root or option_root

    def _slash_changes(self, args: tuple[str, ...]) -> None:
        try:
            request = parse_changes_args(args)
        except ValueError as exc:
            self._append_entry(
                "error", f"Usage: /changes [SCOPE] [PATH] or "
                f"/changes base|commit REF [PATH] ({exc})")
            return
        native_running = self._native_pipeline_is_running()
        if ((self._busy or native_running)
                and request.scope in {"last-turn", "base", "commit"}):
            self.notify(
                "This scope is only available after the active operation settles.",
                severity="warning")
            return
        snapshot = self._load_changes_snapshot(request)
        self.push_screen(TUIChangesScreen(
            snapshot, self._load_changes_snapshot,
            live=self._busy or native_running,
            live_active=lambda: bool(self._busy or
                                     self._native_pipeline_is_running())))

    def _load_changes_snapshot(self, request: ChangesRequest) -> ChangesSnapshot:
        status = self._changes_status()
        delta = self._changes_delta()
        if request.scope == "last-turn":
            return self._load_last_turn_changes(request, status, delta)
        diff = self._changes_diff(request, request.path)
        snapshot = build_changes_snapshot(request, status, diff, delta)
        if not request.path and snapshot.request.path:
            selected = snapshot.request.path
            diff = self._changes_diff(request, selected)
            snapshot = build_changes_snapshot(
                ChangesRequest(request.scope, selected, request.target),
                status, diff, delta)
        return snapshot

    def _load_last_turn_changes(
            self, request: ChangesRequest, status: dict[str, Any],
            delta: Optional[dict[str, Any]]) -> ChangesSnapshot:
        raw = delta.get("paths", []) if isinstance(delta, dict) else []
        paths = [str(path) for path in raw if str(path)]
        selected = request.path or (paths[0] if paths else "")
        scoped = ChangesRequest("last-turn", selected)
        if not paths:
            diff = _changes_diff_error(
                "last-turn", selected, "",
                "no authoritative last-turn paths are available")
        elif selected not in paths:
            diff = _changes_diff_error(
                "last-turn", selected, "",
                "selected path is not part of the authoritative last-turn delta")
        else:
            diff = self._changes_diff(scoped, selected)
        return build_changes_snapshot(scoped, status, diff, delta)

    def _changes_status(self) -> dict[str, Any]:
        method = getattr(self._backend, "workspace_status", None)
        if not callable(method):
            return {"available": False,
                    "error": "workspace status is unavailable for this backend"}
        try:
            value = method()
        except Exception as exc:
            return {"available": False, "error": str(exc)}
        return value if isinstance(value, dict) else {"error": str(value)}

    def _changes_diff(self, request: ChangesRequest, path: str) -> dict[str, Any]:
        scope = "all" if request.scope == "last-turn" else request.scope
        method = getattr(self._backend, "workspace_diff_scope", None)
        if callable(method):
            try:
                value = method(scope, path, request.target)
            except Exception as exc:
                return _changes_diff_error(scope, path, request.target, exc)
            return value if isinstance(value, dict) else {"error": str(value)}
        legacy = getattr(self._backend, "workspace_diff", None)
        if scope == "all" and not request.target and callable(legacy):
            try:
                value = legacy(path)
            except Exception as exc:
                return _changes_diff_error(scope, path, request.target, exc)
            return value if isinstance(value, dict) else {"error": str(value)}
        return _changes_diff_error(
            scope, path, request.target,
            "the active backend does not support scoped Git comparisons")

    def _changes_delta(self) -> Optional[dict[str, Any]]:
        delta = self._projected_workspace_delta()
        if delta is not None:
            return delta
        value = self._backend_snapshot().get("last_turn_workspace")
        return value if isinstance(value, dict) else None

    def _open_inspector(self, heading: str, body: str, kind: str) -> None:
        current = self.screen
        if isinstance(current, TUIInspectorScreen):
            current.inspector_kind = kind
            current.update_document(heading, body)
            return
        self.push_screen(TUIInspectorScreen(heading, body, kind))

    def _refresh_plan_inspector(self) -> None:
        current = self.screen
        if (isinstance(current, TUIInspectorScreen)
                and current.inspector_kind == "plan"):
            current.update_document(
                "Plan inspector",
                render_plan_inspector(self._active_timeline().entries))

    def _refresh_completion_inspector(self) -> None:
        current = self.screen
        if (isinstance(current, TUIInspectorScreen)
                and current.inspector_kind == "completion"):
            current.update_document(
                "Completion inspector",
                render_completion(self._completion_snapshot()))

    def _refresh_artifact_inspector(self) -> None:
        current = self.screen
        if isinstance(current, TUIArtifactScreen):
            catalog, root = self._artifact_catalog()
            current.replace_snapshot(catalog, root)

    def action_show_diff(self) -> None:
        self._slash_changes(())

    def _palette_plan(self) -> None:
        self._slash_plan(())

    def _palette_completion(self) -> None:
        self._slash_completion(())

    def _palette_artifacts(self) -> None:
        self._slash_artifacts(())

    def _palette_changes(self) -> None:
        self._slash_changes(())


def _changes_diff_error(scope: str, path: str, target: str,
                        error: Any) -> dict[str, Any]:
    return {"scope": scope, "path": path, "target": target, "patch": "",
            "truncated": False, "files": [], "files_truncated": False,
            "error": clean_text(error)}
