"""Bounded private ledger reads and coverage, without real runtime discovery."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from pbatch import instance_observation_io as io
from pbatch import instance_view as view


class MeasuredReader:
    def __init__(self, handle, sizes, after_read=None):
        self.handle, self.sizes, self.after_read = handle, sizes, after_read

    def fileno(self):
        return self.handle.fileno()

    def seek(self, position):
        return self.handle.seek(position)

    def read(self, size):
        assert 0 < size <= io.MAX_BYTES, "no unbounded or over-budget read"
        result = self.handle.read(size)
        self.sizes.append(len(result))
        if self.after_read:
            callback, self.after_read = self.after_read, None
            callback()
        return result


def _measure(monkeypatch, sizes, after_read=None):
    original = io._open_regular

    @contextmanager
    def opened(path):
        with original(path) as handle:
            yield MeasuredReader(handle, sizes, after_read)

    monkeypatch.setattr(io, "_open_regular", opened)


def test_large_ledger_is_tail_read_not_read_text_or_whole_file(tmp_path, monkeypatch):
    path = tmp_path / "owned-ledger.jsonl"
    with path.open("wb") as handle:
        for _ in range(10):
            handle.write(b'{"schema_version":1,"n":0}\n' * 65536)
        handle.write(b'{"schema_version":1,"n":17}\n')
    sizes = []
    _measure(monkeypatch, sizes)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("no whole text read"))
    monkeypatch.setattr(Path, "read_bytes", lambda *a, **kw: pytest.fail("no whole byte read"))
    records = view._read_jsonl(path, 2, 1)
    assert len(records) == 2 and records[-1]["n"] == 17
    assert sum(sizes) <= io.MAX_BYTES + 1
    assert len(sizes) == 2


@pytest.mark.parametrize("aligned", [True, False])
def test_byte_window_aligns_complete_lines_without_decoding_fragment(tmp_path, monkeypatch, aligned):
    path = tmp_path / "ledger"
    tail = b'{"n":1}\n{"n":2}\n'
    path.write_bytes(b"IGNORED PRIVATE FRAGMENT\n" + tail)
    monkeypatch.setattr(io, "MAX_BYTES", len(tail) + (0 if aligned else 4))
    values, info = io.read_window(path, 10)
    assert values == [{"n": 1}, {"n": 2}]
    assert info["truncated"] is True and info["malformed"] == 0
    assert info["bytes_read"] <= io.MAX_BYTES + 1


def test_no_completed_record_in_byte_window_stays_partial_unknown(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"private":"' + b"X" * 1000 + b'"}\n')
    monkeypatch.setattr(io, "MAX_BYTES", 64)
    values, info = io.read_window(path, 10)
    assert values == [] and info["truncated"] is True
    assert info["records"] == 0 and info["available"] is True


def test_row_limit_is_before_decoding_and_gaps_are_retained(tmp_path):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"n":1}\n{"n":2}\nBAD\n{"n":4}\n')
    values, info = io.read_window(path, 2)
    assert values == [None, {"n": 4}]
    assert (info["lines"], info["records"], info["malformed"]) == (2, 1, 1)
    assert info["truncated"] is True


def test_request_above_hard_record_cap_cannot_expand_read(tmp_path):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"n":1}\n' * (io.MAX_RECORDS + 10))
    values, info = io.read_window(path, io.MAX_RECORDS + 1000)
    assert len(values) == io.MAX_RECORDS
    assert info["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1])
def test_zero_negative_limits_do_not_open_read_or_enumerate(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(io, "_open_regular", lambda *a: pytest.fail("no file open"))
    monkeypatch.setattr(os, "scandir", lambda *a: pytest.fail("no enumeration"))
    assert io.read_window(tmp_path / "ledger", limit)[0] == []
    assert io.project_window(tmp_path / "projects", limit)[0] == []
    assert view._read_jsonl(tmp_path / "ledger", limit) == []


@pytest.mark.parametrize("payload", [
    b'{"status":"RUNNING","status":"MERGED"}\n', b'{"x":NaN}\n',
    b'{"x":Infinity}\n', b'{"x":1e400}\n', b'[]\n', b'null\n', b'\xff\n',
    ('{"n":1}\n'.encode("utf-16")), b'{"x":' + b'[' * 2000 + b']' * 2000 + b'}\n',
])
def test_bad_utf8_ambiguous_nonfinite_or_recursive_records_are_gaps(tmp_path, payload):
    path = tmp_path / "ledger"
    path.write_bytes(payload)
    values, info = io.read_window(path, 10)
    assert not any(isinstance(value, dict) for value in values)
    assert info["malformed"] >= 1


def test_overlong_line_is_not_given_to_json_decoder(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"private":"' + b"X" * io.MAX_LINE_BYTES + b'"}\n')
    monkeypatch.setattr(json, "loads", lambda *a, **kw: pytest.fail("overlong decoder call"))
    values, info = io.read_window(path, 10)
    assert values == [None] and info["malformed"] == 1


def test_valid_json_without_newline_is_unfinished_not_a_fact(tmp_path):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"n":1}\n{"n":2}')
    values, info = io.read_window(path, 10)
    assert values == [{"n": 1}]
    assert info["incomplete"] is True


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "directory", "fifo", "hardlink"])
def test_unsafe_ledger_paths_refuse_without_repair(tmp_path, kind):
    target, path = tmp_path / "target", tmp_path / "ledger"
    target.write_bytes(b'{"private":"not projected"}\n')
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "parent_symlink":
        directory = tmp_path / "real"
        directory.mkdir()
        (directory / "ledger").write_bytes(b'{"n":1}\n')
        (tmp_path / "alias").symlink_to(directory, target_is_directory=True)
        path = tmp_path / "alias" / "ledger"
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("POSIX fixture")
        os.mkfifo(path)
    else:
        os.link(target, path)
    before = target.read_bytes()
    values, info = io.read_window(path, 10)
    assert values == [] and info["status"] == "unavailable"
    assert info["bytes_read"] == 0
    assert target.read_bytes() == before and path.exists()


def test_foreign_or_unknown_owner_cannot_be_assumed(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"n":1}\n')
    owner = path.stat().st_uid
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "geteuid", lambda: owner + 1)
        values, info = io.read_window(path, 10)
    assert values == [] and info["status"] == "unavailable"
    assert info["bytes_read"] == 0


@pytest.mark.parametrize("change", ["replace", "append", "remove"])
def test_changed_file_withholds_all_observations_and_closes_reader(tmp_path, monkeypatch, change):
    path = tmp_path / "ledger"
    path.write_bytes(b'{"n":1}\n')
    before = path.read_bytes()

    def changed():
        if change == "replace":
            path.rename(tmp_path / "preserved-old")
            path.write_bytes(b'{"n":2}\n')
        elif change == "append":
            with path.open("ab") as handle:
                handle.write(b'{"n":2}\n')
        else:
            path.unlink()

    sizes = []
    _measure(monkeypatch, sizes, changed)
    values, info = io.read_window(path, 10)
    assert values == [] and info["status"] == "unavailable"
    assert info["error"] and sizes == [len(before)]
    if change == "replace":
        assert (tmp_path / "preserved-old").read_bytes() == before
        assert path.read_bytes() == b'{"n":2}\n'


def test_directory_enumeration_and_display_have_independent_caps(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    for index in range(20):
        (root / f"owned-{index:02d}").mkdir()
    monkeypatch.setattr(io, "MAX_DIRECTORY_ENTRIES", 8)
    paths, info = io.project_window(root, 2)
    assert len(paths) == 2
    assert info["entries_observed"] == 8
    assert info["directories_observed"] == 8
    assert info["truncated"] is True
    assert paths == sorted(paths)


def test_symbolic_project_does_not_expand_observation_scope(tmp_path, monkeypatch):
    root, foreign = tmp_path / "projects", tmp_path / "not-admitted"
    root.mkdir()
    foreign.mkdir()
    (root / "escape").symlink_to(foreign, target_is_directory=True)
    paths, info = io.project_window(root, 10)
    assert paths == [] and info["malformed"] == 1
    (tmp_path / "root-alias").symlink_to(root, target_is_directory=True)
    paths, info = io.project_window(tmp_path / "root-alias", 10)
    assert paths == [] and info["error"]


@pytest.mark.parametrize("name", ["control\x1b[31m", "bidi\u202e", "c1\x85"])
def test_control_workspace_names_are_not_rendered_or_read(tmp_path, name):
    root = tmp_path / "projects"
    root.mkdir()
    (root / name).mkdir()
    paths, info = io.project_window(root, 10)
    assert paths == [] and info["malformed"] == 1


@pytest.mark.parametrize("option,value", [
    ("stale_seconds", 0), ("stale_seconds", -1), ("stale_seconds", True),
    ("limit", -1), ("limit", 201), ("limit", True), ("limit", 1.5),
])
def test_invalid_view_options_refuse_before_enumeration(monkeypatch, option, value):
    monkeypatch.setattr(view, "project_window", lambda *a: pytest.fail("no enumeration"))
    with pytest.raises(ValueError):
        view.instance_rows(**{option: value})
