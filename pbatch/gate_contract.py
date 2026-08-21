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
_FINDING_FIELDS = ("severity", "file", "line", "problem", "fix_hint")


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
            yield json.loads(body), True
        except (TypeError, ValueError, RecursionError):
            yield None, True
    stripped = (text or "").strip()
    if ((stripped.startswith("{") and stripped.endswith("}"))
            or (stripped.startswith("[") and stripped.endswith("]"))):
        found = True
        try:
            yield json.loads(stripped), True
        except (TypeError, ValueError, RecursionError):
            yield None, True
    if not found:
        return


def _is_contract_candidate(value) -> bool:
    return isinstance(value, dict) and (
        "schema_version" in value or "verdict" in value)


def _structured_decision(text: str) -> tuple[Optional[GateDecision], bool]:
    """Return (decision, saw_contract_candidate)."""
    saw_candidate = False
    decision = None
    for value, parsed in _json_objects(text):
        if not parsed:
            continue
        if value is None:
            saw_candidate = True
            return GateDecision(reason="malformed structured Gate JSON",
                                source="structured", valid=False), True
        if not _is_contract_candidate(value):
            continue
        if saw_candidate:
            return GateDecision(
                reason="multiple structured Gate contracts in one artifact",
                source="structured", valid=False), True
        saw_candidate = True
        normalized = dict(value)
        if isinstance(normalized.get("verdict"), str):
            normalized["verdict"] = normalized["verdict"].upper()
        errors = validate_json(normalized, GATE_DECISION_SCHEMA)
        if errors:
            return GateDecision(
                reason="structured Gate contract rejected: " + "; ".join(errors[:4]),
                source="structured", valid=False), True
        validators = [dict(item) for item in normalized.get("validators", [])]
        findings = _bounded_findings(normalized.get("findings", [])) or []
        if normalized["verdict"] == "PASS" and any(
                str(item.get("status", "")).upper() in ("FAIL", "REJECT")
                for item in validators):
            return GateDecision(
                reason="structured Gate PASS contains a failing validator",
                source="structured", valid=False), True
        decision = GateDecision(
            verdict=normalized["verdict"], reason=normalized["reason"][:4000],
            evidence=[str(item)[:1000] for item in normalized.get("evidence", [])],
            validators=validators[:64], findings=findings,
            source="structured", valid=True)
    return decision, saw_candidate


def _legacy_decision(text: str) -> GateDecision:
    first_pass = None
    bare = False
    for match in _VERDICT_RE.finditer(text or ""):
        line_end = text.find("\n", match.end())
        rest = text[match.end():line_end if line_end >= 0 else len(text)].strip()
        if not rest:
            bare = True
            continue
        verdict = match.group(1).upper()
        if verdict in ("FAIL", "REJECT"):
            return GateDecision(verdict=verdict, reason=rest[:4000],
                                source="legacy_text", valid=True)
        if first_pass is None:
            first_pass = GateDecision(verdict="PASS", reason=rest[:4000],
                                      source="legacy_text", valid=True)
    if first_pass:
        return first_pass
    return GateDecision(
        reason="bare VERDICT has no reason" if bare else "no usable Gate verdict",
        source="legacy_text" if bare else "none", valid=False)


def parse_gate_text(text: str) -> GateDecision:
    """Parse one artifact, preferring structured JSON over legacy text."""
    structured, saw_candidate = _structured_decision(text)
    if saw_candidate:
        return structured or GateDecision(
            reason="structured Gate contract is missing", source="structured")
    return _legacy_decision(text)


def evaluate_gate_artifacts(output_paths: list[str], max_bytes: int = 0) -> GateDecision:
    """Aggregate artifacts; any structured contract error fails closed."""
    decisions = []
    errors = []
    limit = max_bytes or config.OUTPUT_MAX_BYTES
    for raw_path in output_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            text = read_text_bounded(path, limit, "gate artifact")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        decision = parse_gate_text(text)
        if decision.source == "structured" and not decision.valid:
            errors.append(f"{path}: {decision.reason}")
        elif decision.verdict:
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
    for value, parsed in _json_objects(text):
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
