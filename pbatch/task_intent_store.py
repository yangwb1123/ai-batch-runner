"""Explicit import/revocation of private immutable prompt excerpts; never execute them."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .task_center_io import _open_regular
from .task_goal_store import (_actor, _stat_key, _version, evidence_reference,
                              load_goal, resolve_source)
from .task_intent_schema import (INTENT_PATTERN, MAX_IMPORTS, fingerprint, intent_key,
                                validate_export, validate_messages, validate_state)
from .task_lifecycle import stamp
from .task_state_io import MAX_STATE_BYTES, atomic_write, encode, load, locked
from .user_dirs import pbatch_runtime_home
from . import task_native_history as native

MAX_INTENTS = 32


def intent_path(key: str) -> Path:
    if not isinstance(key, str) or not INTENT_PATTERN.fullmatch(key):
        raise ValueError("invalid prompt history identity")
    return pbatch_runtime_home() / "ops" / "intents" / (key + ".json")


def read_history(key: str) -> dict:
    value = load(intent_path(key))
    if value is None:
        raise ValueError("prompt history not found")
    validate_state(value)
    if value["intent_id"] != key:
        raise ValueError("prompt history path/identity mismatch")
    return value


def _read_bytes(source: dict, file: str, maximum: int) -> tuple[bytes, dict]:
    proof = evidence_reference(source, file, maximum)
    with _open_regular(Path(proof["path"])) as handle:
        before = os.fstat(handle.fileno())
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    if (_stat_key(before) != _stat_key(after) or _stat_key(Path(proof["path"]).stat()) != _stat_key(after)
            or len(raw) != proof["bytes"] or hashlib.sha256(raw).hexdigest() != proof["sha256"]):
        raise ValueError("prompt export changed during authorized import")
    return raw, proof


def _read_export(source: dict, file: str) -> tuple[dict, dict]:
    raw, proof = _read_bytes(source, file, 128 * 1024)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid prompt export JSON") from exc
    return validate_export(value), proof


def _read_input(source: dict, file: str, goal: str, format: str, channel: str, leaf: str) -> tuple:
    if format == "normalized":
        manifest, proof = _read_export(source, file)
        return manifest, proof, None, None
    raw, proof = _read_bytes(source, file, native.MAX_NATIVE_BYTES)
    manifest, adapter = native.convert(raw, source, goal, format, channel, leaf)
    return manifest, proof, adapter, raw


def import_history(source_id: str, goal_id: str, file: str, by: str, reason: str,
                   expected: int | None = None, *, format: str = "normalized",
                   channel: str = "", leaf: str = "") -> dict:
    began = time.perf_counter()
    _actor(by, reason)
    native.options(format, channel, leaf)
    source = resolve_source(source_id)
    goal = load_goal(goal_id)
    if goal["source_id"] != source["source_id"]:
        raise ValueError("prompt history belongs to a different goal source")
    manifest, proof, adapter, raw = _read_input(source, file, goal_id, format, channel, leaf)
    if manifest["goal_id"] != goal_id:
        raise ValueError("export goal does not match authorized goal")
    key = intent_key(source["source_id"], goal_id, manifest["channel"])
    with locked(intent_path(key)) as path:
        value = load(path)
        if value is not None:
            validate_state(value)
            if value["intent_id"] != key:
                raise ValueError("history identity mismatch; no overwrite")
        _version(value, expected)
        if value is None:
            _capacity(path.parent)
            value = _new_history(goal, key, manifest)
        else:
            native.check_continuation(value, adapter, raw, manifest["messages"])
        new = _merge(value, manifest)
        value.update(authorized=True, contract_digest=goal["contract_digest"],
                     messages_digest=fingerprint(value["messages"]))
        _append_import(value, manifest, proof, adapter, new, began)
        _audit(value, "imported", by, reason)
        validate_state(value)
        if len(encode(value)) > MAX_STATE_BYTES - 4096:
            raise ValueError("history byte budget reserves room for revocation")
        _check_goal(goal_id, goal["contract_digest"])
        atomic_write(path, value)
    return value


def _new_history(goal: dict, key: str, manifest: dict) -> dict:
    return {"schema_version": 1, "intent_id": key, "source_id": goal["source_id"],
            "goal_id": goal["goal_key"], "channel": manifest["channel"], "version": 0,
            "contract_digest": goal["contract_digest"], "authorized": True,
            "history_complete": manifest["history_complete"], "messages": [],
            "messages_digest": fingerprint([]), "imports": [], "audit": []}


def _append_import(value: dict, manifest: dict, proof: dict, adapter: dict | None,
                   new: list, began: float) -> None:
    record = {"evidence": proof, "message_ids": [m["id"] for m in manifest["messages"]],
              "new_message_ids": new, "at": stamp(), "analysis_seconds": time.perf_counter() - began}
    if adapter is not None:
        record["adapter"] = adapter
    value["imports"].append(record)


def _merge(value: dict, manifest: dict) -> list[str]:
    known = {message["id"]: message for message in value["messages"]}
    new = []
    for message in manifest["messages"]:
        previous = known.get(message["id"])
        if previous is not None and fingerprint(previous) != fingerprint(message):
            raise ValueError("existing history message was rewritten; use a separate channel")
        if previous is None:
            value["messages"].append(message)
            known[message["id"]] = message
            new.append(message["id"])
    validate_messages(value["messages"])
    if manifest["history_complete"]:
        if manifest["messages"] != value["messages"]:
            raise ValueError("complete coverage needs the entire retained chronological history")
        value["history_complete"] = True
    else:
        value["history_complete"] = False
    return new


def _check_goal(key: str, digest: str) -> None:
    if load_goal(key)["contract_digest"] != digest:
        raise ValueError("goal changed during history authorization; retry explicitly")


def _audit(value: dict, action: str, by: str, reason: str) -> None:
    maximum = MAX_IMPORTS - 1 if action == "imported" else MAX_IMPORTS
    if len(value["audit"]) >= maximum:
        raise ValueError("history audit budget exhausted; no history was discarded")
    value["version"] += 1
    value["audit"].append({"version": value["version"], "action": action,
                           "by": by, "reason": reason, "at": stamp()})


def _capacity(directory: Path) -> None:
    for index, _ in enumerate(directory.iterdir()):
        if index >= MAX_INTENTS * 2:
            raise ValueError("prompt history capacity reached; no private history was deleted")


def revoke(key: str, by: str, reason: str, expected: int) -> dict:
    _actor(by, reason)
    _version(read_history(key), expected)
    with locked(intent_path(key)) as path:
        value = read_history(key)
        _version(value, expected)
        if not value["authorized"]:
            return value
        value["authorized"] = False
        _audit(value, "revoked", by, reason)
        validate_state(value)
        atomic_write(path, value)
    return value
