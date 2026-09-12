#!/usr/bin/env python3
"""Validate the bounded Phase 4 implementation-readiness summary."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_common import load_platform_map, project_names  # noqa: E402

REQUIRED_OBJECTS = {
    "persistence": (
        "tables", "transaction_boundaries", "migration_steps", "open_questions",
    ),
    "workflow": (
        "states", "commands", "events", "approval_rules", "compensation_rules",
    ),
    "audit": ("ingest_path", "schema_version", "outbox_contract", "receipt_contract", "missing_evidence"),
    "operations": (
        "failure_cases", "runbooks", "tests", "definition_of_ready", "definition_of_done",
    ),
    "ownership": ("collaborating_projects", "unaffected_projects", "sources_of_truth"),
}
LIST_FIELDS = {
    "blockers", "risks", "tables", "transaction_boundaries", "migration_steps",
    "open_questions", "states", "commands", "events", "approval_rules",
    "compensation_rules", "missing_evidence", "failure_cases", "runbooks",
    "tests", "definition_of_ready", "definition_of_done", "collaborating_projects",
    "unaffected_projects",
}
CHANGE_ID_RE = re.compile(r"^COMPOSE-[0-9]{4}-[0-9]{3}$")
EVENT_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){2,}@v[0-9]+$")


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid readiness summary: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("readiness summary must be a JSON object")
    return value


def _require_text(value: object, name: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name}: must be a non-empty string")


def _check_top_level(summary: dict, errors: list[str]) -> None:
    required = {"change_id", "primary_project", "status", "blockers", "persistence",
                "workflow", "audit", "operations", "ownership", "risks"}
    for key in sorted(required - set(summary)):
        errors.append(f"missing top-level field: {key}")
    for key in LIST_FIELDS:
        if key in summary and not isinstance(summary[key], list):
            errors.append(f"{key}: must be a list")


def _check_sections(summary: dict, errors: list[str]) -> None:
    for section, fields in REQUIRED_OBJECTS.items():
        value = summary.get(section)
        if not isinstance(value, dict):
            errors.append(f"{section}: must be an object")
            continue
        for field in fields:
            if field not in value:
                errors.append(f"{section}.{field}: missing")
            elif field in LIST_FIELDS and not isinstance(value[field], list):
                errors.append(f"{section}.{field}: must be a list")


def _check_identity(summary: dict, errors: list[str]) -> None:
    if not CHANGE_ID_RE.fullmatch(str(summary.get("change_id", ""))):
        errors.append("change_id: must match COMPOSE-YYYY-NNN")
    if summary.get("primary_project") != "sverpweb-stable":
        errors.append("primary_project: first slice must be sverpweb-stable")
    if summary.get("status") not in {"READY_FOR_INDEPENDENT_GATE", "BLOCKED"}:
        errors.append("status: invalid readiness status")


def _check_audit_and_events(summary: dict, errors: list[str]) -> None:
    audit = summary.get("audit")
    if isinstance(audit, dict):
        for field in ("ingest_path", "schema_version", "outbox_contract", "receipt_contract"):
            _require_text(audit.get(field), f"audit.{field}", errors)
    workflow = summary.get("workflow")
    events = workflow.get("events", []) if isinstance(workflow, dict) else []
    for index, event in enumerate(events):
        if isinstance(event, str) and not EVENT_RE.fullmatch(event):
            errors.append(f"workflow.events[{index}]: invalid event name {event!r}")


def _check_shape(summary: dict, errors: list[str]) -> None:
    _check_top_level(summary, errors)
    _check_sections(summary, errors)
    _check_identity(summary, errors)
    _check_audit_and_events(summary, errors)


def _check_projects(summary: dict, root: Path, errors: list[str]) -> None:
    try:
        known = project_names(load_platform_map(root / "examples/snaplink-platform"))
    except ValueError as exc:
        errors.append(f"platform-map: {exc}")
        return
    ownership = summary.get("ownership")
    if not isinstance(ownership, dict):
        return
    for field in ("collaborating_projects", "unaffected_projects"):
        values = ownership.get(field, [])
        if not isinstance(values, list):
            continue
        for project in values:
            if project not in known:
                errors.append(f"ownership.{field}: unknown project {project!r}")
    overlap = set(ownership.get("collaborating_projects", [])) & set(ownership.get("unaffected_projects", []))
    if overlap:
        errors.append(f"ownership: collaborating/unaffected overlap: {sorted(overlap)}")


def check_summary(summary: dict, root: Path) -> list[str]:
    errors: list[str] = []
    _check_shape(summary, errors)
    _check_projects(summary, root, errors)
    if summary.get("status") == "READY_FOR_INDEPENDENT_GATE" and summary.get("blockers"):
        errors.append("status=READY_FOR_INDEPENDENT_GATE requires an empty blockers list")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        summary = _load(Path(args.summary))
        errors = check_summary(summary, Path(args.root).resolve())
    except ValueError as exc:
        errors = [str(exc)]
    report = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for error in errors:
            print(f"READINESS: {error}")
        print("READINESS: PASS" if report["ok"] else "READINESS: FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
