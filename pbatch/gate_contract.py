"""Structured, fail-closed Gate decisions.

Gate artifacts are usually Markdown, so the contract is carried in a
`````json`` block.  Legacy ``VERDICT: PASS|FAIL|REJECT - reason`` lines remain
supported for existing pipelines, but a malformed structured contract never
falls back to a potentially misleading text verdict.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config
from .json_schema import validate as validate_json
from .text_io import read_text_bounded


GATE_SCHEMA_VERSION = 1
GATE_DECISION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "verdict", "reason"],
    "properties": {
        "schema_version": {"const": GATE_SCHEMA_VERSION},
        "verdict": {"enum": ["PASS", "FAIL", "REJECT"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
        "evidence": {
            "type": "array", "minItems": 1, "maxItems": 64,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "validators": {
            "type": "array", "minItems": 1, "maxItems": 64,
            "items": {
                "type": "object", "required": ["name", "status"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "status": {"enum": ["PASS", "WARN", "FAIL", "SKIP"]},
                    "detail": {"type": "string", "maxLength": 1000},
                },
            },
        },
        "findings": {"type": "array", "maxItems": 50, "items": {"type": "object"}},
    },
    "allOf": [{
        "if": {"properties": {"verdict": {"const": "PASS"}}},
        "then": {"required": ["evidence", "validators"]},
    }],
}

_VERDICT_RE = re.compile(
    r"^\s*\*{0,2}VERDICT\s*:\s*\*{0,2}"
    r"(PASS|FAIL|REJECT)\b", re.IGNORECASE | re.MULTILINE)
_FENCE_RE = re.compile(
    r"```(?P<language>[A-Za-z0-9_-]*)[ \t]*\n(?P<body>.*?)```", re.DOTALL)
_EVENT_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"\\]+)"')
_FINDING_FIELDS = ("severity", "file", "line", "problem", "fix_hint")
_TRANSCRIPT_PREFIXES = ("thread.", "turn.", "item.")
_TRANSCRIPT_EVENT_TYPES = frozenset({
    "error", "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed",
})


@dataclass
class GateDecision:
    """Normalized result of one or more Gate artifacts."""

    verdict: Optional[str] = None
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    validators: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    source: str = "none"
    valid: bool = False


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    """Build one JSON object while rejecting every duplicate key."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _load_unique_json(text: str):
    """Decode JSON with recursive object-key uniqueness enforcement."""
    return json.loads(text, object_pairs_hook=_unique_json_object)


def _looks_like_transcript_event(text: str) -> bool:
    return any(value == "error" or value.startswith(_TRANSCRIPT_PREFIXES)
               for value in _EVENT_TYPE_RE.findall(text))


def _has_substantive_reason(value: object) -> bool:
    """A reason must contain human-readable substance, not only markup."""
    return isinstance(value, str) and any(char.isalnum() for char in value)


def unwrap_gate_transcript(text: str) -> tuple[str, str]:
    """Return the final assistant payload from a Codex JSONL transcript.

    Plain Markdown/JSON artifacts pass through unchanged. Once an official
    transcript event is observed, malformed JSONL, provider failure, a
    missing terminal event, or a missing final message fails closed via the
    returned error instead of falling back to regexes over transport data.
    """
    messages, saw_event, malformed, terminal, sequence_error = (
        _scan_gate_transcript(text))
    if not saw_event:
        return text, ""
    if malformed:
        return "", "malformed Gate JSONL transcript"
    if sequence_error:
        return "", sequence_error
    if terminal in ("turn.failed", "error"):
        return "", f"Gate JSONL transcript ended with {terminal}"
    if terminal != "turn.completed":
        return "", "Gate JSONL transcript has no turn.completed event"
    if not messages:
        return "", "Gate JSONL transcript has no completed agent message"
    final_message = messages[-1]
    if not isinstance(final_message, str) or not final_message.strip():
        return "", "Gate JSONL transcript final agent message is empty or non-string"
    return final_message, ""


def _scan_gate_transcript(text: str) -> tuple[list[object], bool, bool, str, str]:
    messages: list[object] = []
    saw_event = malformed = False
    terminal = sequence_error = ""
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if terminal:
            sequence_error = "Gate JSONL transcript has events after terminal event"
            continue
        try:
            value = _load_unique_json(line)
        except (TypeError, ValueError, RecursionError):
            malformed = True
            saw_event = saw_event or _looks_like_transcript_event(line)
            continue
        if not isinstance(value, dict):
            malformed = True
            continue
        event_type = value.get("type")
        if (not isinstance(event_type, str) or not event_type.strip()
                or event_type not in _TRANSCRIPT_EVENT_TYPES):
            malformed = True
            continue
        saw_event = True
        if event_type in ("turn.completed", "turn.failed", "error"):
            terminal = event_type
            continue
        _append_agent_message(value, messages)
    return messages, saw_event, malformed, terminal, sequence_error


def _append_agent_message(value: dict, messages: list[object]) -> None:
    if value.get("type") != "item.completed":
        return
    item = value.get("item")
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return
    # Record every completed assistant message, including an empty or null
    # payload.  Otherwise an earlier PASS could incorrectly become the
    # effective final message when the provider completed with no text.
    messages.append(item.get("text"))


def _bounded_findings(value) -> Optional[list[dict]]:
    if not isinstance(value, list) or not value:
        return None
    safe = []
    for item in value[:50]:
        if isinstance(item, dict):
            safe.append({key: str(item[key])[:2000]
                         for key in _FINDING_FIELDS
                         if key in item and item[key] is not None})
    return safe or None


def _json_objects(text: str):
    """Yield JSON objects from fenced blocks, plus a bare JSON object."""
    found = False
    for match in _FENCE_RE.finditer(text or ""):
        language = match.group("language").lower()
        body = match.group("body").strip()
        if language not in ("", "json") and not body.startswith(("{", "[")):
            continue
        if not body.startswith(("{", "[")):
            continue
        found = True
        try:
            yield _load_unique_json(body), True
        except (TypeError, ValueError, RecursionError):
            yield None, True
    stripped = (text or "").strip()
    if ((stripped.startswith("{") and stripped.endswith("}"))
            or (stripped.startswith("[") and stripped.endswith("]"))):
        found = True
        try:
            yield _load_unique_json(stripped), True
        except (TypeError, ValueError, RecursionError):
            yield None, True
    if not found:
        return


def _is_contract_candidate(value) -> bool:
    return isinstance(value, dict) and (
        "schema_version" in value or "verdict" in value)


def _pass_contract_error(normalized: dict, validators: list[dict]) -> str:
    if normalized["verdict"] != "PASS":
        return ""
    if any(not _has_substantive_reason(item)
           for item in normalized.get("evidence", [])):
        return "structured Gate PASS evidence has no substantive text"
    if any(not _has_substantive_reason(item.get("name"))
           for item in validators):
        return "structured Gate PASS validator name has no substantive text"
    if any(str(item.get("status", "")).upper() in ("FAIL", "REJECT")
           for item in validators):
        return "structured Gate PASS contains a failing validator"
    return ""


def _decision_from_contract(value: dict) -> GateDecision:
    normalized = dict(value)
    if isinstance(normalized.get("verdict"), str):
        normalized["verdict"] = normalized["verdict"].upper()
    errors = validate_json(normalized, GATE_DECISION_SCHEMA)
    if errors:
        return GateDecision(
            reason="structured Gate contract rejected: " + "; ".join(errors[:4]),
            source="structured", valid=False)
    if not _has_substantive_reason(normalized.get("reason")):
        return GateDecision(
            reason="structured Gate reason has no substantive text",
            source="structured", valid=False)
    validators = [dict(item) for item in normalized.get("validators", [])]
    semantic_error = _pass_contract_error(normalized, validators)
    if semantic_error:
        return GateDecision(reason=semantic_error, source="structured", valid=False)
    return GateDecision(
        verdict=normalized["verdict"], reason=normalized["reason"][:4000],
        evidence=[str(item)[:1000] for item in normalized.get("evidence", [])],
        validators=validators[:64],
        findings=_bounded_findings(normalized.get("findings", [])) or [],
        source="structured", valid=True)


def _structured_decision(text: str) -> tuple[Optional[GateDecision], bool]:
    """Return (decision, saw_contract_candidate)."""
    decision = None
    for value, parsed in _json_objects(text):
        if not parsed:
            continue
        if value is None:
            return GateDecision(reason="malformed structured Gate JSON",
                                source="structured", valid=False), True
        if not _is_contract_candidate(value):
            continue
        if decision is not None:
            return GateDecision(
                reason="multiple structured Gate contracts in one artifact",
                source="structured", valid=False), True
        decision = _decision_from_contract(value)
    return decision, decision is not None


def _legacy_decision(text: str) -> GateDecision:
    first_pass = None
    first_rejection = None
    bare = False
    for match in _VERDICT_RE.finditer(text or ""):
        line_end = text.find("\n", match.end())
        rest = text[match.end():line_end if line_end >= 0 else len(text)].strip()
        if not _has_substantive_reason(rest):
            bare = True
            continue
        verdict = match.group(1).upper()
        if verdict in ("FAIL", "REJECT") and first_rejection is None:
            first_rejection = GateDecision(
                verdict=verdict, reason=rest[:4000],
                source="legacy_text", valid=True)
        elif verdict == "PASS" and first_pass is None:
            first_pass = GateDecision(
                verdict="PASS", reason=rest[:4000],
                source="legacy_text", valid=True)
    if bare:
        return GateDecision(reason="bare VERDICT has no reason",
                            source="legacy_text", valid=False)
    if first_rejection:
        return first_rejection
    if first_pass:
        return first_pass
    return GateDecision(reason="no usable Gate verdict", source="none",
                        valid=False)


def parse_gate_text(text: str) -> GateDecision:
    """Parse one artifact, preferring structured JSON over legacy text."""
    payload, transcript_error = unwrap_gate_transcript(text)
    if transcript_error:
        return GateDecision(reason=transcript_error, source="transcript",
                            valid=False)
    structured, saw_candidate = _structured_decision(payload)
    if saw_candidate:
        return structured or GateDecision(
            reason="structured Gate contract is missing", source="structured")
    return _legacy_decision(payload)


def evaluate_gate_artifacts(output_paths: list[str], max_bytes: int = 0) -> GateDecision:
    """Aggregate artifacts; any structured contract error fails closed."""
    decisions = []
    errors = []
    limit = max_bytes or config.OUTPUT_MAX_BYTES
    for raw_path in output_paths:
        path = Path(raw_path)
        if path.is_symlink():
            errors.append(f"{path}: Gate artifact symlinks are not allowed")
            continue
        if not path.is_file():
            errors.append(f"{path}: Gate artifact is missing")
            continue
        try:
            text = read_text_bounded(path, limit, "gate artifact")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        decision = parse_gate_text(text)
        if not decision.valid or not decision.verdict:
            errors.append(f"{path}: {decision.reason}")
        else:
            decisions.append(decision)
    if errors:
        return GateDecision(reason="; ".join(errors)[:4000], source="invalid")
    rejected = next((item for item in decisions
                     if item.verdict in ("FAIL", "REJECT")), None)
    if rejected:
        return rejected
    structured = next((item for item in decisions if item.source == "structured"), None)
    if structured:
        return structured
    if decisions:
        return decisions[0]
    return GateDecision(reason="no usable Gate verdict", source="none")


def gate_verdict(output_paths: list[str]) -> Optional[str]:
    """Compatibility helper returning only PASS/FAIL/REJECT."""
    return evaluate_gate_artifacts(output_paths).verdict


def extract_findings(text: str) -> Optional[list[dict]]:
    """Extract findings from either the new object or legacy JSON arrays."""
    payload, error = unwrap_gate_transcript(text)
    if error:
        return None
    for value, parsed in _json_objects(payload):
        if not parsed or value is None:
            continue
        if isinstance(value, dict) and "findings" in value:
            findings = _bounded_findings(value.get("findings"))
        elif isinstance(value, list):
            findings = _bounded_findings(value)
        else:
            findings = None
        if findings:
            return findings
    return None
