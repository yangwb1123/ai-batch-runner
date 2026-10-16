"""A registry query must never create a writer/probe or imply full inventory."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from pbatch import registry


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "owned-runs"
    monkeypatch.setattr(registry, "_REGISTRY_DIR", path)
    monkeypatch.setattr(registry, "_registry_candidates", lambda: [path])
    monkeypatch.setattr(registry, "_own_path", None)
    return path


def _record(root, pid=123, **extra):
    root.mkdir(exist_ok=True)
    path = root / f"{pid}.json"
    path.write_text(json.dumps({"pid": pid, "host": socket.gethostname(),
                               "mode": "batch", "repo": "owned-workspace",
                               "session": "owned-session", "status": "running",
                               "start": 100, "last_active": 200, **extra}) + "\n")
    return path


def test_default_ps_on_absent_registry_has_no_filesystem_side_effects(root, monkeypatch, capsys):
    monkeypatch.setattr(registry, "_prepare_registry_dir",
                        lambda *a: pytest.fail("read must not prepare writable directory"))
    registry.ps_main(["--json"])
    assert json.loads(capsys.readouterr().out) == []
    assert not root.exists()


def test_listing_existing_registry_never_calls_writer_probe(root, monkeypatch):
    path = _record(root)
    before = path.read_bytes()
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    monkeypatch.setattr(registry, "_prepare_registry_dir",
                        lambda *a: pytest.fail("no write probe"))
    assert registry.list_runs()[0]["pid"] == 123
    assert path.read_bytes() == before
    assert list(root.iterdir()) == [path]


def test_ps_does_not_publish_argv_extra_bodies_or_trusted_file_reference(root, monkeypatch, capsys):
    _record(root, argv=["--prompt=PRIVATE-PROMPT", "--token", "PRIVATE-TOKEN"],
            prompt="PRIVATE-BODY", payload={"private": "PRIVATE-PAYLOAD"},
            _file="/unadmitted/PRIVATE-PATH")
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    registry.ps_main(["--json"])
    value = capsys.readouterr().out
    assert "PRIVATE-" not in value
    assert "argv" not in value and "_file" not in value
    assert json.loads(value)[0]["pid"] == 123
    registry.ps_main([])
    value = capsys.readouterr().out
    assert "ARGV" not in value and "PRIVATE-" not in value


def test_ps_literals_strip_terminal_c1_and_bidi_controls(root, monkeypatch, capsys):
    _record(root, mode="batch\x1b[31m", repo="quoted\u202e\x85", session="session\x00")
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    registry.ps_main([])
    output = capsys.readouterr().out
    assert "quoted" in output
    assert all(control not in output for control in ("\x1b", "\x00", "\x85", "\u202e"))


@pytest.mark.parametrize("tail", [b'\xff', b'{"pid":123,"pid":456}', b'{"pid":123,"x":NaN}'])
def test_malformed_registry_cannot_crash_probe_pid_or_disappear_without_diagnostic(
        root, monkeypatch, capsys, tail):
    root.mkdir()
    (root / "123.json").write_bytes(tail)
    monkeypatch.setattr(registry, "_alive", lambda *a: pytest.fail("no malformed PID probe"))
    registry.ps_main(["--json"])
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    assert output.err and "PRIVATE" not in output.err


def test_oversized_metadata_is_not_read_whole_or_given_to_decoder(root, monkeypatch, capsys):
    root.mkdir()
    (root / "123.json").write_bytes(b'{"pid":123,"private":"' + b"X" * (2 * 1024 * 1024) + b'"}')
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("no whole text read"))
    monkeypatch.setattr(registry, "_alive", lambda *a: pytest.fail("no refused PID probe"))
    registry.ps_main(["--json"])
    output = capsys.readouterr()
    assert json.loads(output.out) == [] and output.err


def test_same_pid_from_foreign_host_is_not_probed(root, monkeypatch):
    _record(root, host="foreign-source", status="dead")
    monkeypatch.setattr(registry, "_alive", lambda *a: pytest.fail("no foreign PID probe"))
    value = registry.list_runs()[0]
    assert value["status"] == "unknown"


def test_registered_running_is_not_verified_progress_or_process_identity(root, monkeypatch, capsys):
    _record(root)
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    registry.ps_main(["--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["presence"] == "pid_present_or_unknown"
    assert row["liveness"] == "not_verified"
    assert row["progress"] == "untracked" and row["controllable"] is False


def test_diagnostic_snapshot_reports_scope_and_bounded_coverage(root, monkeypatch):
    _record(root)
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    value = registry.run_snapshot()
    assert value["schema_version"] == 1
    assert value["complete_inventory"] is False and value["grants_execution"] is False
    assert len(value["runs"]) == 1
    assert value["coverage"]["available"] is True


def test_read_selection_uses_existing_fallback_without_creating_missing_primary(root, tmp_path, monkeypatch):
    fallback = tmp_path / "existing-fallback"
    _record(fallback)
    monkeypatch.setattr(registry, "_registry_candidates", lambda: [root, fallback])
    monkeypatch.setattr(registry, "_alive", lambda pid: True)
    assert registry.list_runs()[0]["pid"] == 123
    assert not root.exists()


def test_refused_existing_registry_does_not_widen_to_fallback(root, tmp_path, monkeypatch, capsys):
    outside = tmp_path / "not-selected"
    _record(outside)
    root.symlink_to(outside, target_is_directory=True)
    fallback = tmp_path / "other-runs"
    _record(fallback, pid=456)
    monkeypatch.setattr(registry, "_registry_candidates", lambda: [root, fallback])
    monkeypatch.setattr(registry, "_alive", lambda *a: pytest.fail("no widening"))
    registry.ps_main(["--json"])
    output = capsys.readouterr()
    assert json.loads(output.out) == [] and output.err


def test_ordinary_query_does_not_prune_even_when_pid_is_absent(root, monkeypatch):
    path = _record(root)
    monkeypatch.setattr(registry, "_alive", lambda pid: False)
    registry.ps_main(["--json"])
    assert path.exists()
