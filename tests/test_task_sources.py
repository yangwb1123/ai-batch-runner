"""Source registration and bounded reads are separate from execution/cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pbatch import task_sources
from pbatch.task_center_io import read_rows


@pytest.fixture
def home(tmp_path, monkeypatch):
    value = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(value))
    return value


def test_custom_source_is_durable_private_and_registration_removal_keeps_data(home, tmp_path):
    data = tmp_path / "custom"; data.mkdir()
    state = tmp_path / "output" / "state.jsonl"
    item = task_sources.source("aero", str(data), campaign_state=str(state))
    task_sources.register_source(item)
    assert task_sources.registered_sources() == [item]
    assert task_sources.catalog_path().stat().st_mode & 0o777 == 0o600
    task_sources.unregister_source(item["source_id"])
    assert data.is_dir()
    assert task_sources.registered_sources() == []


def test_registration_does_not_overwrite_conflicting_alias(home, tmp_path):
    data = tmp_path / "data"; data.mkdir()
    item = task_sources.source("one", str(data))
    task_sources.register_source(item)
    before = task_sources.catalog_path().read_bytes()
    with pytest.raises(ValueError, match="already registered"):
        task_sources.register_source(task_sources.source("other", str(data)))
    assert task_sources.catalog_path().read_bytes() == before


def test_corrupt_catalog_is_not_repaired_or_overwritten(home, tmp_path):
    path = task_sources.catalog_path(); path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    data = tmp_path / "data"; data.mkdir()
    with pytest.raises(ValueError, match="invalid"):
        task_sources.register_source(task_sources.source("test", str(data)))
    assert path.read_text() == "broken"


def test_unknown_catalog_version_and_identity_fail_closed(home, tmp_path):
    data = tmp_path / "data"; data.mkdir()
    item = task_sources.source("test", str(data))
    path = task_sources.catalog_path(); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 2, "sources": [item]}))
    with pytest.raises(ValueError, match="schema"):
        task_sources.registered_sources()
    item["source_id"] = "forged"
    path.write_text(json.dumps({"schema_version": 1, "sources": [item]}))
    with pytest.raises(ValueError, match="mismatch"):
        task_sources.registered_sources()


def test_source_symlink_is_rejected(home, tmp_path):
    target = tmp_path / "real"; target.mkdir()
    alias = tmp_path / "alias"; alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        task_sources.source("test", str(alias))
    path = task_sources.catalog_path(); path.parent.mkdir(parents=True)
    other = tmp_path / "other.json"; other.write_text("{}")
    path.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        task_sources.registered_sources()
    assert other.read_text() == "{}"


def test_ledger_symlink_or_fifo_never_read(home, tmp_path):
    secret = tmp_path / "secret.jsonl"; secret.write_text('{"secret":"hidden"}\n')
    link = tmp_path / "link.jsonl"; link.symlink_to(secret)
    rows, info = read_rows(link)
    assert rows == [] and info["error"]
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    rows, info = read_rows(fifo)
    assert rows == [] and info["error"]


def test_jsonl_byte_line_and_record_caps_are_visible(tmp_path, monkeypatch):
    from pbatch import task_center_io as io
    monkeypatch.setattr(io, "MAX_BYTES", 80)
    monkeypatch.setattr(io, "MAX_LINE_BYTES", 30)
    monkeypatch.setattr(io, "MAX_RECORDS", 2)
    path = tmp_path / "ledger.jsonl"
    path.write_text('x' * 300 + '\n' + '{"index":1}\n{"index":2}\n{"index":3}\n')
    rows, info = io.read_rows(path)
    assert rows == [{"index": 2}, {"index": 3}]
    assert info["truncated"] is True
    path.write_text('{"large":"' + 'x' * 40 + '"}\n')
    rows, info = io.read_rows(path)
    assert rows == [] and info["malformed"] == 1


def test_discovery_does_not_create_missing_catalog_or_scan_custom_parents(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    (elsewhere / "agents").mkdir()
    assert task_sources.discover_sources() == ([], [])
    assert not home.exists()


def test_legacy_default_is_read_only_and_explicit_boundary_disables_it(tmp_path, monkeypatch):
    from pbatch import user_dirs
    monkeypatch.setattr(user_dirs, "_home", lambda: tmp_path)
    for key in ("PBATCH_RUNTIME_HOME", "PBATCH_HOME", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    legacy = tmp_path / ".codex" / "pbatch" / "projects" / "legacy"
    legacy.mkdir(parents=True)
    rows, notes = task_sources.discover_sources()
    assert not notes and [row["data_dir"] for row in rows] == [str(legacy)]
    assert not (tmp_path / ".abr").exists()
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "isolated"))
    assert task_sources.discover_sources() == ([], [])


def test_cli_add_remove_does_not_remove_runtime(home, tmp_path, capsys):
    from pbatch.ops import ops_main
    data = tmp_path / "data"; data.mkdir()
    assert ops_main(["sources", "add", "custom", "--data-dir", str(data)]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert ops_main(["sources", "remove", registered["source_id"]]) == 0
    capsys.readouterr()
    assert data.exists()
