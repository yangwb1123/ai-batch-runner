"""Fail-closed metadata schema. Goal text and attestations grant no execution."""
from __future__ import annotations

import hashlib
import re

from .task_state_io import encode

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
GOAL_PATTERN = re.compile(r"g-[a-f0-9]{32}\Z")
MAX_CRITERIA = 32
MAX_HISTORY = 128


def text(value, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def digest(contract: dict, revision: int) -> str:
    return hashlib.sha256(encode({"contract": contract, "revision": revision})).hexdigest()


def goal_key(source_id: str, logical_id: str) -> str:
    return "g-" + hashlib.sha256(encode({"source": source_id, "goal": logical_id})).hexdigest()[:32]


def validate_contract(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "goal_id", "statement", "criteria"}:
        raise ValueError("invalid goal contract fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported goal contract schema")
    if not isinstance(value["goal_id"], str) or not ID_PATTERN.fullmatch(value["goal_id"]):
        raise ValueError("invalid logical goal identity")
    if not text(value["statement"], 4096):
        raise ValueError("goal statement must contain 1 through 4096 characters")
    criteria = value["criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= MAX_CRITERIA:
        raise ValueError("goal needs 1 through 32 acceptance criteria")
    names = [_criterion_id(criterion) for criterion in criteria]
    if len(set(names)) != len(names):
        raise ValueError("duplicate acceptance criterion")
    encode(value)
    return value


def _criterion_id(criterion: dict) -> str:
    if not isinstance(criterion, dict) or set(criterion) != {"id", "description"}:
        raise ValueError("invalid criterion fields")
    if not isinstance(criterion["id"], str) or not ID_PATTERN.fullmatch(criterion["id"]):
        raise ValueError("invalid criterion identity")
    if not text(criterion["description"], 1024):
        raise ValueError("invalid criterion description")
    return criterion["id"]


def validate_state(value: dict) -> dict:
    fields = {"schema_version", "goal_key", "source_id", "version", "contract_revision",
              "contract", "contract_digest", "bindings", "attestations", "history"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid goal state fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported goal state schema")
    validate_contract(value["contract"])
    if not isinstance(value["source_id"], str) or not re.fullmatch(r"src-[a-f0-9]{24}", value["source_id"]):
        raise ValueError("invalid goal source identity")
    if value["goal_key"] != goal_key(value["source_id"], value["contract"]["goal_id"]):
        raise ValueError("goal source/identity mismatch")
    for field in ("version", "contract_revision"):
        if type(value[field]) is not int or value[field] < 1:
            raise ValueError("invalid goal revision")
    if value["contract_revision"] > value["version"]:
        raise ValueError("invalid contract revision order")
    if value["contract_digest"] != digest(value["contract"], value["contract_revision"]):
        raise ValueError("goal contract digest mismatch")
    _validate_collections(value)
    return value


def _validate_collections(value: dict) -> None:
    for field, maximum in (("bindings", 64), ("attestations", MAX_HISTORY), ("history", MAX_HISTORY)):
        if not isinstance(value[field], list) or len(value[field]) > maximum:
            raise ValueError("goal state collection exceeds its budget")
    if len(value["history"]) != value["version"]:
        raise ValueError("incomplete goal revision history")
    if not all(isinstance(item, dict) and type(item.get("version")) is int
               and item["version"] == index
               for index, item in enumerate(value["history"], 1)):
        raise ValueError("invalid goal history sequence")
    for binding in value["bindings"]:
        if not isinstance(binding, dict) or set(binding) != {"run_id"}:
            raise ValueError("invalid goal run binding")
        if not isinstance(binding["run_id"], str) or not re.fullmatch(r"pl-[a-f0-9]{32}", binding["run_id"]):
            raise ValueError("invalid bound run identity")
    for receipt in value["attestations"]:
        _validate_receipt(receipt)


def _validate_receipt(receipt: dict) -> None:
    keys = {"criterion_id", "decision", "contract_digest", "by", "reason", "at", "evidence"}
    if not isinstance(receipt, dict) or set(receipt) != keys:
        raise ValueError("invalid goal attestation fields")
    if not isinstance(receipt["decision"], str) or receipt["decision"] not in {"accepted", "rejected"}:
        raise ValueError("invalid acceptance decision")
    if not all(text(receipt[key], maximum) for key, maximum in
               (("criterion_id", 64), ("by", 128), ("reason", 1024), ("at", 64))):
        raise ValueError("incomplete goal attestation")
    if not isinstance(receipt["contract_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", receipt["contract_digest"]):
        raise ValueError("invalid attestation revision")
    _validate_evidence(receipt["evidence"], receipt["decision"])


def _validate_evidence(evidence: dict | None, decision: str) -> None:
    if evidence is None and decision == "rejected":
        return
    if not isinstance(evidence, dict) or set(evidence) != {"path", "bytes", "sha256"}:
        raise ValueError("acceptance needs bound file evidence")
    if not text(evidence["path"], 4096) or not isinstance(evidence["sha256"], str):
        raise ValueError("invalid evidence reference")
    if not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"]):
        raise ValueError("invalid evidence digest")
    if type(evidence["bytes"]) is not int or not 1 <= evidence["bytes"] <= 16 * 1024 * 1024:
        raise ValueError("invalid evidence size")
