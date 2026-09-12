#!/usr/bin/env python3
"""Mechanical gate for the platform architecture first-slice contract.

This checker validates the bounded Phase 3 artifacts without treating an LLM
review as evidence.  The default mode is fail-closed: a mechanical contract
and an independent ``VERDICT: PASS`` gate are both required.  Use
``--mechanical-only`` while the human/independent gate is still pending.

Usage:
  python check-architecture-contract.py [ROOT] --json
  python check-architecture-contract.py [ROOT] --mechanical-only
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType


PHASE3 = Path("docs/snaplink-platform/phase3")
CONTRACTS = Path("docs/snaplink-platform/contracts")
REQUIRED_FILES = (
    "01-intake.md",
    "02-scenario.md",
    "03-capabilities.md",
    "04-composition.md",
    "05-changes.md",
    "06-review-triage.md",
    "07-gate-evidence.md",
    "summary.json",
    "change-manifest.yaml",
)
CONTRACT_FILES = (
    "README.md",
    "EXECUTION_CONTEXT_V1.md",
    "WORKFLOW_CONTRACT_V1.md",
    "AUDIT_PROVENANCE_V1.md",
    "AGENT_CAPABILITY_V1.md",
    "PLUGIN_MANIFEST_V1.md",
)
REQUIRED_MARKERS = {
    "01-intake.md": ("## Interpreted Goal", "## Evidence and Unknowns"),
    "02-scenario.md": ("## Purchase Request Happy Path", "## Tenant Isolation Scenarios"),
    "03-capabilities.md": ("## Capability Map", "## Missing Contracts"),
    "04-composition.md": ("## Data Sources of Truth", "## Forbidden Dependencies"),
    "05-changes.md": ("## Process and Approval Schema", "## Test Plan"),
    "06-review-triage.md": ("## Accepted or Partially Accepted Findings", "## Rejected or Untrusted Findings"),
    "07-gate-evidence.md": ("## Gate Questions", "## Review Triage"),
}
CONTRACT_MARKERS = {
    "README.md": ("## Contract registry", "## Promotion states"),
    "EXECUTION_CONTEXT_V1.md": ("Tenant resolution", "Acceptance criteria"),
    "WORKFLOW_CONTRACT_V1.md": ("ProcessInstance", "Acceptance criteria"),
    "AUDIT_PROVENANCE_V1.md": ("Actor model", "Acceptance criteria"),
    "AGENT_CAPABILITY_V1.md": ("Grant object", "Acceptance criteria"),
    "PLUGIN_MANIFEST_V1.md": ("Manifest", "Acceptance criteria"),
}
REQUIRED_EVENTS = {
    "governance.workflow.submitted@v1",
    "governance.approval.recorded@v1",
    "governance.execution.completed@v1",
    "governance.compensation.requested@v1",
    "vault.asset.linked@v1",
}
REQUIRED_PERMISSIONS = {
    "governance.workflow.start",
    "governance.workflow.approve",
    "vault.asset.share",
    "audit.event.write",
}
REQUIRED_FAILURE_POLICIES = {
    "authorization": "fail_closed",
    "audit": "durable_async",
    "notification": "best_effort_async",
}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load checker module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_inputs(root: Path):
    scripts = root / "examples/snaplink-platform/scripts"
    common = _load_module("platform_common_for_arch_gate", scripts / "platform_common.py")
    summary_checker = _load_module("decision_summary_for_arch_gate", scripts / "check-decision-summary.py")
    manifest_checker = _load_module("change_manifest_for_arch_gate", scripts / "check-change-manifest.py")
    pmap = common.load_platform_map(root / "examples/snaplink-platform")
    summary_path = root / PHASE3 / "summary.json"
    manifest_path = root / PHASE3 / "change-manifest.yaml"
    summary = summary_checker.load_summary(summary_path)
    manifest = manifest_checker.load_manifest(manifest_path)
    return common, summary_checker, manifest_checker, pmap, summary, manifest


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def _check_files(root: Path, errors: list[str]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for name in REQUIRED_FILES:
        path = root / PHASE3 / name
        if not path.is_file():
            errors.append(f"missing artifact: {path}")
            continue
        size = path.stat().st_size
        sizes[name] = size
        if size == 0:
            errors.append(f"empty artifact: {path}")
    for name, markers in REQUIRED_MARKERS.items():
        path = root / PHASE3 / name
        if not path.is_file():
            continue
        text = _read(path)
        for marker in markers:
            if marker not in text:
                errors.append(f"{name}: missing required marker {marker!r}")
    return sizes


def _check_contract_files(root: Path, errors: list[str]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for name in CONTRACT_FILES:
        path = root / CONTRACTS / name
        if not path.is_file():
            errors.append(f"missing contract: {path}")
            continue
        size = path.stat().st_size
        sizes[f"contracts/{name}"] = size
        if size == 0:
            errors.append(f"empty contract: {path}")
    for name, markers in CONTRACT_MARKERS.items():
        path = root / CONTRACTS / name
        if not path.is_file():
            continue
        text = _read(path)
        for marker in markers:
            if marker not in text:
                errors.append(f"contracts/{name}: missing required marker {marker!r}")
    return sizes


def _check_summary_projects(summary: dict, errors: list[str]) -> None:
    ownership = summary.get("ownership") or {}
    if ownership.get("primary_project") != "sverpweb-stable":
        errors.append("ownership.primary_project must be sverpweb-stable for the first slice")
    collaborators = set(ownership.get("collaborating_projects") or [])
    unaffected = set(ownership.get("unaffected_projects") or [])
    if collaborators & unaffected:
        errors.append("collaborating_projects and unaffected_projects overlap")
    for project in ("snaplink", "aero-id", "snaplink-audit-governance"):
        if project not in collaborators:
            errors.append(f"required collaborator missing: {project}")
    for project in ("aero-vault", "aero-im", "snaplink-console", "iris-ui"):
        if project not in unaffected:
            errors.append(f"first-slice unaffected project missing: {project}")


def _check_summary_changes(summary: dict, errors: list[str]) -> None:
    changes = summary.get("changes") or {}
    events = set(changes.get("events") or [])
    permissions = set(changes.get("permissions") or [])
    for name in sorted(REQUIRED_EVENTS - events):
        errors.append(f"summary changes.events missing required event: {name}")
    for name in sorted(REQUIRED_PERMISSIONS - permissions):
        errors.append(f"summary changes.permissions missing required permission: {name}")


def _check_summary_policies(summary: dict, errors: list[str]) -> None:
    policies = (summary.get("integration") or {}).get("failure_policies") or {}
    for key, expected in REQUIRED_FAILURE_POLICIES.items():
        if policies.get(key) != expected:
            errors.append(f"summary integration.failure_policies.{key} must be {expected}")


def _check_summary(summary: dict, pmap: dict, summary_checker, errors: list[str]) -> None:
    errors.extend(summary_checker.check_summary(summary, pmap))
    _check_summary_projects(summary, errors)
    _check_summary_changes(summary, errors)
    _check_summary_policies(summary, errors)


def _check_manifest(manifest: dict, pmap: dict, manifest_checker, errors: list[str]) -> None:
    errors.extend(manifest_checker.check_manifest(manifest, pmap))
    if manifest.get("change_id") != "COMPOSE-2026-018":
        errors.append("change_manifest.change_id must be COMPOSE-2026-018")
    if manifest.get("primary_project") != "sverpweb-stable":
        errors.append("change_manifest.primary_project must be sverpweb-stable")
    failure = manifest.get("failure_policy") or {}
    for key, expected in REQUIRED_FAILURE_POLICIES.items():
        if failure.get(key) != expected:
            errors.append(f"change_manifest.failure_policy.{key} must be {expected}")


def _check_architecture_invariants(root: Path, errors: list[str]) -> None:
    paths = [root / PHASE3 / name for name in REQUIRED_FILES if name.endswith(".md")]
    text = "\n".join(_read(path) for path in paths if path.is_file())
    invariants = (
        ("approved != executed", "architecture contract must separate approval from execution"),
        ("audit != telemetry", "architecture contract must separate audit from telemetry"),
        ("tenant", "architecture contract must mention tenant context"),
        ("department_id", "architecture contract must mention stable department_id"),
        ("fail_closed", "architecture contract must contain fail-closed policy"),
        ("durable_async", "architecture contract must contain durable audit delivery"),
        ("Capability Grant", "architecture contract must constrain Agent capability"),
        ("WORM", "architecture contract must define immutable archive evidence"),
        ("sverpweb` 当前为单租户", "architecture contract must retain current single-tenant guard"),
    )
    for needle, message in invariants:
        if needle not in text:
            errors.append(message)


def _gate_status(root: Path) -> tuple[bool, str]:
    path = root / PHASE3 / "gate-v2.md"
    if not path.is_file():
        return False, "gate-v2.md is missing"
    text = _read(path)
    matches = re.findall(r"^\s*VERDICT\s*:\s*(PASS|FAIL|REJECT)\b.*$", text, re.I | re.M)
    if not matches:
        return False, "gate-v2.md has no non-bare VERDICT"
    if any(item.upper() in {"FAIL", "REJECT"} for item in matches):
        return False, "gate-v2.md contains FAIL/REJECT"
    if "PASS" not in {item.upper() for item in matches}:
        return False, "gate-v2.md is not PASS"
    return True, "independent gate PASS"


def check(root: Path, mechanical_only: bool = False) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    sizes = _check_files(root, errors)
    sizes.update(_check_contract_files(root, errors))
    try:
        _, summary_checker, manifest_checker, pmap, summary, manifest = _load_inputs(root)
    except (OSError, ValueError, RuntimeError) as exc:
        errors.append(str(exc))
        pmap = {}
        summary = {}
        manifest = {}
    if summary:
        _check_summary(summary, pmap, summary_checker, errors)
    if manifest:
        _check_manifest(manifest, pmap, manifest_checker, errors)
    _check_architecture_invariants(root, errors)
    gate_ok, gate_reason = _gate_status(root)
    if not gate_ok:
        if mechanical_only:
            warnings.append(f"approval gate pending: {gate_reason}")
        else:
            errors.append(f"approval gate: {gate_reason}")
    return {
        "ok": not errors,
        "mechanical_ok": not any(
            item.startswith("approval gate:") for item in errors
        ) and not any(
            item.startswith("missing artifact") or item.startswith("empty artifact")
            for item in errors
        ),
        "approval_ready": gate_ok and not errors,
        "errors": errors,
        "warnings": warnings,
        "artifacts": sizes,
        "gate": {"ok": gate_ok, "reason": gate_reason},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--mechanical-only",
        action="store_true",
        help="allow the independent approval gate to remain pending",
    )
    args = parser.parse_args(argv)
    report = check(Path(args.root).resolve(), args.mechanical_only)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["errors"]:
            print(f"ARCH-GATE: {item}")
        for item in report["warnings"]:
            print(f"ARCH-GATE WARNING: {item}")
        print("ARCH-GATE: PASS" if report["ok"] else "ARCH-GATE: FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
