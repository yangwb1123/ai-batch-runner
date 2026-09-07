"""Bounded transcript contracts for long-running interactive sessions."""

from pbatch.tui_transcript import (
    MAX_TRANSCRIPT_BYTES,
    MAX_TRANSCRIPT_ENTRIES,
    MAX_TRANSCRIPT_ENTRY_BYTES,
    TranscriptEntry,
    transcript_bytes,
    trim_transcript,
)


def test_trim_bounds_count_and_bytes_while_preserving_active_entries():
    entries = [
        TranscriptEntry("system", f"row-{index}:" + "x" * 2048)
        for index in range(MAX_TRANSCRIPT_ENTRIES + 5)
    ]
    assistant_index = len(entries)
    entries.append(TranscriptEntry("assistant", "active answer"))
    error_index = len(entries)
    entries.append(TranscriptEntry("error", "active warning"))

    result = trim_transcript(entries, (assistant_index, error_index))

    assert result.omitted
    assert len(entries) <= MAX_TRANSCRIPT_ENTRIES
    assert transcript_bytes(entries) <= MAX_TRANSCRIPT_BYTES
    assistant_index, error_index = result.protected_indexes
    assert assistant_index is not None and entries[assistant_index].text == "active answer"
    assert error_index is not None and entries[error_index].text == "active warning"


def test_trim_caps_utf8_entries_and_keeps_the_useful_edge():
    entries = [
        TranscriptEntry("system", "BEGIN-" + "前" * MAX_TRANSCRIPT_ENTRY_BYTES),
        TranscriptEntry("assistant", "后" * MAX_TRANSCRIPT_ENTRY_BYTES + "-FINAL"),
    ]

    result = trim_transcript(entries)

    assert result.omitted
    assert entries[0].text.startswith("BEGIN-")
    assert entries[1].text.endswith("-FINAL")
    assert all(len(entry.text.encode("utf-8")) <= MAX_TRANSCRIPT_ENTRY_BYTES
               for entry in entries)
