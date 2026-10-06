"""Explicit local goal metadata operations with optimistic revisions.

Evidence is a full-file binding to an operator's claim, not proof that its
contents, tests, or claimed reviewer identity are true. No shell is executed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .task_center_io import checked_path, _open_regular
from .task_goal_schema import (MAX_HISTORY, GOAL_PATTERN, digest, goal_key,
                               text, validate_contract, validate_state)
from .task_lifecycle import load_run, stamp
from .task_sources import discover_sources
from .task_state_io import atomic_write, encode, load, locked
from .user_dirs import pbatch_runtime_home

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_GOALS = 128


def goal_path(key: str) -> Path:
    if not isinstance(key, str) or not GOAL_PATTERN.fullmatch(key):
        raise ValueError("invalid goal identity")
    return pbatch_runtime_home() / "ops" / "goals" / (key + ".json")


def resolve_source(identity: str) -> dict:
    rows, _ = discover_sources()
    matches = [row for row in rows if identity in {row["source_id"], row["name"]}]
    if len(matches) != 1:
        raise ValueError("goal needs one unambiguous registered/discovered source")
    return matches[0]


def load_goal(key: str) -> dict:
    value = load(goal_path(key))
    if value is None:
        raise ValueError("goal not found")
    validate_state(value)
    if value["goal_key"] != key:
        raise ValueError("goal file identity mismatch")
    return value


def _actor(by: str, reason: str) -> None:
    if not text(by, 128) or not text(reason, 1024):
        raise ValueError("explicit actor label and reason are required")


def _version(value: dict | None, expected: int | None) -> None:
    current = value["version"] if value else 0
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("expected version must be a nonnegative integer")
    if (value is not None and expected is None) or (expected is not None and expected != current):
        raise ValueError(f"goal revision conflict; current version is {current}")


def _history(value: dict, kind: str, by: str, reason: str) -> None:
    if len(value["history"]) >= MAX_HISTORY:
        raise ValueError("goal history budget exhausted; history will not be discarded")
    value["version"] += 1
    item = {"version": value["version"], "kind": kind, "by": by, "reason": reason,
            "at": stamp(), "contract_digest": value["contract_digest"]}
    if kind == "defined":
        item["contract"] = value["contract"]
    value["history"].append(item)


def define(source_id: str, contract: dict, by: str, reason: str,
           expected: int | None = None) -> dict:
    validate_contract(contract)
    _actor(by, reason)
    source = resolve_source(source_id)
    key = goal_key(source["source_id"], contract["goal_id"])
    encode(contract)
    with locked(goal_path(key)) as path:
        previous = load(path)
        if previous is not None:
            validate_state(previous)
            if previous["goal_key"] != key or previous["source_id"] != source["source_id"]:
                raise ValueError("goal file/source mismatch")
        _version(previous, expected)
        if previous is None:
            _capacity(path.parent)
            value = {"schema_version": 1, "goal_key": key, "source_id": source["source_id"],
                "version": 0, "contract_revision": 0, "contract": contract,
                "contract_digest": "", "bindings": [], "attestations": [], "history": []}
        else:
            value = previous
        value["contract"] = contract
        value["contract_revision"] += 1
        value["contract_digest"] = digest(contract, value["contract_revision"])
        _history(value, "defined", by, reason)
        validate_state(value)
        atomic_write(path, value)
    return value


def _capacity(directory: Path) -> None:
    for index, _ in enumerate(directory.iterdir()):
        if index >= MAX_GOALS * 2:
            raise ValueError("goal storage capacity reached; no history was deleted")


def bind(key: str, run_id: str, by: str, reason: str, expected: int) -> dict:
    _actor(by, reason)
    initial = load_goal(key)
    _version(initial, expected)
    source = resolve_source(initial["source_id"])
    if load_run(source, run_id) is None:
        raise ValueError("pipeline run checkpoint not found; binding was not invented")
    with locked(goal_path(key)) as path:
        value = load_goal(key)
        _version(value, expected)
        resolve_source(value["source_id"])
        if {"run_id": run_id} not in value["bindings"]:
            value["bindings"].append({"run_id": run_id})
            _history(value, "run_bound", by, reason)
            validate_state(value)
            atomic_write(path, value)
    return value


def evidence_reference(source: dict, file: str, maximum: int = MAX_EVIDENCE_BYTES,
                       on_read=None) -> dict:
    path = Path(file)
    if not path.is_absolute():
        if not source["workspace"]:
            raise ValueError("relative evidence needs an explicitly registered workspace")
        path = Path(source["workspace"]) / path
    path = checked_path(path)
    roots = [Path(source["data_dir"])] + ([Path(source["workspace"])] if source["workspace"] else [])
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("evidence is outside registered roots")
    digest_value = hashlib.sha256()
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > maximum:
            raise ValueError("evidence verification byte budget exceeded")
        total = 0
        while chunk := handle.read(min(65536, maximum - total + 1)):
            total += len(chunk)
            if on_read is not None:
                on_read(len(chunk))
            if total > maximum:
                raise ValueError("evidence grew beyond its byte budget")
            digest_value.update(chunk)
        after = os.fstat(handle.fileno())
        if _stat_key(before) != _stat_key(after) or total != after.st_size:
            raise ValueError("evidence changed during verification")
        if _stat_key(path.stat()) != _stat_key(after):
            raise ValueError("evidence path changed during verification")
    return {"path": str(path), "bytes": total, "sha256": digest_value.hexdigest()}


def _stat_key(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def attest(key: str, criterion: str, decision: str, evidence: str,
           by: str, reason: str, expected: int) -> dict:
    _actor(by, reason)
    if decision not in {"accepted", "rejected"}:
        raise ValueError("invalid acceptance decision")
    _version(load_goal(key), expected)
    with locked(goal_path(key)) as path:
        value = load_goal(key)
        _version(value, expected)
        if criterion not in {item["id"] for item in value["contract"]["criteria"]}:
            raise ValueError("criterion is not in the current goal contract")
        source = resolve_source(value["source_id"])
        if decision == "accepted" and not evidence:
            raise ValueError("acceptance needs a file evidence reference")
        reference = evidence_reference(source, evidence) if evidence else None
        if reference is not None and not reference["bytes"]:
            raise ValueError("empty evidence cannot support an acceptance claim")
        receipt = {"criterion_id": criterion, "decision": decision, "evidence": reference,
                   "by": by, "reason": reason, "at": stamp(),
                   "contract_digest": value["contract_digest"]}
        value["attestations"].append(receipt)
        _history(value, "criterion_attested", by, reason)
        validate_state(value)
        atomic_write(path, value)
    return value
