"""Observe freshly rendered composer readiness in the fixed-size soak PTY."""

from __future__ import annotations

import codecs


class ComposerObservation:
    """Track the composer row, accepting a marker only when its end is painted.

    Journal terminal events precede the UI's finish callback. A busy-to-ready
    composer transition is the visible acknowledgement that another command can
    be submitted. Old screen contents and old markers cannot satisfy a new turn.
    """

    def __init__(self, rows: int = 36, columns: int = 140):
        self.rows, self.columns = rows, columns
        self.row, self.column = 1, 1
        self.cells = [" "] * columns
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state, self.control = "text", ""
        self.revision = self.busy_revision = self.ready_revision = 0
        self.phase = "unknown"

    def feed(self, raw: bytes) -> None:
        for char in self.decoder.decode(raw):
            if self.state == "escape":
                self._escape(char)
            elif self.state == "csi":
                self._csi(char)
            elif self.state == "osc":
                self._osc(char)
            else:
                self._text(char)

    def _escape(self, char: str) -> None:
        self.state = "csi" if char == "[" else "osc" if char == "]" else "text"
        self.control = ""

    def _csi(self, char: str) -> None:
        if "@" <= char <= "~":
            self._control(char)
            self.state = "text"
        elif len(self.control) < 128:
            self.control += char
        else:
            self.state = "text"

    def _osc(self, char: str) -> None:
        if char == "\x07" or char == "\\" and self.control.endswith("\x1b"):
            self.state = "text"
        self.control = char

    def _text(self, char: str) -> None:
        if char == "\x1b":
            self.state = "escape"
        elif char == "\r":
            self.column = 1
        elif char == "\n":
            self.row = min(self.rows, self.row + 1)
        elif char == "\b":
            self.column = max(1, self.column - 1)
        elif char >= " ":
            self._paint(char)

    def _paint(self, char: str) -> None:
        if self.row == self.rows - 1 and 1 <= self.column <= self.columns:
            self.cells[self.column - 1] = char
            self._observe_marker()
        self.column += 1

    def _observe_marker(self) -> None:
        line = "".join(self.cells)
        for marker, phase in (("Queue follow-up", "busy"), ("Ask pi-batch…", "ready")):
            start = line.find(marker)
            if start >= 0 and self.column == start + len(marker):
                self.revision += 1
                self.phase = phase
                setattr(self, phase + "_revision", self.revision)

    def _control(self, final: str) -> None:
        if any(char not in "0123456789;" for char in self.control):
            return
        values = [int(part or "0") for part in self.control.split(";")]
        self._apply_control(final, values)
        self._clamp_cursor()

    def _apply_control(self, final: str, values: list[int]) -> None:
        if final in {"H", "f"}:
            self._set_position(values)
        elif final in {"A", "B", "d"}:
            self._move_vertical(final, values[0] or 1)
        elif final in {"C", "D", "G"}:
            self._move_horizontal(final, values[0] or 1)
        elif final == "K" and self.row == self.rows - 1:
            self._erase_line(values[0])
        elif final == "J" and values[0] in {2, 3}:
            self.cells[:] = [" "] * self.columns

    def _set_position(self, values: list[int]) -> None:
        self.row = values[0] or 1
        self.column = (values[1] or 1) if len(values) > 1 else 1

    def _move_vertical(self, final: str, amount: int) -> None:
        self.row = {"A": self.row - amount, "B": self.row + amount,
                    "d": amount}[final]

    def _move_horizontal(self, final: str, amount: int) -> None:
        self.column = {"C": self.column + amount, "D": self.column - amount,
                       "G": amount}[final]

    def _clamp_cursor(self) -> None:
        self.row = max(1, min(self.rows, self.row))
        self.column = max(1, min(self.columns + 1, self.column))

    def _erase_line(self, mode: int) -> None:
        start, end = (0, self.columns) if mode == 2 else (
            (0, self.column) if mode == 1 else (self.column - 1, self.columns))
        start, end = max(0, start), min(self.columns, end)
        self.cells[start:end] = [" "] * (end - start)

    def ready_after(self, revision: int) -> bool:
        return (self.phase == "ready"
                and self.ready_revision > self.busy_revision > revision)

    def summary(self) -> dict:
        return {"phase": self.phase, "revision": self.revision,
                "busy_revision": self.busy_revision, "ready_revision": self.ready_revision,
                "composer": "".join(self.cells).strip()}
