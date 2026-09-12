"""Source-first project comparison, migration planning, and upgrade rehearsal.

The command deliberately separates deterministic evidence from model judgment:

    pi-batch migrate compare --source OLD --target NEW --manifest FILE --json
    pi-batch migrate plan --source OLD --target NEW --manifest FILE --json
    pi-batch migrate rehearse --source OLD --target NEW --manifest FILE --json
    pi-batch migrate upgrade --baseline BASE --target PROJECT --manifest FILE

A manifest describes capabilities, source/target evidence, dependencies and
acceptance commands.  Missing evidence is a migration gap; target evidence
alone is not declared parity until an explicit acceptance command passes.
No command in this module edits project code or starts an agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import config
from .config import log, yaml
from .text_io import read_text_bounded

SCHEMA_VERSION = "migration/v1"
STATUSES = (
    "source_invalid", "missing", "present_unverified", "verified",
    "gate_failed", "retained_noop", "not_in_scope",
)
STATUS_WEIGHT = {
    "source_invalid": 0.0, "missing": 0.0, "present_unverified": 0.5,
    "verified": 1.0, "gate_failed": 0.0, "retained_noop": 1.0,
    "not_in_scope": 1.0,
}
KNOWN_TOP = {"schema_version", "name", "scope", "capabilities", "gates"}
KNOWN_CAPABILITY = {
    "id", "title", "description", "priority", "risk", "effort",
    "depends_on", "owner", "disposition", "source", "target", "acceptance",
}
KNOWN_SIDE = {"evidence"}
KNOWN_EVIDENCE = {"path", "symbol", "line", "claim"}
KNOWN_ACCEPTANCE = {"id", "description", "command", "timeout"}
DEFAULT_EXCLUDES = (
    ".git", ".pi-batch", "node_modules", "dist", "build", "coverage",
    "__pycache__", ".venv", "venv",
)


class MigrationSpecError(ValueError):
    """The source/target contract is malformed or unsafe."""


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise MigrationSpecError(f"{label} must be a mapping")
    return value


def _nonempty(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationSpecError(f"{label} must be a non-empty string")
    return value.strip()


def _safe_relative(value: str, label: str) -> str:
    shown = _nonempty(value, label)
    path = Path(shown)
    if path.is_absolute() or ".." in path.parts:
        raise MigrationSpecError(f"{label} must stay relative to its project root")
    return path.as_posix()


def _string_list(value, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MigrationSpecError(f"{label} must be a string list")
    return [item.strip() for item in value if item.strip()]


def _evidence(value, label: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    result = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            item = {"path": item}
        item = _mapping(item, f"{label}[{index}]")
        unknown = sorted(set(item) - KNOWN_EVIDENCE)
        if unknown:
            raise MigrationSpecError(
                f"{label}[{index}] unknown field(s): {', '.join(unknown)}")
        path = _safe_relative(item.get("path", ""), f"{label}[{index}].path")
        normalized = {"path": path}
        for key in ("symbol", "claim"):
            if key in item:
                normalized[key] = _nonempty(item[key], f"{label}[{index}].{key}")
        if "line" in item:
            if isinstance(item["line"], bool) or not isinstance(item["line"], int) or item["line"] < 1:
                raise MigrationSpecError(f"{label}[{index}].line must be a positive integer")
            normalized["line"] = item["line"]
        result.append(normalized)
    return result


def _acceptance(value, label: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MigrationSpecError(f"{label} must be a list")
    result = []
    ids = set()
    for index, item in enumerate(value):
        item = _mapping(item, f"{label}[{index}]")
        unknown = sorted(set(item) - KNOWN_ACCEPTANCE)
        if unknown:
            raise MigrationSpecError(
                f"{label}[{index}] unknown field(s): {', '.join(unknown)}")
        check_id = _nonempty(item.get("id", f"check-{index + 1}"), f"{label}[{index}].id")
        if check_id in ids:
            raise MigrationSpecError(f"{label} duplicate id: {check_id}")
        ids.add(check_id)
        description = _nonempty(item.get("description", check_id), f"{label}[{index}].description")
        check = {"id": check_id, "description": description}
        if "command" in item:
            check["command"] = _nonempty(item["command"], f"{label}[{index}].command")
        if "timeout" in item:
            timeout = item["timeout"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise MigrationSpecError(f"{label}[{index}].timeout must be positive")
            check["timeout"] = float(timeout)
        result.append(check)
    return result


def _side(value, label: str) -> dict:
    value = _mapping(value or {}, label)
    unknown = sorted(set(value) - KNOWN_SIDE)
    if unknown:
        raise MigrationSpecError(f"{label} unknown field(s): {', '.join(unknown)}")
    return {"evidence": _evidence(value.get("evidence"), f"{label}.evidence")}


def _capability(value, index: int) -> dict:
    label = f"capabilities[{index}]"
    value = _mapping(value, label)
    unknown = sorted(set(value) - KNOWN_CAPABILITY)
    if unknown:
        raise MigrationSpecError(f"{label} unknown field(s): {', '.join(unknown)}")
    item = {
        "id": _nonempty(value.get("id", ""), f"{label}.id"),
        "title": _nonempty(value.get("title", ""), f"{label}.title"),
        "description": str(value.get("description", "")).strip(),
        "priority": value.get("priority", "P1"),
        "risk": value.get("risk", "medium"),
        "effort": value.get("effort", 30),
        "depends_on": _string_list(value.get("depends_on"), f"{label}.depends_on"),
        "owner": value.get("owner", "target"),
        "disposition": value.get("disposition", "migrate"),
        "source": _side(value.get("source"), f"{label}.source"),
        "target": _side(value.get("target"), f"{label}.target"),
        "acceptance": _acceptance(value.get("acceptance"), f"{label}.acceptance"),
    }
    if item["priority"] not in {"P0", "P1", "P2", "P3"}:
        raise MigrationSpecError(f"{label}.priority must be P0, P1, P2 or P3")
    if item["risk"] not in {"low", "medium", "high"}:
        raise MigrationSpecError(f"{label}.risk must be low, medium or high")
    if item["owner"] not in {"target", "iris-ui", "source", "external", "manual"}:
        raise MigrationSpecError(f"{label}.owner is not a supported owner")
    if item["disposition"] not in {"migrate", "retain_noop", "not_in_scope"}:
        raise MigrationSpecError(f"{label}.disposition is invalid")
    if isinstance(item["effort"], bool) or not isinstance(item["effort"], (int, float)) or item["effort"] <= 0:
        raise MigrationSpecError(f"{label}.effort must be positive minutes")
    return item


def load_manifest(path: str = "") -> dict:
    """Load a strict migration contract; missing manifest means inventory only."""
    if not path:
        return {"schema_version": SCHEMA_VERSION, "name": "unmapped-comparison", "scope": _scope({}), "capabilities": [], "gates": []}
    candidate = Path(path).expanduser().resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise MigrationSpecError(f"manifest is not a regular file: {candidate}")
    try:
        data = yaml.safe_load(read_text_bounded(candidate, config.INPUT_MAX_BYTES, "migration manifest")) or {}
    except Exception as exc:
        raise MigrationSpecError(f"cannot read manifest: {exc}") from exc
    data = _mapping(data, "manifest")
    unknown = sorted(set(data) - KNOWN_TOP)
    if unknown:
        raise MigrationSpecError(f"manifest unknown field(s): {', '.join(unknown)}")
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise MigrationSpecError(f"unsupported schema_version: {version!r}")
    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list):
        raise MigrationSpecError("capabilities must be a list")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "name": _nonempty(data.get("name", candidate.stem), "manifest.name"),
        "scope": _scope(data.get("scope")),
        "capabilities": [_capability(item, index) for index, item in enumerate(capabilities)],
        "gates": _acceptance(data.get("gates"), "gates"),
    }
    ids = [item["id"] for item in normalized["capabilities"]]
    if len(set(ids)) != len(ids):
        raise MigrationSpecError("capabilities contains duplicate ids")
    known = set(ids)
    for item in normalized["capabilities"]:
        unknown_deps = sorted(set(item["depends_on"]) - known)
        if unknown_deps:
            raise MigrationSpecError(f"{item['id']} depends on unknown capability: {unknown_deps[0]}")
    _topological_order(normalized["capabilities"])
    return normalized


def _scope(value) -> dict:
    value = _mapping(value or {}, "manifest.scope")
    unknown = sorted(set(value) - {"include", "exclude", "max_files"})
    if unknown:
        raise MigrationSpecError(f"manifest.scope unknown field(s): {', '.join(unknown)}")
    include = _string_list(value.get("include"), "manifest.scope.include")
    exclude = _string_list(value.get("exclude"), "manifest.scope.exclude")
    max_files = value.get("max_files", 10000)
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        raise MigrationSpecError("manifest.scope.max_files must be positive")
    return {"include": include, "exclude": exclude or list(DEFAULT_EXCLUDES), "max_files": max_files}


def _root(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise MigrationSpecError(f"{label} is not a directory: {path}")
    return path


def _path_inside(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise MigrationSpecError(f"evidence escapes project root: {relative}") from exc
    return target


def _evidence_result(root: Path, items: list[dict]) -> dict:
    existing, missing = [], []
    for item in items:
        target = _path_inside(root, item["path"])
        record = dict(item)
        record["path_exists"] = target.is_file() or target.is_dir()
        record["symbol_exists"] = _symbol_exists(target, item.get("symbol"))
        record["exists"] = record["path_exists"] and record["symbol_exists"]
        (existing if record["exists"] else missing).append(record)
    return {"expected": len(items), "existing": existing, "missing": missing, "complete": bool(items) and not missing}


def _symbol_exists(path: Path, symbol: Optional[str]) -> bool:
    if not symbol or not path.is_file():
        return True if not symbol else False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return symbol in content


def _file_allowed(relative: Path, scope: dict) -> bool:
    import fnmatch
    parts = set(relative.parts)
    if any(part in scope["exclude"] for part in parts):
        return False
    include = scope["include"]
    if not include:
        return True
    shown = relative.as_posix()
    return any(fnmatch.fnmatch(shown, pattern) or fnmatch.fnmatch(shown, pattern + "/**") for pattern in include)


def _inventory(root: Path, scope: dict) -> dict:
    paths, total_bytes = [], 0
    for path in sorted(root.rglob("*")):
        if len(paths) >= scope["max_files"]:
            break
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if not _file_allowed(relative, scope):
            continue
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue
        paths.append(relative.as_posix())
    return {"files": len(paths), "bytes": total_bytes, "truncated": len(paths) >= scope["max_files"], "paths": paths}


def _topological_order(capabilities: list[dict]) -> list[str]:
    by_id = {item["id"]: item for item in capabilities}
    order, visiting, visited = [], set(), set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise MigrationSpecError(f"capability dependency cycle at: {item_id}")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in by_id[item_id]["depends_on"]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)
        order.append(item_id)

    for item in capabilities:
        visit(item["id"])
    return order


def _run_gate(check: dict, target: Path) -> dict:
    timeout = float(check.get("timeout", 300))
    try:
        result = subprocess.run(check["command"], cwd=str(target), shell=True, text=True,
                                capture_output=True, timeout=timeout)
        return {"id": check["id"], "command": check["command"], "exit": result.returncode,
                "passed": result.returncode == 0, "stdout": (result.stdout or "")[-2000:],
                "stderr": (result.stderr or "")[-2000:]}
    except subprocess.TimeoutExpired as exc:
        return {"id": check["id"], "command": check["command"], "exit": None,
                "passed": False, "timeout": True, "stdout": str(exc.stdout or "")[-2000:],
                "stderr": str(exc.stderr or "")[-2000:]}
    except OSError as exc:
        return {"id": check["id"], "command": check["command"], "exit": None,
                "passed": False, "error": str(exc)}


def _assess_capability(item: dict, source: Path, target: Path, run_gates: bool) -> dict:
    source_evidence = _evidence_result(source, item["source"]["evidence"])
    target_evidence = _evidence_result(target, item["target"]["evidence"])
    checks = []
    if run_gates and target_evidence["complete"]:
        checks = [_run_gate(check, target) for check in item["acceptance"] if check.get("command")]
    source_ok = source_evidence["complete"]
    target_ok = target_evidence["complete"]
    if item["disposition"] == "not_in_scope":
        status = "not_in_scope"
    elif item["disposition"] == "retain_noop":
        status = "retained_noop"
    elif not source_ok:
        status = "source_invalid"
    elif not target_ok:
        status = "missing"
    elif checks and all(check["passed"] for check in checks):
        status = "verified"
    elif checks:
        status = "gate_failed"
    else:
        status = "present_unverified"
    return {"id": item["id"], "title": item["title"], "priority": item["priority"],
            "risk": item["risk"], "effort": float(item["effort"]), "owner": item["owner"],
            "disposition": item["disposition"], "depends_on": item["depends_on"], "status": status,
            "source_evidence": source_evidence, "target_evidence": target_evidence,
            "acceptance": item["acceptance"], "gate_results": checks}


def _metrics(results: list[dict], gates: list[dict]) -> dict:
    in_scope = [item for item in results if item["status"] != "not_in_scope"]
    counts = {status: sum(item["status"] == status for item in results) for status in STATUSES}
    denominator = len(in_scope)
    weighted = sum(STATUS_WEIGHT[item["status"]] for item in in_scope)
    source_valid = sum(item["source_evidence"]["complete"] for item in in_scope)
    target_present = sum(item["target_evidence"]["complete"] for item in in_scope)
    executed = [result for result in gates if result.get("id")]
    passed = sum(result.get("passed", False) for result in executed)
    return {
        "capabilities": len(results), "in_scope": denominator, "counts": counts,
        "weighted_completion": round(weighted / denominator, 3) if denominator else 1.0,
        "source_evidence_coverage": round(source_valid / denominator, 3) if denominator else 1.0,
        "target_evidence_coverage": round(target_present / denominator, 3) if denominator else 1.0,
        "acceptance": {"executed": len(executed), "passed": passed,
                       "coverage": round(passed / len(executed), 3) if executed else None},
        "ready": bool(denominator) and all(item["status"] in {"verified", "retained_noop"} for item in in_scope),
        "strict_failures": [item["id"] for item in in_scope if item["status"] not in {"verified", "retained_noop"}],
    }


def _plan(results: list[dict]) -> dict:
    pending = {item["id"]: item for item in results if item["status"] in {"missing", "present_unverified", "gate_failed"} and item["disposition"] == "migrate"}
    by_id = {item["id"]: item for item in results}
    waves = {}
    for item in results:
        if item["id"] not in pending:
            continue
        dependencies = [dependency for dependency in item["depends_on"] if dependency in pending]
        waves[item["id"]] = 0 if not dependencies else max(waves[dependency] for dependency in dependencies) + 1
    grouped = {}
    for item_id, wave in waves.items():
        grouped.setdefault(wave, []).append(item_id)
    items = []
    for wave in sorted(grouped):
        for item_id in sorted(grouped[wave], key=lambda value: (pending[value]["priority"], value)):
            item = pending[item_id]
            items.append({"id": item_id, "title": item["title"], "wave": wave,
                          "priority": item["priority"], "risk": item["risk"],
                          "effort_minutes": item["effort"], "owner": item["owner"],
                          "depends_on": item["depends_on"], "route": "target" if item["owner"] == "target" else item["owner"],
                          "status": item["status"]})
    return {"items": items, "waves": len(grouped), "pending": len(items),
            "serial_minutes": round(sum(item["effort_minutes"] for item in items), 1),
            "critical_path_minutes": round(sum(max((item["effort_minutes"] for item in items if item["wave"] == wave), default=0) for wave in grouped), 1)}


def build_report(source_value: str, target_value: str, manifest_value: str = "",
                 mode: str = "migrate", run_gates: bool = False,
                 concurrency: int = 1, budget_minutes: Optional[float] = None) -> dict:
    source, target = _root(source_value, "source"), _root(target_value, "target")
    manifest = load_manifest(manifest_value)
    scope = manifest["scope"]
    results = [_assess_capability(item, source, target, run_gates) for item in manifest["capabilities"]]
    capability_gates = [gate for capability in results for gate in capability["gate_results"]]
    project_gates = [_run_gate(check, target) for check in manifest["gates"]] if run_gates else []
    gate_results = capability_gates + project_gates
    metrics = _metrics(results, gate_results)
    failed_project_gates = [item["id"] for item in project_gates if not item.get("passed")]
    if failed_project_gates:
        metrics["ready"] = False
        metrics["strict_failures"].extend(f"project-gate:{item}" for item in failed_project_gates)
    plan = _plan(results)
    effective_minutes = (plan["critical_path_minutes"] if concurrency > 1
                         else plan["serial_minutes"])
    rehearsal = {
        "mode": "dry_run",
        "concurrency": concurrency,
        "budget_minutes": budget_minutes,
        "budget_status": "not_set" if budget_minutes is None else
                         ("within" if effective_minutes <= budget_minutes else "exceeded"),
        "serial_minutes": plan["serial_minutes"],
        "critical_path_minutes": effective_minutes,
        "projected_completion_if_all_pending_pass": _projected_completion(metrics, plan),
    }
    return {
        "schema_version": SCHEMA_VERSION, "mode": mode, "manifest": manifest["name"],
        "source": str(source), "target": str(target),
        "inventory": {"source": _inventory(source, scope), "target": _inventory(target, scope)},
        "capabilities": results, "metrics": metrics, "plan": plan,
        "rehearsal": rehearsal, "gates": gate_results,
        "_note": "Evidence and commands are deterministic checks; no parity claim is made for unmapped source behavior.",
    }


def _projected_completion(metrics: dict, plan: dict) -> float:
    total = metrics["in_scope"]
    if not total:
        return 1.0
    verified = metrics["counts"].get("verified", 0) + metrics["counts"].get("retained_noop", 0)
    return round((verified + plan["pending"]) / total, 3)


def _common(parser: argparse.ArgumentParser, baseline: bool = False) -> None:
    parser.add_argument("--source" if not baseline else "--baseline", required=True,
                        help="source/reference project" if not baseline else "baseline project")
    parser.add_argument("--target", required=True, help="target/current project")
    parser.add_argument("--manifest", default="", help="migration/v1 YAML contract")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="", help="explicit JSON report path")
    parser.add_argument("--run-gates", action="store_true", help="run declared acceptance commands; never edit code")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--budget-minutes", type=float, default=None)
    parser.add_argument("--strict", action="store_true", help="exit 1 when the contract is not fully verified")


def _write_report(path: str, report: dict) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def _print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    metrics = report["metrics"]
    print(f"# Migration {report['mode']}: {report['manifest']}")
    print(f"source: {report['source']}\ntarget: {report['target']}")
    print("capabilities: {capabilities} | verified: {verified} | missing: {missing} | "
          "unverified: {unverified}".format(
              capabilities=metrics["capabilities"], verified=metrics["counts"].get("verified", 0),
              missing=metrics["counts"].get("missing", 0),
              unverified=metrics["counts"].get("present_unverified", 0)))
    print(f"weighted completion: {metrics['weighted_completion']:.1%} | ready: {metrics['ready']}")
    print(f"plan: {report['plan']['pending']} pending / {report['plan']['waves']} waves")
    for item in report["plan"]["items"]:
        print(f"- W{item['wave']} {item['priority']} {item['id']} [{item['status']}] → {item['route']}")


def migrate_main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-batch.py migrate", description="Source-first compare/plan/rehearse/upgrade")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("compare", "plan", "rehearse"):
        child = sub.add_parser(name, help=f"{name} without code mutation")
        _common(child)
    upgrade = sub.add_parser("upgrade", help="compare a baseline project with its current target")
    _common(upgrade, baseline=True)
    args = parser.parse_args(argv)
    if args.concurrency < 1 or (args.budget_minutes is not None and args.budget_minutes < 0):
        parser.error("--concurrency must be positive and --budget-minutes must be non-negative")
    try:
        source = args.source if args.command != "upgrade" else args.baseline
        report = build_report(source, args.target, args.manifest, "upgrade" if args.command == "upgrade" else args.command,
                              args.run_gates, args.concurrency, args.budget_minutes)
        if args.command == "rehearse" and args.budget_minutes is not None and report["rehearsal"]["budget_status"] == "exceeded":
            report["rehearsal"]["ready"] = False
        _write_report(args.out, report)
        _print_report(report, args.json)
    except (MigrationSpecError, OSError, ValueError) as exc:
        log.error("migrate: %s", exc)
        if args.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.strict and not report["metrics"]["ready"]:
        return 1
    if args.command == "rehearse" and report["rehearsal"].get("budget_status") == "exceeded":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(migrate_main())
