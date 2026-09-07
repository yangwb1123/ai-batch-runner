"""Read-only review-finding picker with safe Changes navigation."""

from __future__ import annotations

from typing import Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .tui_findings import (
    FindingsSnapshot, ReviewFinding, finding_detail, finding_label,
)


class TUIReviewFindingsScreen(ModalScreen[None]):
    """Select one structured finding, then hand it to a read-only Changes view."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("f", "fix", "Fix", priority=True),
        Binding("q", "close", "Close", show=False),
    ]
    CSS = """
    TUIReviewFindingsScreen { align: center middle; background: $background; }
    #review-findings-dialog {
        width: 90%; height: 82%; min-width: 44;
        background: $surface; border: round $accent;
    }
    #review-findings-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #review-findings-options { height: 1fr; padding: 0 1; }
    #review-findings-detail {
        height: 10; padding: 1 2; color: $text-muted;
        border-top: solid $panel-lighten-1;
    }
    #review-findings-hint { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, snapshot: FindingsSnapshot,
                 on_jump: Callable[[ReviewFinding], None],
                 on_fix: Optional[Callable[[ReviewFinding], None]] = None) -> None:
        super().__init__(id="review-findings-inspector")
        self.inspector_kind = "review-findings"
        self.snapshot = snapshot
        self._on_jump = on_jump
        self._on_fix = on_fix
        self._visible = snapshot.findings

    def compose(self) -> ComposeResult:
        with Vertical(id="review-findings-dialog"):
            yield Static(self._heading(), id="review-findings-heading", markup=False)
            yield OptionList(*self._options(), id="review-findings-options", markup=False)
            yield Static(self._detail(0), id="review-findings-detail", markup=False)
            yield Static(self._hint(), id="review-findings-hint", markup=False)

    def on_mount(self) -> None:
        options = self.query_one("#review-findings-options", OptionList)
        options.focus()
        self._update_detail(options.highlighted)

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        self._update_detail(event.option_index)

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if index < 0 or index >= len(self._visible):
            return
        finding = self._visible[index]
        self.dismiss(None)
        try:
            self.app.call_after_refresh(self._on_jump, finding)
        except RuntimeError:
            pass

    def refresh_snapshot(self, snapshot: FindingsSnapshot) -> None:
        if not isinstance(snapshot, FindingsSnapshot):
            return
        self.snapshot = snapshot
        self._visible = snapshot.findings
        if not self.is_mounted:
            return
        options = self.query_one("#review-findings-options", OptionList)
        options.set_options(self._options())
        options.highlighted = 0 if self._visible else None
        self.query_one("#review-findings-heading", Static).update(self._heading())
        self._update_detail(options.highlighted)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_fix(self) -> None:
        if not callable(self._on_fix):
            return
        options = self.query_one("#review-findings-options", OptionList)
        index = options.highlighted
        if index is None or index < 0 or index >= len(self._visible):
            return
        finding = self._visible[index]
        self.dismiss(None)
        try:
            self.app.call_after_refresh(self._on_fix, finding)
        except RuntimeError:
            pass

    def _hint(self) -> str:
        suffix = " · f fix this finding" if callable(self._on_fix) else ""
        return f"↑/↓ inspect · Enter jump to Changes{suffix} · Esc close"

    def _heading(self) -> str:
        warning = (f" · {self.snapshot.skipped} omitted"
                   if self.snapshot.skipped else "")
        return f"Review findings ({len(self._visible)}){warning}"

    def _options(self) -> list[Option]:
        return [Option(finding_label(finding), id=f"finding-{index}")
                for index, finding in enumerate(self._visible)]

    def _update_detail(self, index: Optional[int]) -> None:
        self.query_one("#review-findings-detail", Static).update(
            self._detail(index))

    def _detail(self, index: Optional[int]) -> str:
        if index is None or index < 0 or index >= len(self._visible):
            return "No structured findings are available."
        return finding_detail(self._visible[index])


__all__ = ["TUIReviewFindingsScreen"]
