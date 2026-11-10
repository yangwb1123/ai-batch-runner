"""Whitelisted metadata for typed runtime evidence; no file access or acceptance."""

from __future__ import annotations

import re


EVIDENCE_EVENTS = frozenset({
    "validation.started", "validation.completed", "gate.verdict", "artifact.created",
})
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def project_evidence(event, summary, identity):
    """Project explicit fields only; invalid/missing facts never become PASS."""
    payload = event.payload
    labels = (("gate", "name") if event.kind == "gate.verdict" else
              ("name", "path") if event.kind == "artifact.created" else ("validator", "name"))
    fields = _base_fields(payload, labels, summary, identity)
    if event.kind == "artifact.created":
        _artifact_fields(payload, fields, summary)
    elif event.kind == "gate.verdict":
        verdict = payload.get("verdict")
        fields["state"] = {"PASS": "passed", "FAIL": "failed", "REJECT": "rejected"}.get(
            verdict if isinstance(verdict, str) else "", "unknown")
    elif event.kind == "validation.started":
        fields["state"] = "running"
    else:
        fields["state"], fields["exit_code"] = _validation_outcome(payload)
    fields["evidence_partial"] |= fields["state"] == "unknown"
    if fields["evidence_partial"]:
        fields["state"] = "unknown"
    return fields


def _base_fields(payload, labels, summary, identity):
    invalid_flags = any(name in payload and type(payload[name]) is not bool
                        for name in ("truncated", "evidence_partial"))
    fields = {"evidence_partial": invalid_flags or payload.get("evidence_partial") is True,
              "truncated": payload.get("truncated") is True}
    try:
        fields.update(identity(payload))
    except ValueError:
        fields["evidence_partial"] = True
    values = [payload[name] for name in labels if name in payload]
    fields["evidence_partial"] |= any(not isinstance(value, str) for value in values)
    label = next((value for value in values if isinstance(value, str) and value), "")
    fields["text"], cut = summary(label, 256)
    fields["truncated"] |= cut
    fields["evidence_partial"] |= not bool(fields["text"])
    if "reason" in payload:
        _summary_field(payload, fields, "reason", 1024, summary)
    return fields


def _summary_field(payload, fields, name, limit, summary):
    value = payload[name]
    if not isinstance(value, str):
        fields["evidence_partial"] = True
        return
    fields[name], cut = summary(value, limit)
    fields["truncated"] |= cut


def _exit_code(payload):
    values = [payload[name] for name in ("validation_exit", "exit_code") if name in payload]
    valid = [value for value in values if type(value) is int and -(2 ** 31) <= value < 2 ** 31]
    invalid = len(valid) != len(values) or len(set(valid)) > 1
    return (valid[0] if valid and not invalid else None), invalid


def _validation_outcome(payload):
    code, invalid = _exit_code(payload)
    outcomes = [payload[name] for name in ("success", "passed", "ok") if name in payload]
    flags = [value for value in outcomes if type(value) is bool]
    if (invalid or len(flags) != len(outcomes) or not flags or len(set(flags)) > 1
            or type(payload.get("cancelled", False)) is not bool):
        return "unknown", code
    if payload.get("cancelled") is True:
        return "cancelled", code
    if code is not None and flags[0] != (code == 0):
        return "unknown", code
    return ("passed" if flags[0] else "failed"), code


def _artifact_fields(payload, fields, summary):
    fields["state"] = "recorded"
    digest = payload.get("sha256")
    if isinstance(digest, str) and _HASH.fullmatch(digest):
        fields["sha256"] = digest
    else:
        fields["evidence_partial"] = True
    size = payload.get("size_bytes")
    if type(size) is int and 0 <= size <= 2 ** 53 - 1:
        fields["size_bytes"] = size
    else:
        fields["evidence_partial"] = True
    if "media_type" in payload:
        _summary_field(payload, fields, "media_type", 128, summary)
    if "path" in payload:
        path = {"evidence_partial": False, "truncated": False}
        _summary_field(payload, path, "path", 512, summary)
        fields["evidence_partial"] |= path.get("evidence_partial", False)
        fields["truncated"] |= path.get("truncated", False)
        if path.get("path"):
            fields["paths"] = [path["path"]]
