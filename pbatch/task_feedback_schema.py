"""Closed local synthetic feedback metadata. Quoted case text is never runnable policy."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .task_goal_schema import text
from .task_retention_schema import instant

FEEDBACK_PATTERN = re.compile(r"fd-[a-f0-9]{32}\Z")
SOURCE_PATTERN = re.compile(r"src-[a-f0-9]{24}\Z")
DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
MAX_INPUT_BYTES = 64 * 1024
MAX_TEXT_BYTES = 16 * 1024
MAX_METADATA_BYTES = 16 * 1024
WITHDRAW_RESERVE = 4096
MAX_DRAFTS = 32
_CASE_FIELDS = {"schema_version", "case_type", "case_id", "summary", "inputs", "expected", "observed", "hypothesis"}


def fields(value, expected: set) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid feedback fields; commands, publication and execution grants are not supported")


def matches(pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def digest(value) -> str:
    return hashlib.sha256(encoded(value, MAX_INPUT_BYTES)).hexdigest()


def feedback_key(source_id: str, case_id: str) -> str:
    return "fd-" + digest([source_id, case_id])[:32]


def content_digest(value: dict) -> str:
    """All closed case fields except the label ID; text/order is never normalized."""
    validate_case(value)
    return digest({key: item for key, item in value.items() if key != "case_id"})


def _schema(value) -> None:
    if type(value) is not int or value != 1:
        raise ValueError("unsupported feedback schema")


def _inputs(value) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("feedback needs 1-8 explicit synthetic fragments")
    names, texts = set(), []
    for item in value:
        fields(item, {"name", "content"})
        if not matches(NAME_PATTERN, item["name"]) or item["name"] in names:
            raise ValueError("feedback fragment names must be unique simple names, never paths")
        if not isinstance(item["content"], str) or len(item["content"]) > 4096:
            raise ValueError("feedback content unsupported or over budget; nothing was truncated")
        names.add(item["name"])
        texts.append(item["content"])
    return texts


def validate_case(value: dict) -> dict:
    fields(value, _CASE_FIELDS)
    _schema(value["schema_version"])
    if value["case_type"] != "operator_declared_synthetic" or not matches(NAME_PATTERN, value["case_id"]):
        raise ValueError("explicit synthetic case declaration and simple case ID are required")
    texts = _inputs(value["inputs"])
    for key in ("summary", "expected", "observed", "hypothesis"):
        if not text(value[key], 4096):
            raise ValueError("feedback summary, expected, observed and hypothesis need bounded quoted text")
        texts.append(value[key])
    if sum(len(item.encode("utf-8")) for item in texts) > MAX_TEXT_BYTES:
        raise ValueError("feedback case text byte budget exhausted; no fragments were omitted")
    encoded(value, MAX_INPUT_BYTES)
    return value


def validate_state(value: dict) -> dict:
    expected = {"schema_version", "feedback_id", "source_id", "source_digest", "case_id", "case_sha256",
        "input", "summary", "created_at", "version", "state", "audit"}
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in {1, 2}:
        raise ValueError("unsupported feedback metadata schema")
    if value["schema_version"] == 2:
        expected.add("case_content_sha256")
        if not matches(DIGEST_PATTERN, value.get("case_content_sha256")):
            raise ValueError("invalid feedback content identity digest")
    fields(value, expected)
    if not matches(SOURCE_PATTERN, value["source_id"]) or not matches(NAME_PATTERN, value["case_id"]):
        raise ValueError("invalid feedback source/case identity")
    if value["feedback_id"] != feedback_key(value["source_id"], value["case_id"]):
        raise ValueError("feedback identity mismatch")
    if any(not matches(DIGEST_PATTERN, value[key]) for key in ("source_digest", "case_sha256")):
        raise ValueError("invalid feedback content/source digest")
    _input_proof(value["input"])
    if not isinstance(value["summary"], str) or len(value["summary"]) > 256:
        raise ValueError("invalid bounded feedback summary")
    _audit(value)
    state_bytes(value)
    return value


def _input_proof(value: dict) -> None:
    fields(value, {"path", "bytes", "sha256"})
    if (not isinstance(value["path"], str) or len(value["path"]) > 2048 or
            not Path(value["path"]).is_absolute() or str(Path(value["path"])) != value["path"]):
        raise ValueError("feedback input proof needs a canonical absolute path")
    if (type(value["bytes"]) is not int or not 0 < value["bytes"] <= MAX_INPUT_BYTES or
            not matches(DIGEST_PATTERN, value["sha256"])):
        raise ValueError("invalid feedback input byte/hash proof")


def _audit(value: dict) -> None:
    if type(value["version"]) is not int or value["version"] not in {1, 2}:
        raise ValueError("invalid feedback revision")
    if value["state"] != ("draft" if value["version"] == 1 else "withdrawn"):
        raise ValueError("feedback state/revision mismatch; withdrawal is permanent")
    if not isinstance(value["audit"], list) or len(value["audit"]) != value["version"]:
        raise ValueError("feedback audit must be complete")
    previous = instant(value["created_at"])
    for number, row in enumerate(value["audit"], 1):
        current = _audit_row(row, number)
        if current < previous or (number == 1 and row["at"] != value["created_at"]):
            raise ValueError("feedback audit time mismatch")
        previous = current


def _audit_row(row: dict, number: int) -> float:
    fields(row, {"version", "action", "by", "reason", "at"})
    if type(row["version"]) is not int or row["version"] != number:
        raise ValueError("invalid feedback audit sequence")
    if row["action"] != ("drafted" if number == 1 else "withdrawn"):
        raise ValueError("invalid feedback audit action")
    if not text(row["by"], 128) or not text(row["reason"], 256):
        raise ValueError("feedback actor label and reason are required")
    return instant(row["at"])


def encoded(value, maximum: int) -> bytes:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError("feedback JSON is unsupported") from exc
    if len(raw) > maximum:
        raise ValueError("feedback byte budget exhausted; no history was discarded")
    return raw


def state_bytes(value: dict) -> bytes:
    return encoded(value, MAX_METADATA_BYTES - (WITHDRAW_RESERVE if value["state"] == "draft" else 0))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate feedback JSON field")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("nonfinite feedback JSON")


def decode(raw: bytes, maximum: int) -> dict:
    if len(raw) > maximum:
        raise ValueError("feedback input byte budget exceeded")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if not isinstance(value, dict):
            raise ValueError("feedback JSON must be an object")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid strict feedback JSON") from exc
