"""Declared requests never dispatch, mutate deadlines or manufacture original budgets."""
from __future__ import annotations

import copy
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_task_repair_preview import source, _checkpoint, _goal, _pipeline, _publish, _write
from pbatch import task_center, task_goal_store, task_sources
from pbatch.ops import ops_main
from pbatch.task_lifecycle import run_path
from pbatch import task_repair_store as store
from pbatch.task_repair_binding import read_manifest
from pbatch.task_repair_schema import (MAX_BYTES, MAX_REQUESTS, decode, encoded, fingerprint,
                                       request_key, validate_manifest, validate_state)
from pbatch.task_state_io import atomic_write, locked


def manifest_for(source, goal, task):
    preview = task["repair_preview"]
    return {"schema_version": 1, "source_id": source["source_id"], "task_id": task["task_id"],
        "run_id": task["run_id"], "goal_id": goal["goal_key"], "goal_version": goal["version"],
        "contract_digest": goal["contract_digest"], "stage_key": preview["candidate_stage"]["stage_key"],
        "checkpoint": {key: preview["checkpoint_ref"][key] for key in ("sequence", "sha256")},
        "limits": {"requested_deadline_at": "2030-01-01T00:00:00Z", "max_attempts": 1,
                   "max_elapsed_seconds": 300, "max_total_tokens": 10000,
                   "max_cost": {"amount": 2.0, "currency": "USD"}}}


@pytest.fixture
def prepared(source):
    value = _checkpoint(source)
    _publish(source, value)
    goal = _goal(source, value["run_id"])
    return source, goal, value, manifest_for(source, goal, _pipeline(source))


def register(prepared, **kwargs):
    source, _, _, manifest = prepared
    return store.register(source["source_id"], manifest, kwargs.get("by", "operator"), kwargs.get("reason", "inspect same failure"))


def view_of(value, source, **kwargs):
    view = task_center.snapshot(source_id=source["source_id"], **kwargs)
    return next(row for row in view["repairs"] if row["repair_id"] == value["repair_id"])


def test_pinned_request_is_private_and_unenforced_with_immutable_audit(prepared):
    source, goal, _, manifest = prepared
    value = register(prepared)
    validate_state(value)
    path = store.request_path(value["repair_id"])
    assert path.stat().st_mode & 0o777 == 0o600
    public = view_of(value, source)
    assert public["status"] == "declared_request_only" and public["version"] == 1
    assert public["execution_authority"] == "not_granted" and public["original_limits_verified"] is False
    assert public["limits"] == manifest["limits"]
    assert public["limits_basis"] == "operator_declared_not_original_budget_verification_or_enforcement"
    assert all(value is False for value in public["capabilities"].values())
    snapshot = task_center.snapshot(source_id=source["source_id"])
    assert snapshot["tasks"][0]["repair_links"] == [value["repair_id"]]
    assert snapshot["goals"][0]["repair_links"] == [value["repair_id"]]
    assert snapshot["goals"][0]["goal_id"] == goal["goal_key"]
    assert str(path) not in json.dumps(public)


def test_duplicate_is_noop_and_cannot_rewrite_limits_or_actor(prepared):
    source, _, _, manifest = prepared
    value = register(prepared)
    before = store.request_path(value["repair_id"]).read_bytes()
    assert register(prepared) == value
    for change in ("deadline", "attempts", "currency", "actor"):
        updated = copy.deepcopy(manifest)
        by = "operator"
        if change == "deadline":
            updated["limits"]["requested_deadline_at"] = "2031-01-01T00:00:00Z"
        elif change == "attempts":
            updated["limits"]["max_attempts"] = 2
        elif change == "currency":
            updated["limits"]["max_cost"]["currency"] = "EUR"
        else:
            by = "different operator"
        assert request_key(updated) == value["repair_id"]
        with pytest.raises(ValueError, match="cannot be rewritten"):
            store.register(source["source_id"], updated, by, "inspect same failure")
    assert store.request_path(value["repair_id"]).read_bytes() == before


def test_withdrawal_is_permanent_and_no_new_deadline_audit_is_created(prepared):
    source, _, _, _ = prepared
    value = register(prepared)
    withdrawn = store.withdraw(value["repair_id"], "operator", "stop request", 1)
    assert withdrawn["version"] == 2
    assert store.withdraw(value["repair_id"], "operator", "already withdrawn", 2) == withdrawn
    assert view_of(withdrawn, source)["status"] == "withdrawn"
    with pytest.raises(ValueError, match="cannot be reactivated"):
        register(prepared)
    assert len(store.load_request(value["repair_id"])["audit"]) == 2


@pytest.mark.parametrize("expected", [0, -1, True, 3, 1.0, None])
def test_withdraw_revision_is_explicit_and_strict(prepared, expected):
    value = register(prepared)
    with pytest.raises(ValueError):
        store.withdraw(value["repair_id"], "operator", "stop", expected)
    assert store.load_request(value["repair_id"])["version"] == 1


def test_withdraw_after_source_removal_goal_loss_and_deadline_expiry(prepared):
    source, goal, _, _ = prepared
    value = register(prepared)
    assert view_of(value, source, now=2_000_000_000)["status"] == "expired_declaration"
    task_sources.unregister_source(source["source_id"])
    task_goal_store.goal_path(goal["goal_key"]).unlink()  # owned test-only metadata
    withdrawn = store.withdraw(value["repair_id"], "operator", "withdraw stale request", 1)
    assert withdrawn["state"] == "withdrawn"


@pytest.mark.parametrize("field,new", [("schema_version", True), ("goal_version", True),
    ("goal_version", 0), ("goal_version", 129), ("stage_key", "../escape"), ("contract_digest", "bad"),
    ("source_id", "src-" + "0" * 24), ("task_id", "tc-" + "0" * 32), ("run_id", "bad")])
def test_invalid_manifest_refuses_before_storage(prepared, field, new):
    source, _, _, manifest = prepared
    manifest[field] = new
    with pytest.raises((ValueError, TypeError)):
        register(prepared)
    assert not store.request_root().exists()


@pytest.mark.parametrize("field,new", [("max_attempts", 0), ("max_attempts", -1), ("max_attempts", True),
    ("max_attempts", 4), ("max_elapsed_seconds", 0), ("max_elapsed_seconds", 86401),
    ("max_total_tokens", float("inf")), ("max_total_tokens", 0),
    ("requested_deadline_at", "2030-01-01T00:00:00"), ("requested_deadline_at", "bad"),
    ("requested_deadline_at", "2000-01-01T00:00:00Z")])
def test_limits_are_explicit_finite_positive_and_expiring(prepared, field, new):
    prepared[3]["limits"][field] = new
    with pytest.raises(ValueError):
        register(prepared)
    assert not store.request_root().exists()


@pytest.mark.parametrize("cost", [{"amount": 0, "currency": "USD"}, {"amount": -1, "currency": "USD"},
    {"amount": True, "currency": "USD"}, {"amount": float("nan"), "currency": "USD"},
    {"amount": float("inf"), "currency": "USD"}, {"amount": 1, "currency": "usd"},
    {"amount": 1, "currency": ""}, {"amount": 1}])
def test_cost_cap_does_not_infer_currency_or_accept_nonfinite_metadata(prepared, cost):
    prepared[3]["limits"]["max_cost"] = cost
    with pytest.raises(ValueError):
        register(prepared)


@pytest.mark.parametrize("change", ["goal_revision", "goal_source", "goal_binding", "checkpoint", "stage", "feed_partial"])
def test_stale_or_mismatched_pins_cannot_be_registered(prepared, change):
    source, goal, checkpoint, manifest = prepared
    if change == "goal_revision":
        task_goal_store.define(source["source_id"], goal["contract"], "operator", "revision", 2)
    elif change == "goal_source":
        other = Path(source["data_dir"]).parent / "other"
        other.mkdir()
        item = task_sources.source("other", str(other))
        task_sources.register_source(item)
        goal = task_goal_store.define(item["source_id"], goal["contract"], "operator", "different scope")
        manifest["goal_id"], manifest["goal_version"] = goal["goal_key"], goal["version"]
        manifest["contract_digest"] = goal["contract_digest"]
    elif change == "goal_binding":
        goal["bindings"] = []
        atomic_write(task_goal_store.goal_path(goal["goal_key"]), goal)
    elif change == "checkpoint":
        checkpoint["name"] = "same sequence, different raw fields"
        atomic_write(run_path(source, checkpoint["run_id"]), checkpoint)
    elif change == "stage":
        manifest["stage_key"] = "st-" + "0" * 32
    else:
        _write(source["events_file"], [{"kind": "unknown"}])
        with open(source["events_file"], "a") as handle:
            handle.write("bad\n")
    with pytest.raises(ValueError):
        register(prepared)
    assert not store.request_root().exists()


@pytest.mark.parametrize("change,expected", [("goal", "stale_goal_revision"),
    ("feed", "stale_observed_checkpoint"), ("durable", "checkpoint_header_conflict_with_goal_view"),
    ("missing", "checkpoint_unavailable_in_view"), ("expired", "expired_declaration"),
    ("missing_goal", "goal_unavailable_in_view"), ("partial", "partial_observation_window")])
def test_queries_disclose_staleness_without_rewriting_request(prepared, change, expected):
    source, goal, checkpoint, _ = prepared
    value = register(prepared)
    before = store.request_path(value["repair_id"]).read_bytes()
    kwargs = {}
    if change == "goal":
        task_goal_store.define(source["source_id"], goal["contract"], "operator", "revision", 2)
    elif change in {"feed", "durable"}:
        checkpoint["seq"] += 1
        if change == "feed":
            _write(Path(source["data_dir"]) / "tasks/lifecycle.jsonl", [checkpoint])
        else:
            atomic_write(run_path(source, checkpoint["run_id"]), checkpoint)
    elif change == "missing":
        _write(Path(source["data_dir"]) / "tasks/lifecycle.jsonl", [])
    elif change == "missing_goal":
        task_goal_store.goal_path(goal["goal_key"]).unlink()
    elif change == "partial":
        Path(source["events_file"]).write_text("unfinished")
    else:
        kwargs["now"] = 2_000_000_000
    public = view_of(value, source, **kwargs)
    assert public["status"] == expected
    assert public["execution_authority"] == "not_granted"
    assert store.request_path(value["repair_id"]).read_bytes() == before


def test_query_uses_admitted_full_window_not_returned_task_limit(prepared):
    source, _, checkpoint, _ = prepared
    value = register(prepared)
    newer = copy.deepcopy(checkpoint)
    newer["run_id"] = "pl-" + "b" * 32
    newer["ts"] = "2026-10-10T00:00:20Z"
    _write(Path(source["data_dir"]) / "tasks/lifecycle.jsonl", [checkpoint, newer])
    one = task_center.snapshot(1, source["source_id"])
    assert one["tasks"][0]["run_id"] != value["manifest"]["run_id"]
    assert one["repairs"] == task_center.snapshot(200, source["source_id"])["repairs"]
    assert one["repairs"][0]["status"] == "declared_request_only"


@pytest.mark.parametrize("mutation", ["digest", "audit", "unknown", "state", "revision"])
def test_corrupt_state_is_visible_and_never_repaired(prepared, mutation):
    source, _, _, _ = prepared
    value = register(prepared)
    if mutation == "digest":
        value["manifest_digest"] = "0" * 64
    elif mutation == "audit":
        value["audit"] = []
    elif mutation == "unknown":
        value["dispatch"] = True
    elif mutation == "state":
        value["state"] = "approved"
    else:
        value["version"] = True
    path = store.request_path(value["repair_id"])
    path.write_bytes(encoded(value))
    before = path.read_bytes()
    view = task_center.snapshot(source_id=source["source_id"])
    assert view["repairs"] == []
    assert any("repair request invalid" in note for note in view["diagnostics"])
    with pytest.raises(ValueError):
        register(prepared)
    with pytest.raises(ValueError):
        store.withdraw(value["repair_id"], "operator", "stop", 1)
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff',
    b'[]', b'{', b'[' * 2000 + b']' * 2000, b' ' * (MAX_BYTES + 1)])
def test_strict_json_rejects_damage_duplicate_fields_and_decoder_pressure(raw):
    with pytest.raises(ValueError):
        decode(raw)


def test_capacity_does_not_delete_files_or_leave_new_request_locks(prepared):
    root = store.request_root()
    root.mkdir()
    for number in range(MAX_REQUESTS):
        (root / f"foreign-{number}.json").write_text("{}")
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(ValueError, match="capacity"):
        register(prepared)
    assert {path.name: path.read_bytes() for path in root.glob("*.json")} == before
    assert not store.request_path(request_key(prepared[3])).with_suffix(".json.lock").exists()


def test_symlink_state_root_and_nonregular_input_fail_closed(prepared, tmp_path):
    source, _, _, manifest = prepared
    outside = tmp_path / "outside"
    outside.mkdir()
    store.request_root().symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        register(prepared)
    assert list(outside.iterdir()) == []
    file = Path(source["data_dir"]) / "fifo"
    os.mkfifo(file)
    with pytest.raises(ValueError, match="regular"):
        read_manifest(source, str(file))
    with pytest.raises(ValueError, match="outside registered"):
        read_manifest(source, str(outside / "request.json"))
    assert validate_manifest(manifest) == manifest


def test_source_goal_and_checkpoint_locks_prevent_registration_races(prepared):
    source, goal, _, _ = prepared
    from pbatch.task_sources import catalog_path
    targets = (catalog_path().with_suffix(""), task_goal_store.goal_path(goal["goal_key"]),
               run_path(source, prepared[2]["run_id"]))
    for path in targets:
        with locked(path):
            with pytest.raises(BlockingIOError):
                register(prepared)
    assert not list(store.request_root().glob("*.json"))


def test_parallel_withdraw_writers_preserve_single_revision_and_audit(prepared):
    value = register(prepared)
    def stopping(label):
        try:
            return store.withdraw(value["repair_id"], label, "stop request", 1)
        except (ValueError, BlockingIOError):
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(stopping, ["one", "two"]))
    assert sum(result is not None for result in results) == 1
    final = store.load_request(value["repair_id"])
    assert final["version"] == 2 and len(final["audit"]) == 2


def test_rechecks_original_pin_inside_cooperative_locks(prepared, monkeypatch):
    source, _, checkpoint, _ = prepared
    initial = store.check_binding
    calls = []
    def changing(source_value, manifest):
        result = initial(source_value, manifest)
        calls.append(True)
        if len(calls) == 1:
            checkpoint["name"] = "changed between checks"
            atomic_write(run_path(source, checkpoint["run_id"]), checkpoint)
        return result
    monkeypatch.setattr(store, "check_binding", changing)
    with pytest.raises(ValueError, match="checkpoint changed"):
        register(prepared)
    assert list(store.request_root().glob("*.json")) == []


def test_manifest_replacement_or_change_during_read_refuses(prepared, monkeypatch):
    from pbatch import task_repair_binding
    source, _, _, manifest = prepared
    path = Path(source["data_dir"]) / "request.json"
    path.write_bytes(encoded(manifest))
    initial = task_repair_binding._stat
    changed = []
    def changing(stat):
        if not changed:
            path.write_bytes(encoded(manifest))  # identical bytes, changed file metadata
            changed.append(True)
        return initial(stat)
    monkeypatch.setattr(task_repair_binding, "_stat", changing)
    with pytest.raises(ValueError, match="changed during"):
        read_manifest(source, str(path))


def test_declared_deadline_is_rechecked_after_candidate_projection(prepared, monkeypatch):
    from types import SimpleNamespace
    from pbatch import task_repair_binding
    clock = iter([1_000_000_000, 2_000_000_000])
    monkeypatch.setattr(task_repair_binding, "time", SimpleNamespace(time=lambda: next(clock)))
    with pytest.raises(ValueError, match="expired during pin checks"):
        register(prepared)
    assert not store.request_root().exists()


def test_maximum_unicode_audit_preserves_withdrawal_room(prepared):
    value = register(prepared, by="😀" * 128, reason="😀" * 1024)
    withdrawn = store.withdraw(value["repair_id"], "😀" * 128, "😀" * 1024, 1)
    assert len(encoded(withdrawn)) < MAX_BYTES
    assert len(withdrawn["audit"]) == 2


def test_projection_does_not_write_or_reopen_actual_checkpoint(prepared, monkeypatch):
    from pbatch import task_repair_binding, task_goal_view
    source, _, _, _ = prepared
    value = register(prepared)
    path = store.request_path(value["repair_id"])
    before = path.read_bytes()
    reads = []
    initial = task_repair_binding._open_regular
    def bounded(path):
        reads.append(path)
        return initial(path)
    monkeypatch.setattr(task_repair_binding, "_open_regular", bounded)
    # Existing goal view may load its bound checkpoint; repair projection adds no such read.
    monkeypatch.setattr(task_goal_view, "load_run", lambda *_: None)
    assert view_of(value, source)["status"] == "durable_checkpoint_header_unavailable_in_goal_view"
    assert reads == [path]
    assert path.read_bytes() == before


def test_cli_consent_is_checked_before_opening_manifest(prepared, monkeypatch, capsys):
    from pbatch import task_repair_cli
    def forbidden(*args):
        pytest.fail("manifest cannot be read before explicit consent")
    monkeypatch.setattr(task_repair_cli, "read_manifest", forbidden)
    assert ops_main(["repairs", "register", "--source", prepared[0]["source_id"], "--file", "/arbitrary",
                     "--by", "operator", "--reason", "inspect"]) == 2
    assert "--confirm" in capsys.readouterr().out
    assert not store.request_root().exists()


def test_cli_register_show_list_withdraw_and_zero_executor_calls(prepared, monkeypatch, capsys):
    source, _, checkpoint, manifest = prepared
    file = Path(source["data_dir"]) / "request.json"
    file.write_bytes(encoded(manifest))
    original = run_path(source, checkpoint["run_id"]).read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("request metadata cannot call an executor or reset budgets")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert ops_main(["repairs", "register", "--source", source["source_id"], "--file", str(file),
                     "--confirm", "--by", "operator", "--reason", "inspect"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["grants_execution"] is False
    assert ops_main(["repairs", "show", created["repair_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["execution_authority"] == "not_granted"
    assert ops_main(["repairs", "list", "--source", source["source_id"], "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["repairs"]) == 1
    assert ops_main(["repairs", "withdraw", created["repair_id"], "--if-version", "1", "--by", "operator", "--reason", "stop"]) == 0
    capsys.readouterr()
    assert run_path(source, checkpoint["run_id"]).read_bytes() == original
