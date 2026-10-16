"""A fixture export is complete or refused, never repaired by another append."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from test_tui_soak_provider_log import LOG, SoakFailure


def _fixture(tmp_path):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "owned-export-record"})
    return path, reader


def _fault_output(monkeypatch, path, mode):
    original_open, original_fdopen = Path.open, os.fdopen
    opened = []

    class Fault:
        def __init__(self, base):
            self.base = base

        def __enter__(self):
            return self

        def __exit__(self, *args):
            try:
                self.flush()
            finally:
                self.base.close()
            if mode == "close":
                raise OSError("owned close refusal")

        def fileno(self):
            return self.base.fileno()

        def write(self, raw):
            if mode in ("short", "write"):
                count = self.base.write(raw[:5])
                if mode == "write":
                    raise OSError("owned partial write refusal")
                return count
            self.base.write(raw)
            return {"none": None, "bool": True}.get(mode, len(raw))

        def flush(self):
            self.base.flush()
            if mode == "flush":
                raise OSError("owned flush refusal")

    def wrap(base):
        opened.append(base)
        return Fault(base) if len(opened) == 1 else base

    def named(selected, *args, **kwargs):
        base = original_open(selected, *args, **kwargs)
        return wrap(base) if selected == path and args and args[0] == "ab" else base

    def descriptor(selected, *args, **kwargs):
        base = original_fdopen(selected, *args, **kwargs)
        return wrap(base) if args and args[0] == "ab" else base

    monkeypatch.setattr(Path, "open", named)
    monkeypatch.setattr(os, "fdopen", descriptor)
    return opened


@pytest.mark.parametrize("mode", ["short", "none", "bool"])
def test_incomplete_or_unsupported_write_result_never_advances_cursor(tmp_path, monkeypatch, mode):
    path, reader = _fixture(tmp_path)
    _fault_output(monkeypatch, path, mode)
    with pytest.raises(SoakFailure, match="export|write"):
        reader.read()
    assert reader.offset == 0 and not reader.exhausted
    assert path.exists()  # Preserve any partial bytes; never truncate/delete.
    with sqlite3.connect(LOG.database_path(path)) as store:
        assert store.execute("SELECT count(*) FROM events").fetchone() == (1,)


@pytest.mark.parametrize("mode", ["write", "flush", "close"])
def test_failed_export_latches_refusal_before_any_further_sqlite_read_or_append(tmp_path, monkeypatch, mode):
    path, reader = _fixture(tmp_path)
    opened = _fault_output(monkeypatch, path, mode)
    with pytest.raises(OSError, match="owned .*refusal"):
        reader.read()
    preserved = path.read_bytes()
    assert reader.offset == 0 and not reader.exhausted
    monkeypatch.setattr(LOG.sqlite3, "connect", lambda *_a, **_k: pytest.fail("failed export re-read SQLite"))
    with pytest.raises(SoakFailure, match="export"):
        reader.read()
    assert path.read_bytes() == preserved and len(opened) == 1
    assert opened[0].closed


def test_existing_export_refuses_even_with_empty_database_without_body_reads(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    path.write_bytes(b"owned incomplete prefix")
    original = Path.open

    def reject_body(selected, *args, **kwargs):
        if selected == path:
            pytest.fail("existing export body was read or overwritten")
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_body)
    with pytest.raises(SoakFailure, match="export"):
        LOG.ProviderLog(path, SoakFailure)
    assert not LOG.database_path(path).exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "broken-symlink", "hardlink", "directory"])
def test_export_appearing_after_reader_creation_is_not_adopted_or_overwritten(tmp_path, kind):
    path, reader = _fixture(tmp_path)
    target = tmp_path / "preserved-target"
    target.write_bytes(b"owned unrelated preserved bytes")
    if kind == "file":
        path.write_bytes(b"owned other export")
    elif kind == "symlink":
        path.symlink_to(target)
    elif kind == "broken-symlink":
        path.symlink_to(tmp_path / "missing-target")
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.mkdir()
    with pytest.raises((SoakFailure, OSError)):
        reader.read()
    assert reader.offset == 0 and target.read_bytes() == b"owned unrelated preserved bytes"
    assert path.is_symlink() or path.exists()


@pytest.mark.parametrize("kind", ["extend", "truncate", "rewrite", "replace", "hardlink", "symlink", "missing"])
def test_changed_export_is_not_appended_to_and_cursor_stays_at_previous_complete_batch(tmp_path, kind):
    path, reader = _fixture(tmp_path)
    assert reader.read() == [{"kind": "owned-export-record"}]
    before = path.read_bytes()
    LOG.append_provider_event(path, {"kind": "not-to-publish"})
    if kind == "extend":
        with path.open("ab") as output:
            output.write(b"damaged extra bytes")
    elif kind == "truncate":
        path.write_bytes(before[:5])
    elif kind == "rewrite":
        path.write_bytes(b"x" * len(before))
    elif kind == "hardlink":
        os.link(path, tmp_path / "preserved-alias")
    elif kind == "missing":
        path.rename(tmp_path / "preserved-export")
    else:
        path.rename(tmp_path / "preserved-export")
        if kind == "replace":
            path.write_bytes(before)
        else:
            path.symlink_to(tmp_path / "preserved-export")
    preserved = path.read_bytes() if path.exists() else None
    with pytest.raises((SoakFailure, OSError)):
        reader.read()
    assert reader.offset == 1
    assert (path.read_bytes() if path.exists() else None) == preserved


def test_decode_time_real_directory_replacement_cannot_redirect_selected_database_bytes(tmp_path, monkeypatch):
    parent = tmp_path / "selected"; parent.mkdir()
    path, reader = _fixture(parent)
    decode = reader._decode

    def replaced(raw):
        parent.rename(tmp_path / "preserved-original")
        parent.mkdir()
        return decode(raw)

    monkeypatch.setattr(reader, "_decode", replaced)
    with pytest.raises((SoakFailure, OSError)):
        reader.read()
    assert reader.offset == 0 and not path.exists()
    assert LOG.database_path(tmp_path / "preserved-original" / path.name).exists()
    assert not list(parent.iterdir())


def test_changed_export_refuses_empty_read_before_opening_sqlite(tmp_path, monkeypatch):
    path, reader = _fixture(tmp_path)
    reader.read()
    path.write_bytes(b"preserve this damaged export")
    monkeypatch.setattr(LOG.sqlite3, "connect", lambda *_a, **_k: pytest.fail("damaged export queried SQLite"))
    with pytest.raises(SoakFailure, match="export"):
        reader.read()
    assert reader.offset == 1 and not reader.exhausted
    assert path.read_bytes() == b"preserve this damaged export"


def test_replacement_at_close_is_refused_before_cursor_advances(tmp_path, monkeypatch):
    path, reader = _fixture(tmp_path)
    original = os.fdopen

    class ReplacedAtClose:
        def __init__(self, base):
            self.base = base

        def __enter__(self):
            return self.base

        def __exit__(self, *_args):
            self.base.close()
            path.rename(tmp_path / "preserved-complete-export")
            path.write_bytes(b"preserve unrelated replacement")

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: ReplacedAtClose(original(*a, **k)))
    with pytest.raises(SoakFailure, match="export"):
        reader.read()
    assert reader.offset == 0
    assert path.read_bytes() == b"preserve unrelated replacement"
    assert (tmp_path / "preserved-complete-export").read_bytes().endswith(b"\n")


def test_fdopen_failure_closes_owned_file_descriptor_and_preserves_unpublished_file(tmp_path, monkeypatch):
    path, reader = _fixture(tmp_path)
    opened = []

    def fail(descriptor, *_args, **_kwargs):
        opened.append(descriptor)
        raise OSError("owned output factory refusal")

    monkeypatch.setattr(os, "fdopen", fail)
    with pytest.raises(OSError, match="owned output factory refusal"):
        reader.read()
    assert reader.offset == 0 and path.read_bytes() == b""
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    with pytest.raises(SoakFailure, match="export"):
        reader.read()


def test_fifo_export_refuses_without_opening_or_writing_it(tmp_path, monkeypatch):
    path, reader = _fixture(tmp_path)
    os.mkfifo(path)
    monkeypatch.setattr(LOG.sqlite3, "connect", lambda *_a, **_k: pytest.fail("FIFO export queried SQLite"))
    with pytest.raises(SoakFailure, match="export"):
        reader.read()
    assert reader.offset == 0
    assert not path.is_file()


def test_second_reader_cannot_silently_duplicate_an_existing_complete_export(tmp_path):
    path, reader = _fixture(tmp_path)
    assert reader.read() == [{"kind": "owned-export-record"}]
    preserved = path.read_bytes()
    with pytest.raises(SoakFailure, match="export"):
        LOG.ProviderLog(path, SoakFailure)
    assert path.read_bytes() == preserved


def test_clean_multiple_batches_keep_complete_export_order_and_private_mode(tmp_path):
    path, reader = _fixture(tmp_path)
    first = reader.read()
    LOG.append_provider_event(path, {"kind": "next"})
    assert reader.read() == [{"kind": "next"}] and reader.offset == 2
    assert reader.read() == [] and reader.exhausted
    assert [json.loads(raw) for raw in path.read_bytes().splitlines()] == first + [{"kind": "next"}]
    assert path.stat().st_mode & 0o777 == 0o600
