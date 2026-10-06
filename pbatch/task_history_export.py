"""Explicit local range export and replay; no directory scan, import grant or upload."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from .task_center_io import checked_path
from .task_goal_store import _actor, _version, load_goal, resolve_source
from .task_history_export_schema import check_scope
from .task_history_range import scan, validate_range
from .task_intent_schema import fingerprint, validate_export
from .task_lifecycle import stamp
from .task_native_history import options
from .task_state_io import encode, locked, _sync_directory

MAX_EXPORTS = 64
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,110}\.json\Z")
_NATIVE_FIELDS = ("format", "session_digest", "workspace_digest", "leaf_id", "input_evidence", "selection")


def output_path(source: dict, name: str) -> Path:
    if not isinstance(name, str) or not _FILENAME.fullmatch(name):
        raise ValueError("export output must be a simple .json filename, not a filesystem path")
    return checked_path(Path(source["data_dir"]) / "exports" / "intents" / name)


def export_history(source_id: str, goal_id: str, file: str, format: str, channel: str,
                   leaf: str, first: int, last: int, output: str, by: str, reason: str, goal_version: int) -> dict:
    began = time.perf_counter()
    _actor(by, reason)
    options(format, channel, leaf)
    validate_range(first, last)
    if format == "normalized":
        raise ValueError("range export requires a supported native format")
    source, goal = resolve_source(source_id), load_goal(goal_id)
    _version(goal, goal_version)
    if goal["source_id"] != source["source_id"]:
        raise ValueError("history export belongs to a different goal source")
    path = output_path(source, output)
    messages, report, _ = scan(source, file, format, leaf, first, last)
    origin = {"schema_version": 1, "producer": "bounded-native-range-v1", "source_id": source["source_id"],
              "contract_digest": goal["contract_digest"], **report, "messages_sha256": fingerprint(messages),
              "by": by, "reason": reason, "at": stamp()}
    manifest = {"schema_version": 2, "goal_id": goal_id, "channel": channel,
                "history_complete": False, "messages": messages, "origin": origin}
    validate_export(manifest)
    payload = encode(manifest)
    with locked(path.parent / ".publication"):
        _capacity(path.parent)
        if load_goal(goal_id)["contract_digest"] != goal["contract_digest"]:
            raise ValueError("goal changed during history export; retry explicitly")
        _publish(path, payload)
    return {"path": str(path), "export_sha256": hashlib.sha256(payload).hexdigest(),
            "messages": len(messages), "selection": report["selection"], "history_complete": False,
            "local_compute_seconds": time.perf_counter() - began, "llm_requests": 0, "total_tokens": 0,
            "cost_total": None, "grants_import_or_execution": False}


def _capacity(directory: Path) -> None:
    count = 0
    for index, path in enumerate(directory.iterdir()):
        if index >= MAX_EXPORTS + 4:
            raise ValueError("history export directory capacity exceeded; no files were removed")
        count += path.suffix == ".json"
        if count >= MAX_EXPORTS:
            raise ValueError("history export capacity reached; no files were removed")


def _publish(path: Path, payload: bytes) -> None:
    path = checked_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".history-export-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # exclusive atomic publication; never overwrite existing output
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_import(value: dict, manifest: dict, source: dict, goal: dict, origin_file: str) -> None:
    previous = [row["export_origin"] for row in value["imports"] if "export_origin" in row]
    origin = manifest.get("origin")
    if value["imports"] and bool(previous) != bool(origin):
        raise ValueError("user-range and other history formats cannot share a channel")
    if origin is None:
        if origin_file:
            raise ValueError("--origin-file is only valid for a v2 user-range export")
        return
    if not origin_file:
        raise ValueError("v2 history import needs explicit --origin-file authorization for replay")
    if origin["source_id"] != source["source_id"] or origin["contract_digest"] != goal["contract_digest"]:
        raise ValueError("user-range source/current goal contract does not match authorization")
    if previous:
        check_scope(previous[-1], origin)  # contiguity is relative to the latest imported range
    selected = origin["selection"]
    messages, report, prefixes = scan(source, origin_file, origin["format"], origin["leaf_id"],
        selected["first_user"], selected["last_user"], upto=origin["input_evidence"]["bytes"],
        prefixes=[prior["input_evidence"]["bytes"] for prior in previous])
    _compare_replay(origin, manifest["messages"], messages, report)
    for prior in previous:
        proof = prior["input_evidence"]
        if prefixes.get(proof["bytes"]) != proof["sha256"]:
            raise ValueError("user-range native input rewrote a previously imported byte prefix")


def _compare_replay(origin: dict, expected: list, messages: list, report: dict) -> None:
    if expected != messages:
        raise ValueError("user-range messages were not reproduced from the explicitly authorized native input")
    comparable = {key: report[key] for key in _NATIVE_FIELDS}
    comparable["input_evidence"] = {**comparable["input_evidence"], "path": origin["input_evidence"]["path"]}
    if comparable != {key: origin[key] for key in _NATIVE_FIELDS}:
        raise ValueError("user-range native provenance was not reproduced during import")
