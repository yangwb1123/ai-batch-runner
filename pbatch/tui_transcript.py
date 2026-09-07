"""Bounded in-memory transcript storage helpers for the product TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


MAX_TRANSCRIPT_ENTRIES = 400
MAX_TRANSCRIPT_BYTES = 512 * 1024
MAX_TRANSCRIPT_ENTRY_BYTES = 128 * 1024
OMISSION_MARKDOWN = (
    "## System\n\n_Earlier conversation content was omitted to keep this "
    "interactive session bounded._")
_HEAD_MARKER = "\n\n… content truncated …"
_TAIL_MARKER = "… earlier content truncated …\n\n"


@dataclass
class TranscriptEntry:
    role: str
    text: str


@dataclass(frozen=True)
class TranscriptTrim:
    """Result of a trim, including remapped protected entry indexes."""

    protected_indexes: tuple[Optional[int], ...]
    omitted: bool


def transcript_bytes(entries: Sequence[TranscriptEntry]) -> int:
    """Return the UTF-8 payload size retained by transcript entries."""

    return sum(len(entry.text.encode("utf-8")) for entry in entries)


def trim_transcript(
    entries: list[TranscriptEntry],
    protected_indexes: Sequence[Optional[int]] = (),
) -> TranscriptTrim:
    """Enforce transcript limits while preserving active stream entries."""

    indexes = [_valid_index(value, len(entries)) for value in protected_indexes]
    omitted = _truncate_entries(entries)
    total = transcript_bytes(entries)
    while len(entries) > MAX_TRANSCRIPT_ENTRIES or total > MAX_TRANSCRIPT_BYTES:
        victim = _oldest_unprotected(len(entries), indexes)
        if victim is None:
            raise RuntimeError("transcript limits cannot preserve active entries")
        total -= len(entries[victim].text.encode("utf-8"))
        entries.pop(victim)
        indexes = [_remap_index(value, victim) for value in indexes]
        omitted = True
    return TranscriptTrim(tuple(indexes), omitted)


def _truncate_entries(entries: Sequence[TranscriptEntry]) -> bool:
    changed = False
    for entry in entries:
        text, truncated = _bounded_text(entry.text, keep_tail=entry.role in {
            "assistant", "error",
        })
        if truncated:
            entry.text = text
            changed = True
    return changed


def _bounded_text(text: str, *, keep_tail: bool) -> tuple[str, bool]:
    raw = str(text).encode("utf-8")
    if len(raw) <= MAX_TRANSCRIPT_ENTRY_BYTES:
        return str(text), False
    marker = _TAIL_MARKER if keep_tail else _HEAD_MARKER
    marker_bytes = marker.encode("utf-8")
    budget = MAX_TRANSCRIPT_ENTRY_BYTES - len(marker_bytes)
    fragment = raw[-budget:] if keep_tail else raw[:budget]
    decoded = fragment.decode("utf-8", errors="ignore")
    bounded = marker + decoded if keep_tail else decoded + marker
    return bounded, True


def _valid_index(value: Optional[int], length: int) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < length:
        return value
    return None


def _oldest_unprotected(length: int,
                        indexes: Sequence[Optional[int]]) -> Optional[int]:
    protected = {value for value in indexes if value is not None}
    return next((index for index in range(length) if index not in protected), None)


def _remap_index(value: Optional[int], victim: int) -> Optional[int]:
    if value is None:
        return None
    if value == victim:
        return None
    return value - 1 if value > victim else value
