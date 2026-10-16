"""Already-copied fixture JSON must not keep the cooperative SQLite lock held."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from test_tui_soak_provider_log import LOG, TOOLS, SoakFailure


def test_database_is_closed_and_directory_guard_released_before_decode(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "copied"})
    closed = []
    connect, decode = sqlite3.connect, reader._decode

    class Tracked(sqlite3.Connection):
        def close(self):
            super().close()
            closed.append(True)

    monkeypatch.setattr(LOG.sqlite3, "connect", lambda *a, **k: connect(*a, factory=Tracked, **k))

    def inspected(raw):
        assert closed == [True], "JSON decoded before SQLite closed"
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        return decode(raw)

    monkeypatch.setattr(reader, "_decode", inspected)
    assert reader.read() == [{"kind": "copied"}]
    assert closed == [True] and reader.offset == 1


def test_writer_can_commit_while_copied_batch_is_decoding_without_changing_that_batch(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    first, second = {"kind": "selected-before-decode"}, {"kind": "committed-during-decode"}
    LOG.append_provider_event(path, first)
    decode, observed = reader._decode, []

    def append_during_decode(raw):
        # No sleep or host-load lottery. The old scope holds the same directory
        # throughout decode and deterministically exhausts its original 3s wait.
        LOG.append_provider_event(path, second)
        observed.append(reader.offset)
        return decode(raw)

    monkeypatch.setattr(reader, "_decode", append_during_decode)
    assert reader.read() == [first]
    assert observed == [0] and reader.offset == 1
    monkeypatch.setattr(reader, "_decode", decode)
    assert reader.read() == [second] and reader.offset == 2
    assert reader.read() == [] and reader.exhausted
    assert [json.loads(line) for line in path.read_bytes().splitlines()] == [first, second]
    assert LOG.LOCK_TIMEOUT == 3.0


def test_other_fixture_process_can_commit_before_selected_batch_decode_finishes(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "before"})
    decode = reader._decode
    code = ("import pathlib,sys\n"
            f"sys.path.insert(0,{str(TOOLS)!r})\n"
            "from tui_soak_provider_log import append_provider_event\n"
            "append_provider_event(pathlib.Path(sys.argv[1]), {'kind':'during'})\n")

    def append_from_child(raw):
        process = subprocess.Popen([sys.executable, "-c", code, str(path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert process.wait(timeout=5) == 0, "owned fixture writer blocked by JSON decoding"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        assert reader.offset == 0
        return decode(raw)

    monkeypatch.setattr(reader, "_decode", append_from_child)
    assert reader.read() == [{"kind": "before"}] and reader.offset == 1
    monkeypatch.setattr(reader, "_decode", decode)
    assert reader.read() == [{"kind": "during"}] and reader.offset == 2
    assert LOG.LOCK_TIMEOUT == 3.0


@pytest.mark.parametrize("raw", [b"broken-json\n", b"{}", b"[]\n", b"\xff\n"])
def test_all_selected_rows_must_decode_before_any_export_or_cursor_advance(tmp_path, raw):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "valid-prefix-not-to-export"})
    with sqlite3.connect(LOG.database_path(path)) as store:
        store.execute("INSERT INTO events (payload) VALUES (?)", (raw,))
    with pytest.raises(SoakFailure):
        reader.read()
    assert reader.offset == 0 and not reader.exhausted and not path.exists()
    with sqlite3.connect(LOG.database_path(path)) as store:
        assert store.execute("SELECT count(*) FROM events").fetchone() == (2,)


def test_decode_failure_releases_guard_and_does_not_export_a_partial_batch(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "preserved"})
    decode = reader._decode

    def fail(_raw):
        raise SoakFailure("owned decode refusal")

    monkeypatch.setattr(reader, "_decode", fail)
    with pytest.raises(SoakFailure, match="owned decode refusal"):
        reader.read()
    assert reader.offset == 0 and not path.exists()
    LOG.append_provider_event(path, {"kind": "after-refusal"})
    monkeypatch.setattr(reader, "_decode", decode)
    assert reader.read() == [{"kind": "preserved"}, {"kind": "after-refusal"}]


@pytest.mark.parametrize("bad", ["text instead of bytes", b"x" * (LOG.MAX_BYTES + 1)])
def test_raw_sqlite_admission_still_refuses_unsupported_type_or_bytes_before_decode(tmp_path, monkeypatch, bad):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    with sqlite3.connect(LOG.database_path(path)) as store:
        store.execute("INSERT INTO events (payload) VALUES (?)", (bad,))
    monkeypatch.setattr(reader, "_decode", lambda *_: pytest.fail("raw refusal must precede decoding"))
    with pytest.raises(SoakFailure, match="oversized or non-byte"):
        reader.read()
    assert reader.offset == 0 and not path.exists()
