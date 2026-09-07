"""Scoped, keyboard-driven Changes Inspector for the product TUI."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .tui_view_support import clean_text, one_line


CHANGES_LOCAL_SCOPES = frozenset({
    "all", "unstaged", "staged", "untracked", "last-turn",
})
CHANGES_TARGET_SCOPES = frozenset({"base", "commit"})
_CHANGE_FILE_LIMIT = 200
_PATCH_DISPLAY_MAX_BYTES = 256 * 1024
_HUNK_LIMIT = 256


@dataclass(frozen=True)
class ChangesRequest:
    scope: str = "all"
    path: str = ""
    target: str = ""
    line: int = 0


@dataclass(frozen=True)
class ChangeFile:
    path: str
    status: str = ""


@dataclass(frozen=True)
class DiffHunk:
    label: str
    text: str


@dataclass(frozen=True)
class ChangesSnapshot:
    request: ChangesRequest
    status: dict[str, Any]
    diff: dict[str, Any]
    delta: Optional[dict[str, Any]]
    files: tuple[ChangeFile, ...]
    files_truncated: bool = False


def parse_changes_args(args: tuple[str, ...]) -> ChangesRequest:
    """Parse `/changes` while preserving the legacy one-path shorthand."""
    if not args:
        return ChangesRequest()
    first = _scope_alias(args[0])
    if first in CHANGES_TARGET_SCOPES:
        if len(args) not in {2, 3}:
            raise ValueError(f"{first} scope requires REF and optional PATH")
        return _validate_request(ChangesRequest(
            first, args[2] if len(args) == 3 else "", args[1]))
    if first in CHANGES_LOCAL_SCOPES:
        if len(args) > 2:
            raise ValueError(f"{first} scope accepts at most one PATH")
        return _validate_request(ChangesRequest(
            first, args[1] if len(args) == 2 else ""))
    if len(args) == 1:
        return _validate_request(ChangesRequest(path=args[0]))
    raise ValueError("expected [SCOPE] [PATH] or base|commit REF [PATH]")


def build_changes_snapshot(request: ChangesRequest, status: dict[str, Any],
                           diff: dict[str, Any],
                           delta: Optional[dict[str, Any]]) -> ChangesSnapshot:
    """Normalize backend payloads into one bounded immutable screen model."""
    rows, truncated = _change_rows(request, status, diff, delta)
    files = tuple(ChangeFile(path, state) for path, state in rows)
    selected = request.path or (files[0].path if files else "")
    normalized = replace(request, path=selected)
    return ChangesSnapshot(
        normalized, dict(status), dict(diff),
        dict(delta) if isinstance(delta, dict) else None,
        files, truncated,
    )


class TUIChangesScreen(ModalScreen[None]):
    """Browse one bounded Git scope without adding transcript messages."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("r", "refresh", "Refresh", priority=True),
        Binding("n", "next_hunk", "Next hunk", priority=True),
        Binding("p", "previous_hunk", "Previous hunk", priority=True),
        Binding("1", "scope_all", "All", priority=True),
        Binding("2", "scope_unstaged", "Unstaged", priority=True),
        Binding("3", "scope_staged", "Staged", priority=True),
        Binding("4", "scope_untracked", "Untracked", priority=True),
        Binding("5", "scope_last_turn", "Last turn", priority=True),
    ]
    CSS = """
    TUIChangesScreen { align: center middle; background: $background; }
    #changes-dialog {
        width: 94%; height: 92%; min-width: 42;
        background: $surface; border: round $accent;
    }
    #changes-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #changes-body { height: 1fr; }
    #changes-files {
        width: 34%; min-width: 24; height: 1fr;
        border-right: solid $panel-lighten-1;
    }
    #changes-scroll {
        width: 1fr; height: 1fr; padding: 0 2; scrollbar-gutter: stable;
    }
    #changes-content { width: 100%; }
    #changes-hint { height: 3; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, snapshot: ChangesSnapshot,
                 loader: Callable[[ChangesRequest], ChangesSnapshot], *,
                 live: bool = False,
                 live_active: Optional[Callable[[], bool]] = None) -> None:
        super().__init__(id="changes-inspector")
        self.inspector_kind = "changes"
        self.snapshot = snapshot
        self._loader = loader
        self._live = bool(live)
        self._live_active = live_active or (lambda: False)
        self._refresh_generation = 0
        self._refresh_inflight = False
        self._hunk_index = hunk_index_for_line(
            snapshot.diff.get("patch", ""), snapshot.request.line)

    def compose(self) -> ComposeResult:
        with Vertical(id="changes-dialog"):
            yield Static(self._heading(), id="changes-heading", markup=False)
            with Horizontal(id="changes-body"):
                yield OptionList(*self._options(), id="changes-files", markup=False)
                with VerticalScroll(id="changes-scroll"):
                    yield Markdown(self._document(), id="changes-content",
                                   open_links=False)
            hint = (
                "↑/↓ file · Enter open · n/p hunk · 1 all · 2 unstaged · "
                "3 staged · 4 untracked · 5 last turn · r refresh · Esc close")
            if self._live:
                hint = "Live workspace sampling (not atomic) · " + hint
            yield Static(hint, id="changes-hint", markup=False)

    def on_mount(self) -> None:
        options = self.query_one("#changes-files", OptionList)
        self._select_current(options)
        options.focus()
        if self._live:
            # Git snapshots are intentionally sampled off the UI thread.  A
            # live view is advisory and never claims an atomic workspace view.
            self.set_interval(0.75, self._poll_live)

    def on_unmount(self) -> None:
        self._refresh_generation += 1
        self._live = False

    def _poll_live(self) -> None:
        try:
            active = bool(self._live_active())
        except Exception:
            active = False
        if not active or self._refresh_inflight:
            return
        self._start_async_reload(self.snapshot.request)

    def _start_async_reload(self, request: ChangesRequest) -> None:
        if not self.is_mounted or self._refresh_inflight:
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._refresh_inflight = True

        def load() -> None:
            try:
                snapshot = self._loader(request)
                error = None
            except Exception as exc:  # report only on the UI thread
                snapshot, error = None, exc
            try:
                self.call_from_thread(
                    self._apply_async_snapshot, request, generation,
                    snapshot, error)
            except RuntimeError:
                pass  # the screen was closed while Git was being sampled

        self.run_worker(load, name="changes-refresh", group="changes",
                        exit_on_error=False, exclusive=False, thread=True)

    def _apply_async_snapshot(self, request: ChangesRequest, generation: int,
                              snapshot: Any, error: Optional[Exception]) -> None:
        if generation != self._refresh_generation or not self.is_mounted:
            return
        self._refresh_inflight = False
        if error is not None:
            self.notify(f"Unable to refresh changes: {error}", severity="error")
            return
        if not isinstance(snapshot, ChangesSnapshot):
            self.notify("Changes loader returned an invalid snapshot.",
                        severity="error")
            return
        self.snapshot = snapshot
        self._hunk_index = hunk_index_for_line(
            snapshot.diff.get("patch", ""), snapshot.request.line)
        options = self.query_one("#changes-files", OptionList)
        options.set_options(self._options())
        self._select_current(options)
        self._refresh_document()

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if 0 <= index < len(self.snapshot.files):
            request = replace(
                self.snapshot.request, path=self.snapshot.files[index].path)
            self._reload(request)

    def action_refresh(self) -> None:
        self._reload(self.snapshot.request)

    def action_next_hunk(self) -> None:
        self._move_hunk(1)

    def action_previous_hunk(self) -> None:
        self._move_hunk(-1)

    def action_scope_all(self) -> None:
        self._switch_scope("all")

    def action_scope_unstaged(self) -> None:
        self._switch_scope("unstaged")

    def action_scope_staged(self) -> None:
        self._switch_scope("staged")

    def action_scope_untracked(self) -> None:
        self._switch_scope("untracked")

    def action_scope_last_turn(self) -> None:
        self._switch_scope("last-turn")

    def action_close(self) -> None:
        self.dismiss(None)

    def _switch_scope(self, scope: str) -> None:
        if self._live and scope == "last-turn":
            self.notify(
                "Last-turn changes are available after the active turn settles.",
                severity="warning")
            return
        self._reload(ChangesRequest(scope=scope))

    def _reload(self, request: ChangesRequest) -> None:
        if self._live:
            self._start_async_reload(request)
            return
        try:
            snapshot = self._loader(request)
        except Exception as exc:
            self.notify(f"Unable to refresh changes: {exc}", severity="error")
            return
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: Any) -> None:
        if not isinstance(snapshot, ChangesSnapshot):
            self.notify("Changes loader returned an invalid snapshot.",
                        severity="error")
            return
        self.snapshot = snapshot
        self._hunk_index = hunk_index_for_line(
            snapshot.diff.get("patch", ""), snapshot.request.line)
        options = self.query_one("#changes-files", OptionList)
        options.set_options(self._options())
        self._select_current(options)
        self._refresh_document()

    def _move_hunk(self, offset: int) -> None:
        hunks = diff_hunks(self.snapshot.diff.get("patch", ""))
        if len(hunks) <= 1:
            self.notify("The selected diff has no additional hunks.")
            return
        self._hunk_index = (self._hunk_index + offset) % len(hunks)
        self._refresh_document()

    def _refresh_document(self) -> None:
        self.query_one("#changes-heading", Static).update(self._heading())
        self.query_one("#changes-content", Markdown).update(self._document())
        self.query_one("#changes-scroll", VerticalScroll).scroll_home(
            animate=False)

    def _heading(self) -> str:
        request = self.snapshot.request
        target = f" {one_line(request.target)}" if request.target else ""
        path = f" · {one_line(request.path)}" if request.path else ""
        line = f":{request.line}" if request.line else ""
        hunks = diff_hunks(self.snapshot.diff.get("patch", ""))
        hunk = (f" · hunk {min(self._hunk_index + 1, len(hunks))}/{len(hunks)}"
                if hunks else "")
        live = " · LIVE (sampled)" if self._live else ""
        return f"Changes · {request.scope}{live}{target}{path}{line}{hunk}"

    def _document(self) -> str:
        return render_changes_detail(self.snapshot, self._hunk_index)

    def _options(self) -> list[Option]:
        return [Option(_file_label(item), id=f"change-{index}")
                for index, item in enumerate(self.snapshot.files)]

    def _select_current(self, options: OptionList) -> None:
        path = self.snapshot.request.path
        index = next((number for number, item in enumerate(self.snapshot.files)
                      if item.path == path), None)
        if index is not None:
            options.highlighted = index


def render_changes_detail(snapshot: ChangesSnapshot, hunk_index: int = 0) -> str:
    """Render one selected file/hunk with explicit scope and trust source."""
    request, status, diff = snapshot.request, snapshot.status, snapshot.diff
    lines = ["# Changes", "", _scope_summary(request, diff), "",
             _workspace_summary(status), "", _last_turn_summary(snapshot.delta)]
    error = one_line(clean_text(diff.get("error", "")))
    if error:
        return "\n".join(lines + ["", "## Diff unavailable", "", error])
    hunks = diff_hunks(diff.get("patch", ""))
    selected = hunks[hunk_index % len(hunks)] if hunks else None
    title = selected.label if selected else "No diff"
    patch = selected.text if selected else "No diff for the selected scope."
    lines.extend(["", "## Diff statistics", "",
                  _diff_statistics(diff, snapshot.files), ""])
    if _is_binary_patch(patch):
        lines.extend(["## Binary diff", "",
                      "Binary content is present; text preview is unavailable."])
    lines.extend(["", f"## {_markdown_text(title)}", "",
                  _diff_fence(patch)])
    raw_patch = clean_text(diff.get("patch", ""))
    if (diff.get("truncated")
            or len(raw_patch.encode("utf-8", errors="replace"))
            > _PATCH_DISPLAY_MAX_BYTES):
        lines.extend(["", "_Diff truncated at the bounded output limit._"])
    if snapshot.files_truncated:
        lines.extend(["", "_Changed file list was truncated._"])
    return "\n".join(lines)


_HUNK_RANGE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def hunk_index_for_line(value: Any, line: int) -> int:
    """Select the closest added-line hunk for a validated finding location."""
    if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
        return 0
    hunks = diff_hunks(value)
    selected = 0
    for index, hunk in enumerate(hunks):
        match = _HUNK_RANGE.match(hunk.label)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or 1)
        if line < start:
            return selected
        selected = index
        if line <= start + max(0, count - 1):
            return index
    return selected


def diff_hunks(value: Any) -> tuple[DiffHunk, ...]:
    """Split a bounded unified diff for deterministic next/previous navigation."""
    text = _bounded_patch(clean_text(value))
    if not text.strip():
        return ()
    if text.startswith("No diff for the selected"):
        return ()
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("@@")]
    if not starts:
        return (DiffHunk("Full diff", text),)
    hunks = []
    for position, start in enumerate(starts[:_HUNK_LIMIT]):
        end = _hunk_end(lines, start, starts, position)
        label = one_line(lines[start])[:200] or f"Hunk {position + 1}"
        hunks.append(DiffHunk(label, "\n".join(lines[start:end])))
    return tuple(hunks)


def _hunk_end(lines: list[str], start: int, starts: list[int], position: int) -> int:
    next_hunk = starts[position + 1] if position + 1 < len(starts) else len(lines)
    next_file = next((index for index in range(start + 1, next_hunk)
                      if lines[index].startswith("diff --git ")), next_hunk)
    return min(next_hunk, next_file)


def _change_rows(request: ChangesRequest, status: dict[str, Any],
                 diff: dict[str, Any],
                 delta: Optional[dict[str, Any]]) -> tuple[list[tuple[str, str]], bool]:
    if request.scope == "last-turn":
        values = delta.get("paths", []) if isinstance(delta, dict) else []
        raw_rows = [{"path": path, "status": "turn"} for path in values]
        truncated = bool(isinstance(delta, dict) and delta.get("paths_truncated"))
    else:
        value = diff.get("files")
        raw_rows = value if isinstance(value, list) else status.get("files", [])
        truncated = bool(diff.get("files_truncated") or status.get("truncated"))
    rows = _normalize_rows(raw_rows, request.scope)
    if request.path and request.path not in {path for path, _ in rows}:
        rows.insert(0, (request.path, "selected"))
    return rows[:_CHANGE_FILE_LIMIT], truncated or len(rows) > _CHANGE_FILE_LIMIT


def _normalize_rows(value: Any, scope: str) -> list[tuple[str, str]]:
    rows, seen = [], set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not _row_matches_scope(item, scope):
            continue
        path = str(item.get("path", ""))
        if (not path or "\0" in path or path in seen
                or len(path.encode("utf-8", errors="replace")) > 4096):
            continue
        rows.append((path, one_line(clean_text(item.get("status", "")))[:12]))
        seen.add(path)
    return rows


def _row_matches_scope(row: dict[str, Any], scope: str) -> bool:
    status = str(row.get("status", "  ")).ljust(2)
    if scope == "staged":
        return bool(row.get("staged", status[0] not in {" ", "?"}))
    if scope == "unstaged":
        return bool(row.get("unstaged", status[1] not in {" ", "?"}))
    if scope == "untracked":
        return bool(row.get("untracked", status[:2] == "??"))
    return True


def _scope_alias(value: str) -> str:
    return {"worktree": "unstaged", "index": "staged", "turn": "last-turn",
            "last": "last-turn"}.get(str(value).lower(), str(value).lower())


def _validate_request(request: ChangesRequest) -> ChangesRequest:
    path = str(request.path or "")
    if "\0" in path or len(path.encode("utf-8", errors="replace")) > 4096:
        raise ValueError("PATH must not contain NUL or exceed 4096 bytes")
    return request


def _scope_summary(request: ChangesRequest, diff: dict[str, Any]) -> str:
    target = f" · target `{_markdown_text(request.target)}`" if request.target else ""
    resolved = one_line(clean_text(diff.get("resolved_target", "")))[:12]
    revision = f" · resolved `{resolved}`" if resolved else ""
    path = f" · path `{_markdown_text(request.path)}`" if request.path else ""
    return (f"Scope: **{request.scope}**{target}{revision}{path} · "
            "read-only Git evidence")


def _workspace_summary(status: dict[str, Any]) -> str:
    if status.get("error"):
        return f"Workspace unavailable: {one_line(clean_text(status['error']))}"
    return (f"Branch `{_markdown_text(status.get('branch', 'unknown'))}` · "
            f"changed {status.get('changed', 0)} · staged {status.get('staged', 0)} · "
            f"unstaged {status.get('unstaged', 0)} · "
            f"untracked {status.get('untracked', 0)}")


def _last_turn_summary(delta: Optional[dict[str, Any]]) -> str:
    if not isinstance(delta, dict):
        return "Last turn: no authoritative workspace delta is available."
    if not delta.get("changed"):
        return "Last turn: no workspace change was observed."
    head = " · HEAD changed" if delta.get("head_changed") else ""
    return f"Last turn: {delta.get('dirty_files', 0)} dirty file(s){head}."


def _bounded_patch(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _PATCH_DISPLAY_MAX_BYTES:
        return value
    return encoded[:_PATCH_DISPLAY_MAX_BYTES - 3].decode(
        "utf-8", errors="ignore") + "…"


def _diff_fence(value: str) -> str:
    fence = "```"
    while fence in value:
        fence += "`"
    return f"{fence}diff\n{value}\n{fence}"


def _diff_statistics(diff: dict[str, Any], files: tuple[ChangeFile, ...]) -> str:
    patch = clean_text(diff.get("patch", ""))
    additions = sum(line.startswith("+") and not line.startswith("+++")
                    for line in patch.splitlines())
    deletions = sum(line.startswith("-") and not line.startswith("---")
                    for line in patch.splitlines())
    size = diff.get("bytes", len(patch.encode("utf-8", errors="replace")))
    try:
        size = max(0, int(size))
    except (TypeError, ValueError, OverflowError):
        size = 0
    suffix = " · truncated" if diff.get("truncated") else ""
    return (f"files {len(files)} · +{additions}/-{deletions} · "
            f"patch {size} bytes{suffix}")


def _is_binary_patch(value: Any) -> bool:
    text = clean_text(value)
    return any(marker in text for marker in (
        "Binary files ", "GIT binary patch", "\nbinary patch\n"))


def _file_label(item: ChangeFile) -> str:
    path = one_line(clean_text(item.path))
    if len(path) > 120:
        path = "…" + path[-119:]
    return f"{item.status or '--':>4}  {path}"


def _markdown_text(value: Any) -> str:
    return one_line(clean_text(value)).replace("`", "'") or "-"
