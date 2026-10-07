"""Retention review and protection declarations never reclaim resources or reset runs."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_task_repair_preview import source, _checkpoint, _publish, _goal, _write
from pbatch import task_center, task_sources, task_retention_store as store, task_retention_view as view_mod
from pbatch.task_retention_schema import (REVIEW_SECONDS, MAX_AUDIT, decode, validate_state, instant, encoded)
from pbatch.task_lifecycle import run_path
from pbatch.task_state_io import locked
from pbatch.ops import ops_main


@pytest.fixture
def retained(source):
    file = Path(source["data_dir"]) / "retained/report.md"
    file.parent.mkdir()
    file.write_text("private recovery evidence\n")
    value = store.register(source["source_id"], str(file), "file", "operator", "preserve evidence", confirm=True)
    return source, file, value


def _view(retained, **kwargs):
    source, _, value = retained
    return next(row for row in task_center.snapshot(source_id=source["source_id"], **kwargs)["resources"]
                if row["resource_id"] == value["resource_id"])


def _keep(row):
    assert row["plan"]["decision"] == "preserve" and row["cleanup_authority"] == "not_granted"
    assert all(flag is False for flag in row["capabilities"].values())
    assert row["archive_verified"] is False and row["estimated_reclaimable_bytes"] is None
    assert row["availability"]["content_sha256"] is None
    assert {item["id"] for item in row["plan"]["required_disposal_checks"]} == set(view_mod.REQUIRED_CHECKS)
    assert all(item["state"] == "required_not_evaluated" for item in row["plan"]["required_disposal_checks"])


def test_initial_72_hours_private_state_and_expiry_remain_preserve(retained):
    source, file, value = retained
    path = store.resource_path(value["resource_id"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert instant(value["review_at"]) - instant(value["registered_at"]) == REVIEW_SECONDS
    before = (file.read_bytes(), path.read_bytes())
    for at, policy in [(instant(value["registered_at"]), "retention_window_preserve"),
                       (instant(value["review_at"]), "review_due_preserve")]:
        row = _view(retained, now=at)
        assert row["policy"] == policy
        assert row["availability"]["state"] == "matching_stat_only"
        assert row["references"]["run_association"] == "untracked"
        _keep(row)
    assert (file.read_bytes(), path.read_bytes()) == before
    assert str(file) not in json.dumps(_view(retained))  # public paths are relative/redacted only


def test_duplicate_registration_never_renews_review_time_even_after_expiry_or_pin(retained, monkeypatch):
    source, file, value = retained
    path = store.resource_path(value["resource_id"])
    before = path.read_bytes()
    monkeypatch.setattr(store, "stamp", lambda: "2030-01-01T00:00:00+00:00")
    repeated = store.register(source["source_id"], str(file), "file", "operator", "preserve evidence", confirm=True)
    assert repeated == value and path.read_bytes() == before
    for field, argument in [("kind", "directory"), ("by", "different operator"), ("reason", "different reason")]:
        params = dict(kind="file", by="operator", reason="preserve evidence")
        params[field] = argument
        with pytest.raises(ValueError):
            store.register(source["source_id"], str(file), **params, confirm=True)
    pinned = store.protect(value["resource_id"], "operator", "keep forever", 1)
    assert store.register(source["source_id"], str(file), "file", "operator", "preserve evidence", confirm=True) == pinned


def test_extensions_only_increase_review_and_pin_is_permanent(retained):
    value = retained[2]
    extended = store.protect(value["resource_id"], "operator", "more review time", 1, "2030-01-01T00:00:00Z")
    assert extended["version"] == 2 and not extended["pinned"]
    assert instant(extended["registered_at"]) == instant(value["registered_at"])
    for until in (value["review_at"], "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z", "2031-01-01T00:00:00"):
        with pytest.raises(ValueError):
            store.protect(value["resource_id"], "operator", "cannot shorten", 2, until)
    pinned = store.protect(value["resource_id"], "operator", "permanent preservation", 2)
    assert pinned["version"] == 3 and pinned["pinned"]
    assert store.protect(value["resource_id"], "operator", "already protected", 3) == pinned
    with pytest.raises(ValueError, match="cannot be removed"):
        store.protect(value["resource_id"], "operator", "not needed", 3, "2032-01-01T00:00:00Z")
    _keep(_view(retained, now=2_000_000_000))
    assert _view(retained)["policy"] == "permanently_pinned"


def test_pin_works_after_source_and_resource_loss(retained):
    source, file, value = retained
    file.unlink()  # only this test's owned synthetic file
    task_sources.unregister_source(source["source_id"])
    pinned = store.protect(value["resource_id"], "operator", "protect missing-source evidence", 1)
    assert pinned["pinned"] and store.load_resource(value["resource_id"]) == pinned


@pytest.mark.parametrize("version", [None, True, 0, -1, 1.0, 2])
def test_protection_revision_is_strict_and_failed_write_keeps_original(retained, version):
    value = retained[2]
    path = store.resource_path(value["resource_id"])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store.protect(value["resource_id"], "operator", "pin", version)
    assert path.read_bytes() == before


def test_permission_and_explicit_consent_fail_before_registration_reads(monkeypatch):
    monkeypatch.setattr(store, "resolve_source", lambda *args: pytest.fail("no reads before consent"))
    with pytest.raises(ValueError, match="--confirm"):
        store.register("source", "/any/path", "file", "operator", "reason")
    assert not store.resource_root().exists()


@pytest.mark.parametrize("damage", ["root", "workspace", "outside", "symlink", "fifo", "kind", "unknown_kind", "uid", "metadata"])
def test_unsafe_or_wrongly_scoped_resources_refuse_without_registering(source, tmp_path, monkeypatch, damage):
    root = Path(source["data_dir"])
    path, kind = root / "safe.md", "file"
    path.write_text("retained")
    if damage == "root":
        path, kind = root, "directory"
    elif damage == "workspace":
        path = Path(source["workspace"]) / "source.py"
        path.write_text("not runtime data")
    elif damage == "outside":
        path = tmp_path / "elsewhere"
        path.write_text("unselected")
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(root / "missing")
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif damage == "kind":
        kind = "directory"
    elif damage == "unknown_kind":
        kind = "garbage"
    elif damage == "uid":
        monkeypatch.setattr(store.os, "geteuid", lambda: -1)
    else:
        path = store.resource_root() / "self.json"
        path.parent.mkdir(parents=True)
        path.write_text("not a resource")
    with pytest.raises((ValueError, OSError)):
        store.register(source["source_id"], str(path), kind, "operator", "preserve", confirm=True)
    assert not list(store.resource_root().glob("rr-*.json"))


def test_overlapping_directories_aliases_and_hardlinks_cannot_expand_scope(retained):
    source, file, value = retained
    with pytest.raises(ValueError, match="overlap"):
        store.register(source["source_id"], str(file.parent), "directory", "operator", "parent", confirm=True)
    alias = file.with_name("other.md")
    os.link(file, alias)
    with pytest.raises(ValueError, match="another filesystem"):
        store.register(source["source_id"], str(alias), "file", "operator", "alias", confirm=True)
    assert store.load_resource(value["resource_id"]) == value


@pytest.mark.parametrize("damage", ["replace", "same_size", "missing", "symlink", "changed_source", "root_identity"])
def test_resource_changes_are_observations_not_archive_or_cleanup_evidence(retained, monkeypatch, damage):
    source, file, value = retained
    if damage == "replace":
        other = file.with_name("new.md")
        other.write_bytes(file.read_bytes())
        other.replace(file)
    elif damage == "same_size":
        file.write_bytes(b"x" * file.stat().st_size)
    elif damage == "missing":
        file.unlink()
    elif damage == "symlink":
        file.unlink()
        file.symlink_to(file.parent)
    elif damage == "changed_source":
        task_sources.unregister_source(source["source_id"])
        task_sources.register_source(task_sources.source("new name", source["data_dir"], source["workspace"]))
    else:
        monkeypatch.setattr(view_mod, "root_identity", lambda path: {"device": 0, "inode": 0, "uid": 0, "mode": 0})
    row = _view(retained)
    assert row["availability"]["state"] != "matching_stat_only"
    _keep(row)


def test_worktree_is_declared_not_verified_and_directory_bytes_are_unknown(source):
    folder = Path(source["data_dir"]) / "worktrees/failed"
    folder.mkdir(parents=True)
    (folder / "source.py").write_text("unsaved failed edits")
    value = store.register(source["source_id"], str(folder), "worktree", "operator", "protect edits", confirm=True)
    row = _view((source, folder, value))
    assert row["availability"]["observed_file_bytes"] is None
    assert not row["availability"]["tree_contents_checked"]
    assert "directory_contents_and_Git_state_not_verified" in row["plan"]["blockers"]
    _keep(row)


def test_explicit_run_links_reference_goals_and_repairs_without_declared_name_matching(source):
    checkpoint = _checkpoint(source)
    _publish(source, checkpoint)
    goal = _goal(source, checkpoint["run_id"])
    file = Path(source["data_dir"]) / "result.md"
    file.write_text("preserve linked result")
    run_bytes = run_path(source, checkpoint["run_id"]).read_bytes()
    value = store.register(source["source_id"], str(file), "file", "operator", "link recorded run", confirm=True,
                           run_id=checkpoint["run_id"])
    view = task_center.snapshot(source_id=source["source_id"], limit=1)
    row = view["resources"][0]
    assert row["references"]["goal_refs"] == [goal["goal_key"]]
    assert view["goals"][0]["resource_links"] == [value["resource_id"]]
    assert view["tasks"][0]["resource_links"] == [value["resource_id"]]
    assert row["references"]["bound_run_observed_status"] == "failed"
    assert run_path(source, checkpoint["run_id"]).read_bytes() == run_bytes
    _keep(row)


def test_immutable_binding_does_not_rebind_a_replaced_path(retained):
    source, file, value = retained
    replacement = file.with_name("replacement.md")
    replacement.write_bytes(file.read_bytes())
    replacement.replace(file)
    before = store.resource_path(value["resource_id"]).read_bytes()
    with pytest.raises(ValueError, match="cannot be rewritten"):
        store.register(source["source_id"], str(file), "file", "operator", "preserve evidence", confirm=True)
    assert store.resource_path(value["resource_id"]).read_bytes() == before


def test_registration_rechecks_source_and_resource_under_catalog_lock(source, monkeypatch):
    file = Path(source["data_dir"]) / "changing.md"
    file.write_text("before")
    original = store._scope
    calls = []
    def changing(*args):
        if calls:
            file.write_text("changed while waiting for registration lock")
        result = original(*args)
        calls.append(True)
        return result
    monkeypatch.setattr(store, "_scope", changing)
    with pytest.raises(ValueError, match="changed during"):
        store.register(source["source_id"], str(file), "file", "operator", "preserve", confirm=True)
    assert not list(store.resource_root().glob("rr-*.json"))
    assert file.read_text() == "changed while waiting for registration lock"


def test_empty_query_does_not_create_registry_or_protection_locks(source):
    view = task_center.snapshot(source_id=source["source_id"])
    assert view["resources"] == [] and not store.resource_root().exists()


def test_resource_keys_use_full_path_identity_not_matching_redacted_display(source):
    resources = []
    for secret in ("secret_one", "secret_two"):
        file = Path(source["data_dir"]) / ("token=" + secret)
        file.write_text("private result")
        resources.append(store.register(source["source_id"], str(file), "file", "operator", "protect", confirm=True))
    rows = task_center.snapshot(source_id=source["source_id"])["resources"]
    assert resources[0]["resource_id"] != resources[1]["resource_id"]
    assert rows[0]["relative_path_display"] == rows[1]["relative_path_display"]
    assert "secret_one" not in json.dumps(rows) and "secret_two" not in json.dumps(rows)


def test_selected_source_cannot_crawl_other_registered_resource_roots(retained, monkeypatch):
    other_root = Path(retained[0]["data_dir"]).parent / "other-runtime"
    other_root.mkdir()
    other_source = task_sources.source("another", str(other_root))
    task_sources.register_source(other_source)
    file = other_root / "report.md"
    file.write_text("different scope")
    value = store.register(other_source["source_id"], str(file), "file", "operator", "preserve", confirm=True)
    original = view_mod.observed_stat
    def scoped(path):
        assert other_root not in path.parents and path != other_root, "source filtering must not stat excluded roots"
        return original(path)
    monkeypatch.setattr(view_mod, "observed_stat", scoped)
    rows = task_center.snapshot(source_id=retained[0]["source_id"])["resources"]
    assert len(rows) == 1 and rows[0]["resource_id"] != value["resource_id"]


def test_missing_run_never_creates_link_or_resource_record(source):
    file = Path(source["data_dir"]) / "file.md"
    file.write_text("preserve")
    with pytest.raises(ValueError, match="actual matching checkpoint"):
        store.register(source["source_id"], str(file), "file", "operator", "unknown run", confirm=True, run_id="pl-" + "b" * 32)
    assert not list(store.resource_root().glob("rr-*.json"))


def test_active_parent_children_partial_and_unknown_lineage_protect_full_window(source):
    checkpoint = _checkpoint(source)
    _publish(source, checkpoint)
    file = Path(source["data_dir"]) / "result.md"
    file.write_text("preserve")
    value = store.register(source["source_id"], str(file), "file", "operator", "known run", confirm=True, run_id=checkpoint["run_id"])
    records = [{"run_id": "child-" + str(index), "task_id": str(index), "status": "running", "phase": "execution", "attempt": 1,
        "ts": "2026-10-10T00:00:00Z", "parent_run_id": checkpoint["run_id"], "parent_source_id": source["source_id"]}
        for index in range(12)]
    _write(Path(source["data_dir"]) / "agents/ledger.jsonl", records)
    row = _view((source, file, value), limit=1)
    assert row["references"]["children"]["active_execution_observations"] == 12
    assert len(row["references"]["children"]["child_task_refs"]) == 8
    assert row["references"]["children"]["omitted_child_refs"] == 4
    assert "active_run_or_child_execution_observations" in row["plan"]["blockers"]
    assert row == _view((source, file, value), limit=200)
    _keep(row)
    with (Path(source["data_dir"]) / "agents/ledger.jsonl").open("ab") as handle:
        handle.write(b"unfinished")
    assert "partial_reference_observation_window" in _view((source, file, value))["plan"]["blockers"]


def test_registry_corruption_capacity_and_busy_lock_preserve_all_files(retained, monkeypatch):
    source, file, value = retained
    before = file.read_bytes()
    monkeypatch.setattr(store, "MAX_RESOURCES", 1)
    other = file.with_name("other.md")
    other.write_text("second retained output")
    with pytest.raises(ValueError, match="capacity"):
        store.register(source["source_id"], str(other), "file", "operator", "second", confirm=True)
    path = store.resource_path(value["resource_id"])
    path.write_bytes(b'{"broken":')
    with pytest.raises(ValueError):
        store.protect(value["resource_id"], "operator", "do not repair corruption", 1)
    with pytest.raises(ValueError):
        store.register(source["source_id"], str(other), "file", "operator", "second", confirm=True)
    assert file.read_bytes() == before and path.read_bytes() == b'{"broken":'
    assert task_center.snapshot(source_id=source["source_id"])["diagnostics"]


def test_reserve_last_audit_slot_and_unicode_byte_budget_for_pin(retained, monkeypatch):
    key = retained[2]["resource_id"]
    monkeypatch.setattr(store, "stamp", lambda: "2030-12-01T00:00:00Z")
    for version in range(1, MAX_AUDIT - 1):
        value = store.protect(key, "😀" * 128, "😀" * 256, version, f"{2030 + version}-01-01T00:00:00Z")
    assert value["version"] == MAX_AUDIT - 1
    before = store.resource_path(key).read_bytes()
    with pytest.raises(ValueError, match="final protection slot"):
        store.protect(key, "operator", "one too many", MAX_AUDIT - 1, "2050-01-01T00:00:00Z")
    assert store.resource_path(key).read_bytes() == before
    value = store.protect(key, "😀" * 128, "😀" * 256, MAX_AUDIT - 1)
    validate_state(value)
    assert value["pinned"] and len(encoded(value)) <= 32768


@pytest.mark.parametrize("field,item", [("schema_version", True), ("version", True), ("kind", []), ("run_id", False),
    ("relative_path", "../outside"), ("relative_path", "nested/../file"), ("pinned", 1), ("source_digest", "bad")])
def test_closed_strict_schema_never_interprets_unknown_controls(retained, field, item):
    value = copy.deepcopy(retained[2])
    value[field] = item
    with pytest.raises((ValueError, TypeError)):
        decode(json.dumps(value).encode())
    value = copy.deepcopy(retained[2])
    value["delete"] = True
    with pytest.raises(ValueError):
        decode(json.dumps(value).encode())


def test_duplicate_nonfinite_non_utf8_or_incomplete_json_is_rejected(retained):
    raw = encoded(retained[2])
    for payload in (raw[:-1] + b',"version":1}', raw.replace(b'"version":1', b'"version":NaN'), b'\xff', raw[:-1]):
        with pytest.raises(ValueError):
            decode(payload)


def test_metadata_queries_no_body_reads_processes_git_writes_or_disposal(retained, monkeypatch):
    import subprocess
    before = retained[1].read_bytes()
    original = store._open_regular
    def admitting(path):
        assert path != retained[1], "body reads are not part of stat-only preservation views"
        return original(path)
    monkeypatch.setattr(store, "_open_regular", admitting)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("read query spawned process"))
    monkeypatch.setattr(store, "atomic_write", lambda *args: pytest.fail("read query wrote metadata"))
    _keep(_view(retained))
    assert retained[1].read_bytes() == before


def test_concurrent_pin_or_extension_and_catalog_lock_have_no_lost_update(retained):
    key = retained[2]["resource_id"]
    with locked(store.resource_root() / ".catalog"):
        with pytest.raises(BlockingIOError):
            store.protect(key, "operator", "busy", 1)
    def update(_):
        try:
            return store.protect(key, "operator", "concurrent", 1)["version"]
        except (ValueError, BlockingIOError):
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, range(2)))
    assert results.count(2) == 1
    assert store.load_resource(key)["version"] == 2


def test_cli_lists_same_shared_plan_and_has_no_cleanup_unpin_or_archive_action(retained, capsys):
    assert ops_main(["retention", "list", "--json", "--source", retained[0]["source_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["resources"] == [_view(retained)]
    assert ops_main(["retention", "pin", retained[2]["resource_id"], "--if-version", "1", "--by", "operator", "--reason", "protect"]) == 0
    assert json.loads(capsys.readouterr().out)["cleanup_authority"] == "not_granted"
    for action in ("delete", "cleanup", "unpin", "archive", "force"):
        with pytest.raises(SystemExit) as exc:
            ops_main(["retention", action, retained[2]["resource_id"]])
        assert exc.value.code == 2
    assert retained[1].read_text() == "private recovery evidence\n"
