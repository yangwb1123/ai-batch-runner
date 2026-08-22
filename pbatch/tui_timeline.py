"""Bounded, dependency-free timeline projection for the interactive TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import time
from typing import Optional


_ENTRY_KINDS = frozenset({"command", "file", "usage", "message", "status"})
_SUMMARY_BYTES = 512
_DETAIL_BYTES = 2 * 1024
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _bounded(value: object, maximum: int) -> str:
    text = _ANSI_ESCAPE.sub("", str(value or ""))
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    marker = "…"
    prefix = encoded[:maximum - len(marker.encode())].decode("utf-8", "ignore")
    return prefix + marker


@dataclass
class TimelineEntry:
    """One trusted runtime item rendered by :class:`TimelineLog`."""

    kind: str
    ts: float | str
    summary: str
    detail: str = ""
    exit_code: Optional[int] = None
    collapsed: bool = False

    def __post_init__(self) -> None:
        if self.kind not in _ENTRY_KINDS:
            raise ValueError(f"unsupported timeline entry kind: {self.kind}")
        if (self.exit_code is not None and
                (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int))):
            raise TypeError("exit_code must be an integer or None")
        self.summary = _bounded(self.summary, _SUMMARY_BYTES)
        self.detail = _bounded(self.detail, _DETAIL_BYTES)
        self.collapsed = bool(self.collapsed)


class TimelineLog:
    """Current-turn timeline with deterministic folding and bounded retention."""

    def __init__(self, max_entries: int = 500):
        if (isinstance(max_entries, bool) or not isinstance(max_entries, int)
                or max_entries <= 0):
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._entries: list[TimelineEntry] = []
        self._commands: dict[str, TimelineEntry] = {}
        self._command_sequence = 0
        self._file_entry: Optional[TimelineEntry] = None
        self._file_paths: list[str] = []
        self._usage_entry: Optional[TimelineEntry] = None

    @property
    def entries(self) -> tuple[TimelineEntry, ...]:
        return tuple(self._entries)

    def append(self, entry: TimelineEntry) -> bool:
        if not isinstance(entry, TimelineEntry):
            raise TypeError("entry must be a TimelineEntry")
        if len(self._entries) >= self.max_entries:
            removable = next((index for index, item in enumerate(self._entries)
                              if not self._running(item)), None)
            if removable is None:
                return False
            self._forget(self._entries.pop(removable))
        self._entries.append(entry)
        self.collapse_rules()
        return True

    def _forget(self, entry: TimelineEntry) -> None:
        self._commands = {key: value for key, value in self._commands.items()
                          if value is not entry}
        if self._file_entry is entry:
            self._file_entry, self._file_paths = None, []
        if self._usage_entry is entry:
            self._usage_entry = None

    def consume(self, kind: str, text: str, ts: float | str | None = None) -> bool:
        """Project one backend notification without deriving types from body text."""
        normalized = str(kind or "").lower()
        when = time.time() if ts is None else ts
        if normalized == "command":
            self._consume_command(_notice(text), when)
        elif normalized == "command-output":
            self._consume_command_output(_notice(text), when)
        elif normalized == "file":
            self._consume_file(_notice(text), when)
        elif normalized == "usage":
            self._consume_usage(_notice(text), when)
        elif normalized == "stdout":
            clean = str(text or "")
            self.append(TimelineEntry("message", when, _first_line(clean), clean))
        elif normalized in {"stderr", "status"}:
            clean = str(text or "")
            self.append(TimelineEntry("status", when, _first_line(clean), clean))
        else:
            return False
        self.collapse_rules()
        return True

    def _consume_command(self, notice: dict, when: float | str) -> None:
        phase = str(notice.get("phase") or "")
        item_id = self._command_key(notice.get("item_id"), phase == "started")
        command = str(notice.get("command") or "command")
        entry = self._commands.get(item_id)
        if entry is None:
            entry = TimelineEntry("command", when, command)
            if self.append(entry):
                self._commands[item_id] = entry
        elif command != "command":
            entry.summary = _bounded(command, _SUMMARY_BYTES)
        if phase == "completed":
            code = notice.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                entry.exit_code = code
            else:
                status = str(notice.get("status") or "").lower()
                entry.exit_code = 0 if status in {"completed", "succeeded", "success"} else -1

    def _consume_command_output(self, notice: dict, when: float | str) -> None:
        item_id = self._command_key(notice.get("item_id"), False)
        entry = self._commands.get(item_id)
        if entry is None:
            self._consume_command({"item_id": item_id}, when)
            entry = self._commands.get(item_id)
        if entry is not None:
            separator = "\n" if entry.detail else ""
            entry.detail = _bounded(entry.detail + separator +
                                    str(notice.get("output") or ""), _DETAIL_BYTES)

    def _command_key(self, value: object, force_new: bool) -> str:
        item_id = str(value or "")
        if item_id:
            return item_id
        if not force_new:
            running = next((key for key, entry in reversed(self._commands.items())
                            if entry.exit_code is None), "")
            if running:
                return running
        self._command_sequence += 1
        return f"command-{self._command_sequence}"

    def _consume_file(self, notice: dict, when: float | str) -> None:
        paths = notice.get("paths", [])
        paths = [str(path) for path in paths] if isinstance(paths, list) else []
        self._file_paths = list(dict.fromkeys(self._file_paths + paths))
        summary = ", ".join(self._file_paths[:4]) or "file change"
        if len(self._file_paths) > 4 or notice.get("truncated"):
            summary += ", …"
        if self._file_entry is None:
            entry = TimelineEntry("file", when, summary, "\n".join(self._file_paths))
            if self.append(entry):
                self._file_entry = entry
        else:
            self._file_entry.summary = _bounded(summary, _SUMMARY_BYTES)
            self._file_entry.detail = _bounded("\n".join(self._file_paths), _DETAIL_BYTES)

    def _consume_usage(self, notice: dict, when: float | str) -> None:
        summary = _usage_summary(notice)
        detail = json.dumps(notice.get("usage", {}), ensure_ascii=False,
                            sort_keys=True, default=str)
        if self._usage_entry is None:
            entry = TimelineEntry("usage", when, summary, detail)
            if self.append(entry):
                self._usage_entry = entry
        else:
            self._usage_entry.summary = _bounded(summary, _SUMMARY_BYTES)
            self._usage_entry.detail = _bounded(detail, _DETAIL_BYTES)

    @staticmethod
    def _running(entry: TimelineEntry) -> bool:
        return entry.kind == "command" and entry.exit_code is None

    def collapse_rules(self) -> None:
        """Apply the Phase-1 default density without inspecting body text."""
        for entry in self._entries:
            if entry.kind == "command":
                entry.collapsed = entry.exit_code == 0
            elif entry.kind in {"file", "usage", "message", "status"}:
                entry.collapsed = True

    def render_lines(self, width: int) -> list[str]:
        """Return ANSI-free lines, each hard-bounded to ``width`` columns."""
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            return []
        lines: list[str] = []
        for entry in self._entries:
            lines.append(_truncate(self._headline(entry), width))
            if not entry.collapsed and entry.detail:
                details = entry.detail.splitlines() or [entry.detail]
                lines.extend(_truncate(f"  {line}", width) for line in details)
        return lines

    @staticmethod
    def _headline(entry: TimelineEntry) -> str:
        label = entry.kind.upper()
        outcome = ""
        if entry.kind == "command":
            if entry.exit_code is None:
                outcome = " …"
            elif entry.exit_code == 0:
                outcome = " ✓"
            else:
                outcome = f" ✗ exit {entry.exit_code}"
        return f"[{_timestamp(entry.ts)}] {label}{outcome} {entry.summary}".rstrip()

    def to_summary(self) -> str:
        """Project command/file/usage/failure facts for the current turn."""
        commands = [entry for entry in self._entries if entry.kind == "command"]
        files = {
            path.strip() for entry in self._entries if entry.kind == "file"
            for path in entry.detail.splitlines() if path.strip()
        }
        usage = next((entry.summary for entry in reversed(self._entries)
                      if entry.kind == "usage"), "usage unavailable")
        failures = [entry.summary for entry in commands
                    if entry.exit_code is not None and entry.exit_code != 0]
        summary = f"{len(commands)} command(s) · {len(files)} file(s) · {usage}"
        if failures:
            summary += " · failures: " + ", ".join(failures[:3])
            if len(failures) > 3:
                summary += ", …"
        return _bounded(summary, _SUMMARY_BYTES)

    def render_summary(self, width: int) -> str:
        return _truncate(self.to_summary(), max(1, int(width)))


def _timestamp(value: float | str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    return _bounded(value, 32) or "--:--:--"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[:width - 1] + "…"


def _notice(value: str) -> dict:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_line(value: str) -> str:
    clean = " ".join(str(value or "").split())
    return _bounded(clean, _SUMMARY_BYTES) or "message"


def _usage_summary(notice: dict) -> str:
    usage = notice.get("usage", {})
    usage = usage if isinstance(usage, dict) else {}
    total = notice.get("total_tokens", 0)
    parts = []
    for name in ("input_tokens", "cached_input_tokens", "output_tokens",
                 "reasoning_output_tokens"):
        if name in usage:
            parts.append(f"{name.removesuffix('_tokens')} {usage[name]}")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        parts.append(f"total {total} tokens")
    return " · ".join(parts) or "usage unavailable"
