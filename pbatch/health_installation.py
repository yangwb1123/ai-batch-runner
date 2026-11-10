"""Bounded installation diagnostics, distinct from project health and CI."""
from __future__ import annotations

import json
import csv
import io
import os
import tempfile
import time
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from .launcher import is_frozen, script_argv
from .process_exec import ValidationResult, run_bounded
from .version import VERSION

PACKAGE_ROOT = Path(__file__).resolve().parent
SCHEMA = "abr.installation-health.v1"
PROBE_SECONDS = 30.0
OUTPUT_BYTES = 65536
CORE_ENTRYPOINTS = {"pbatch.cli": "main", "pbatch.models": "Task",
                    "pbatch.runner": "run_argv", "pbatch.pipeline": "run_pipeline",
                    "pbatch.tui": "tui_main"}
CORE_MODULES = tuple(CORE_ENTRYPOINTS)
CHECK_NAMES = frozenset({"runtime", "distributed_assets", "designintelligence_selfcheck",
                         "knowledge_selfcheck", "backendquality_fixture"})
REQUIRED_ASSETS = (
    "quality.py", "scripts/check-ui-spec.py", "scripts/check-frontend-quality.py",
    "ui-specs/rules.yaml", "backend-specs/rules.yaml", "product-specs/rules.yaml",
    "evolution-specs/rules.yaml", "examples/frontend-implementation-pipeline.yaml",
    "examples/backend-implementation-pipeline.yaml",
)
EXTRA_ASSETS = ("pi-batch.yaml", "scripts/check-installation.py",
                "scripts/check-design-intelligence.py", "scripts/check-knowledge-freshness.py",
                "scripts/check-backend-quality.py", "evals/classifier.yaml", "evals/rules.yaml",
                "evals/assess.yaml", "evals/evolution.yaml", "agent-engineering/orchestrator.yaml",
                "pipelines/pipeline-code-impl.yaml", "prompts/architect.md")


def installation_kind() -> str:
    """Missing tests cannot select this mode; require an owned install record."""
    if is_frozen():
        return "frozen"
    try:
        installed = distribution("ai-batch-runner")
        if _owns_package_record(installed):
            return "wheel"
    except (PackageNotFoundError, OSError, ValueError, RecursionError, csv.Error):
        return "source"
    return "source"


def _owns_package_record(installed) -> bool:
    # distribution.files may fall back to a source egg-info SOURCES.txt.
    # A source manifest or an editable wheel must never change project mode.
    record = installed.read_text("RECORD")
    if not isinstance(record, str) or not record or len(record.encode("utf-8")) > 2097152:
        return False
    if not _noneditable_origin(installed.read_text("direct_url.json")):
        return False
    rows = csv.reader(io.StringIO(record), strict=True)
    owned = False
    for index, row in enumerate(rows):
        if index >= 16384 or len(row) != 3:
            return False
        if row[0] == "pbatch/__init__.py":
            if owned or Path(installed.locate_file(row[0])).resolve() != PACKAGE_ROOT / "__init__.py":
                return False
            owned = True
    return owned


def _noneditable_origin(raw) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16384:
        return False
    origin = json.loads(raw, object_pairs_hook=_unique_fields)
    if not isinstance(origin, dict):
        return False
    details = origin.get("dir_info", {})
    return isinstance(details, dict) and details.get("editable", False) is False


def _asset_status(asset_root: Path) -> dict:
    required = tuple(dict.fromkeys((*REQUIRED_ASSETS, *EXTRA_ASSETS)))
    missing = []
    for name in required:
        path = asset_root / name
        try:
            valid = path.is_file() and 0 < path.stat().st_size <= 8 * 1024 * 1024
        except OSError:
            valid = False
        if not valid:
            missing.append(name)
    return {"root": str(asset_root), "required": len(required),
            "missing": missing, "healthy": not missing}


def _run_probe(asset_root: Path, args: list[str], cwd: Path, deadline: float) -> ValidationResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ValidationResult(False, -1, "", "installation probe deadline exceeded", True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PBATCH_")}
    environment.update(PYTHONPATH=str(PACKAGE_ROOT.parent), PBATCH_TOOL_ROOT=str(asset_root),
                       PBATCH_SCRIPT_DIR=str(cwd), PBATCH_RUNTIME_HOME=str(cwd / "runtime"),
                       XDG_CONFIG_HOME=str(cwd / "config"))
    return run_bounded(args, str(cwd), min(10.0, remaining), OUTPUT_BYTES, False,
                       "installation probe", env=environment)


def _failed(reason: str) -> dict:
    return {"result": "failed", "reason": reason}


def _unique_fields(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate probe field")
        value[name] = item
    return value


def _object(output: str) -> dict:
    try:
        value = json.loads(output, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _runtime_probe(asset_root: Path, deadline: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="abr-install-runtime-") as name:
        args = script_argv(asset_root / "scripts/check-installation.py", "--json")
        result = _run_probe(asset_root, args, Path(name), deadline)
    value = _object(result.stdout)
    expected = {"schema_version": 1, "package_root": str(PACKAGE_ROOT),
                "modules": list(CORE_MODULES), "package_assets": 13}
    if (not result.ok or result.timed_out or result.stderr or value != expected
            or type(value.get("schema_version")) is not int
            or type(value.get("package_assets")) is not int):
        return _failed("Core imports, package assets or bounded probe protocol failed")
    return {"result": "passed", "modules": value["modules"], "package_assets": 13}


def _selfcheck_probe(asset_root: Path, name: str, deadline: float) -> dict:
    scripts = {"designintelligence": "check-design-intelligence.py",
               "knowledge": "check-knowledge-freshness.py"}
    with tempfile.TemporaryDirectory(prefix="abr-install-checker-") as directory:
        result = _run_probe(asset_root, script_argv(asset_root / "scripts" / scripts[name],
                                                  "--selfcheck"), Path(directory), deadline)
    if not result.ok or result.timed_out or result.stderr or result.stdout.strip() != "selfcheck: OK":
        return _failed("Checker self-test exit, output or deadline failed")
    return {"result": "passed", "evidence": "checker selfcheck: OK; not project tests"}


def _backend_case(asset_root: Path, path: Path, expected: int, deadline: float) -> bool:
    args = script_argv(asset_root / "scripts/check-backend-quality.py", str(path), "--json")
    result = _run_probe(asset_root, args, path.parent, deadline)
    report = _object(result.stdout)
    violations = report.get("violations")
    return (not result.timed_out and result.exit_code == expected
            and result.ok == (expected == 0) and report.get("status") == "ok"
            and type(report.get("files_scanned")) is int and report["files_scanned"] == 1
            and type(report.get("total")) is int and report["total"] == expected
            and isinstance(violations, list) and len(violations) == expected)


def _backend_probe(asset_root: Path, directory: Path, deadline: float) -> dict:
    clean, unsafe = directory / "clean.ts", directory / "unsafe.ts"
    clean.write_text("export const count: number = 1;\n", encoding="utf-8")
    unsafe.write_text("console.log('installation fixture');\n", encoding="utf-8")
    if not (_backend_case(asset_root, clean, 0, deadline)
            and _backend_case(asset_root, unsafe, 1, deadline)):
        return _failed("Backend checker failed clean/rejected fixture protocol")
    return {"result": "passed", "files_scanned": 2, "rejected_violations": 1}


def _backend_fixture(asset_root: Path, deadline: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="abr-install-backend-") as name:
        return _backend_probe(asset_root, Path(name), deadline)


def _safe_probe(probe, *args) -> dict:
    try:
        return probe(*args)
    except (OSError, ValueError, RuntimeError) as exc:
        return _failed(f"Probe could not start or complete ({type(exc).__name__})")


def _not_executed() -> list[dict]:
    reasons = {
        "project_tests": "Installation probes do not collect or execute a project's tests.",
        "project_quality": "Use health --dir PROJECT and that project's mandatory gates.",
        "provider_execution": "No provider process or model task was started.",
        "optional_hub_authentication": "Optional Hub/SSO dependencies, identity and grants were not exercised.",
        "release_acceptance": "Installation diagnostics are not CI, platform or production acceptance.",
    }
    return [{"check": name, "result": "not_executed", "reason": reason}
            for name, reason in reasons.items()]


def installation_report(asset_root: Path) -> dict:
    """Check a usable installation without inventing absent test coverage."""
    asset_root = Path(asset_root).resolve()
    deadline = time.monotonic() + PROBE_SECONDS
    assets = _asset_status(asset_root)
    checks = {"distributed_assets": {"result": "passed"} if assets["healthy"]
              else _failed("Required distributed assets are missing, empty or oversized")}
    checks["runtime"] = _safe_probe(_runtime_probe, asset_root, deadline)
    for name in ("designintelligence", "knowledge"):
        checks[name + "_selfcheck"] = _safe_probe(_selfcheck_probe, asset_root, name, deadline)
    checks["backendquality_fixture"] = _safe_probe(_backend_fixture, asset_root, deadline)
    return {"schema_version": SCHEMA, "scope": "installation",
            "package": {"root": str(PACKAGE_ROOT), "version": VERSION, "kind": installation_kind()},
            "assets": assets, "checks": checks,
            "healthy": all(item["result"] == "passed" for item in checks.values()),
            "not_executed": _not_executed()}


def render_installation(report: dict) -> str:
    status = "AVAILABLE" if report["healthy"] else "ATTENTION NEEDED"
    lines = [f"# Installation Health: {status}", "", "Scope: installation availability only."]
    for name, check in report["checks"].items():
        lines.append(f"- {name}: {check['result']}" + (f" — {check['reason']}" if "reason" in check else ""))
    if report["assets"]["missing"]:
        lines.append("- Missing/invalid assets: " + ", ".join(report["assets"]["missing"]))
    lines.extend(f"- {item['check']}: not executed — {item['reason']}" for item in report["not_executed"])
    return "\n".join(lines)
