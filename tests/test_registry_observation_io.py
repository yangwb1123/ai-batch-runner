"""Bounded registry I/O shared by ps and task-center activity observation."""
from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager

import pytest

from pbatch import registry, registry_observation as reader
from pbatch import task_center_activity as activity


def _record(path, pid=123, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "host": socket.gethostname(),
                               "start": 100, "last_active": 200,
                               "status": "running", **extra}))


class CountingReader:
    def __init__(self, handle, sizes, change=None):
        self.handle, self.sizes, self.change = handle, sizes, change

    def fileno(self):
        return self.handle.fileno()

    def read(self, maximum):
        assert maximum == reader.MAX_BYTES + 1
        raw = self.handle.read(maximum)
        self.sizes.append(len(raw))
        if self.change:
            self.change()
        return raw


def _wrap(monkeypatch, sizes, change=None):
    original = reader._open_regular
    opened_handles = []

    @contextmanager
    def opened(path):
        with original(path) as handle:
            opened_handles.append(handle)
            yield CountingReader(handle, sizes, change)

    monkeypatch.setattr(reader, "_open_regular", opened)
    return opened_handles


def test_byte_limit_is_real_and_reader_closed_after_overflow(tmp_path, monkeypatch):
    path = tmp_path / "123.json"
    path.write_bytes(b"X" * (reader.MAX_BYTES * 128))
    sizes = []
    handles = _wrap(monkeypatch, sizes)
    with pytest.raises(ValueError, match="byte budget"):
        reader.read_record(path)
    assert sizes == [reader.MAX_BYTES + 1]
    assert all(handle.closed for handle in handles)


@pytest.mark.parametrize("change", ["replace", "append", "remove"])
def test_changed_registry_record_is_withheld_not_salvaged(tmp_path, monkeypatch, change):
    path = tmp_path / "123.json"
    _record(path)
    before = path.read_bytes()

    def changed():
        if change == "replace":
            path.rename(tmp_path / "preserved-old")
            _record(path, pid=456)
        elif change == "append":
            with path.open("ab") as handle:
                handle.write(b" ")
        else:
            path.unlink()

    sizes = []
    handles = _wrap(monkeypatch, sizes, changed)
    with pytest.raises((OSError, ValueError)):
        reader.read_record(path)
    assert sizes == [len(before)]
    assert all(handle.closed for handle in handles)
    if change == "replace":
        assert (tmp_path / "preserved-old").read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_unsafe_metadata_is_never_body_read_or_removed(tmp_path, monkeypatch, kind):
    path, target = tmp_path / "123.json", tmp_path / "owned-original"
    target.write_bytes(b'{"private":"QUOTED NEVER EXECUTE"}')
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("POSIX fixture")
        os.mkfifo(path)
    sizes = []
    _wrap(monkeypatch, sizes)
    with pytest.raises((OSError, ValueError)):
        reader.read_record(path)
    assert sizes == [] and path.exists()
    assert target.read_bytes() == b'{"private":"QUOTED NEVER EXECUTE"}'


def test_foreign_uid_is_not_assumed_to_be_authorized(tmp_path, monkeypatch):
    path = tmp_path / "123.json"
    _record(path)
    owner = path.stat().st_uid
    sizes = []
    _wrap(monkeypatch, sizes)
    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: owner + 1)
        with pytest.raises(ValueError, match="owner"):
            reader.read_record(path)
    assert sizes == []


@pytest.mark.parametrize("raw", [b'{"x":NaN}', b'{"x":1e400}', b'{"x":1,"x":2}',
                                  b'\xff', b'[]', b'null',
                                  b'{"x":' + b'['*2000 + b']'*2000 + b'}'])
def test_unsupported_json_has_no_partial_salvage(tmp_path, raw):
    path = tmp_path / "123.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        reader.read_record(path)


def test_directory_entry_and_record_caps_are_independent(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    for index in range(20):
        _record(root / f"{index+1}.json", pid=index+1)
    monkeypatch.setattr(reader, "MAX_ENTRIES", 8)
    values, info = reader.scan(root, maximum=20)
    assert len(values) == 8 and info["entries_observed"] == 8
    assert info["truncated"] is True
    values, info = reader.scan(root, maximum=2)
    assert len(values) == 2 and info["truncated"] is True


def test_zero_read_capacity_does_not_enumerate(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "scandir", lambda *a: pytest.fail("no enumeration"))
    values, info = reader.scan(tmp_path / "absent", maximum=0)
    assert values == [] and info["truncated"] is True


def _activity_scope(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    monkeypatch.setattr(activity, "observation_roots", lambda: [home])
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(activity.tempfile, "gettempdir", lambda: str(tmp_path))
    return home / "runs"


def test_activity_refuses_same_unsafe_metadata_as_ps(tmp_path, monkeypatch):
    root = _activity_scope(tmp_path, monkeypatch)
    root.mkdir(parents=True)
    outside = tmp_path / "owned-quoted"
    _record(outside, private="NO PUBLIC BODY")
    (root / "123.json").symlink_to(outside)
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda *a: pytest.fail("no probe"))
    value = activity.activity_snapshot()
    assert value["processes"] == [] and value["diagnostics"]
    assert "NO PUBLIC BODY" not in json.dumps(value)
    assert (root / "123.json").is_symlink()


def test_redacted_host_label_cannot_authorize_local_pid_probe(tmp_path, monkeypatch):
    root = _activity_scope(tmp_path, monkeypatch)
    # display() strips C0; exact recorded host equality must precede rendering.
    _record(root / "123.json", host=socket.gethostname()+"\x00")
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda *a: pytest.fail("no probe"))
    value = activity.activity_snapshot()
    assert value["processes"][0]["presence"] == "unknown_host"


def test_same_pid_time_and_host_do_not_merge_different_registry_references(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    _activity_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(activity, "observation_roots", lambda: [first, second])
    _record(first / "runs" / "123.json", repo="workspace-a")
    _record(second / "runs" / "123.json", repo="workspace-b")
    monkeypatch.setattr(activity.process_identity, "pid_alive", lambda pid: True)
    value = activity.activity_snapshot()
    assert len(value["processes"]) == 2
    assert {row["workspace"] for row in value["processes"]} == {"workspace-a", "workspace-b"}
    assert all(not row["controllable"] and row["progress"] == "untracked"
               for row in value["processes"])


def test_changed_selected_directory_withholds_records_before_pid_probe(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    _record(root / "123.json")
    original = reader._scan

    def changed(path, maximum, info):
        values = original(path, maximum, info)
        root.rename(tmp_path / "preserved-old-directory")
        root.mkdir()
        return values

    monkeypatch.setattr(reader, "_scan", changed)
    values, info = reader.scan(root)
    assert values == [] and info["error"]
    assert (tmp_path / "preserved-old-directory" / "123.json").exists()


def test_foreign_directory_does_not_allow_an_owned_file_to_widen_reads(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    _record(root / "123.json")
    owner = root.stat().st_uid
    monkeypatch.setattr(reader, "_scan", lambda *a: pytest.fail("no foreign directory reads"))
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "geteuid", lambda: owner + 1)
        values, info = reader.scan(root)
    assert values == [] and info["error"]


def test_public_snapshot_has_no_argv_or_private_files(tmp_path, monkeypatch):
    root = tmp_path / "owned-runs"
    _record(root / "123.json", argv=["PRIVATE ARGV"], body="PRIVATE BODY")
    monkeypatch.setattr(registry, "_registry_candidates", lambda: [root])
    monkeypatch.setattr(registry, "_own_path", None)
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    snapshot = registry.run_snapshot()
    assert "PRIVATE" not in json.dumps(snapshot)
    assert "_file" not in snapshot["runs"][0]
    assert "_file" in registry.list_runs()[0]  # Existing explicit maintenance ABI only.
