"""Closed repair-request metadata schema. Declared limits are not budget grants."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime

from .task_center_adapters import identity
from .task_goal_schema import GOAL_PATTERN, text
from .task_lifecycle import RUN_PATTERN

REQUEST_PATTERN = re.compile(r"rq-[a-f0-9]{32}\Z")
SOURCE_PATTERN = re.compile(r"src-[a-f0-9]{24}\Z")
STAGE_PATTERN = re.compile(r"st-[a-f0-9]{32}\Z")
DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
MAX_BYTES = 16 * 1024
MAX_REQUESTS = 64
MAX_ATTEMPTS = 3
_MANIFEST_FIELDS = {"schema_version", "source_id", "task_id", "run_id", "goal_id", "goal_version",
                    "contract_digest", "checkpoint", "stage_key", "limits"}


def fields(value, expected: set) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid repair request fields")


def integer(value, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def matches(pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def deadline(value) -> float:
    if not text(value, 64):
        raise ValueError("repair deadline must be an explicit timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("repair deadline must be an explicit timezone-aware timestamp") from exc


def fingerprint(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def request_key(value: dict) -> str:
    anchor = {key: value[key] for key in ("source_id", "run_id", "goal_id", "contract_digest", "checkpoint", "stage_key")}
    return "rq-" + fingerprint(anchor)[:32]


def validate_manifest(value: dict) -> dict:
    fields(value, _MANIFEST_FIELDS)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported repair request schema")
    for pattern, key in ((SOURCE_PATTERN, "source_id"), (RUN_PATTERN, "run_id"),
                         (GOAL_PATTERN, "goal_id"), (STAGE_PATTERN, "stage_key"), (DIGEST_PATTERN, "contract_digest")):
        if not matches(pattern, value[key]):
            raise ValueError("invalid repair " + key)
    if value["task_id"] != "tc-" + identity([value["source_id"], "pipeline", value["run_id"]]):
        raise ValueError("repair task/source/run identity mismatch")
    if not integer(value["goal_version"], 128):
        raise ValueError("invalid repair goal revision")
    fields(value["checkpoint"], {"sequence", "sha256"})
    if (not integer(value["checkpoint"]["sequence"], 10**9)
            or not matches(DIGEST_PATTERN, value["checkpoint"]["sha256"])):
        raise ValueError("invalid repair checkpoint pin")
    validate_limits(value["limits"])
    return value


def validate_limits(value: dict) -> None:
    fields(value, {"requested_deadline_at", "max_attempts", "max_elapsed_seconds", "max_total_tokens", "max_cost"})
    deadline(value["requested_deadline_at"])
    for key, maximum in (("max_attempts", MAX_ATTEMPTS), ("max_elapsed_seconds", 86400), ("max_total_tokens", 10**9)):
        if not integer(value[key], maximum):
            raise ValueError("repair needs an explicit bounded positive " + key)
    cost = value["max_cost"]
    fields(cost, {"amount", "currency"})
    amount = cost["amount"]
    if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 < amount <= 10**6:
        raise ValueError("repair needs an explicit positive finite cost cap")
    if not isinstance(cost["currency"], str) or not re.fullmatch(r"[A-Z]{3}", cost["currency"]):
        raise ValueError("repair cost cap requires an explicit currency label")


def validate_state(value: dict) -> dict:
    fields(value, {"schema_version", "repair_id", "manifest", "manifest_digest", "version", "state", "receipt", "audit"})
    validate_manifest(value["manifest"])
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported repair state schema")
    if value["repair_id"] != request_key(value["manifest"]) or value["manifest_digest"] != fingerprint(value["manifest"]):
        raise ValueError("repair identity/manifest digest mismatch")
    if value["state"] not in {"registered", "withdrawn"} or not integer(value["version"], 2):
        raise ValueError("invalid repair state/revision")
    _receipt(value["receipt"])
    _audit(value)
    encoded(value)
    return value


def _receipt(value: dict) -> None:
    fields(value, {"checked_at", "stage_label", "declared_gate", "plan_sha256"})
    deadline(value["checked_at"])
    if (not isinstance(value["stage_label"], str) or len(value["stage_label"]) > 160
            or type(value["declared_gate"]) is not bool or not matches(DIGEST_PATTERN, value["plan_sha256"])):
        raise ValueError("invalid repair binding receipt")


def _audit(value: dict) -> None:
    audit = value["audit"]
    if not isinstance(audit, list) or len(audit) != value["version"]:
        raise ValueError("incomplete repair audit")
    if value["state"] != ("registered" if value["version"] == 1 else "withdrawn"):
        raise ValueError("repair state/audit mismatch")
    previous = deadline(value["receipt"]["checked_at"])
    for index, row in enumerate(audit, 1):
        fields(row, {"version", "action", "by", "reason", "at"})
        if type(row["version"]) is not int or row["version"] != index:
            raise ValueError("invalid repair audit sequence")
        if row["action"] != ("registered" if index == 1 else "withdrawn"):
            raise ValueError("invalid repair audit action")
        if not text(row["by"], 128) or not text(row["reason"], 1024):
            raise ValueError("explicit repair actor label and reason are required")
        current = deadline(row["at"])
        if current < previous:
            raise ValueError("repair audit time moved backwards")
        previous = current
    if deadline(audit[0]["at"]) >= deadline(value["manifest"]["limits"]["requested_deadline_at"]):
        raise ValueError("repair registration is outside its declared deadline")


def encoded(value: dict) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("repair metadata byte budget exceeded; no history was discarded")
    return raw


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate repair JSON field")
        value[key] = item
    return value


def _nonfinite(_value):
    raise ValueError("nonfinite repair JSON")


def decode(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise ValueError("repair input byte budget exceeded")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_nonfinite)
        if not isinstance(value, dict):
            raise ValueError("repair JSON must be an object")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid strict repair JSON: " + str(exc)) from exc
