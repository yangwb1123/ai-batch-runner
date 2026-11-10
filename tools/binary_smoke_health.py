#!/usr/bin/env python3
"""Strict installation-health evidence checks for the binary smoke harness."""

from __future__ import annotations


class SmokeFailure(RuntimeError):
    """A required executable contract failed."""


_SCHEMA = "abr.installation-health.v1"
_REQUIRED_CHECKS = frozenset({
    "runtime", "distributed_assets", "designintelligence_selfcheck",
    "knowledge_selfcheck", "backendquality_fixture",
})
_REQUIRED_MODULES = [
    "pbatch.cli", "pbatch.models", "pbatch.runner", "pbatch.pipeline", "pbatch.tui",
]
_REQUIRED_LIMITS = frozenset({
    "project_tests", "project_quality", "provider_execution",
    "optional_hub_authentication", "release_acceptance",
})
_REQUIRED_ASSETS = 9
_RUNTIME_ASSETS = 13
_BACKEND_FILES = 2
_BACKEND_REJECTIONS = 1


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _passed(row) -> bool:
    return isinstance(row, dict) and row.get("result") == "passed"


def _limit(row) -> bool:
    return (isinstance(row, dict) and row.get("result") == "not_executed"
            and isinstance(row.get("check"), str)
            and isinstance(row.get("reason"), str) and bool(row["reason"].strip()))


def _check_scope(report: dict) -> None:
    if (report.get("schema_version") != _SCHEMA
            or report.get("scope") != "installation"
            or report.get("healthy") is not True
            or "tests" in report or "score" in report):
        raise SmokeFailure("health: invalid installation scope or outcome")
    assets = report.get("assets")
    required = assets.get("required") if isinstance(assets, dict) else None
    if (not isinstance(assets, dict) or assets.get("healthy") is not True
            or assets.get("missing") != [] or not _count(required)
            or required < _REQUIRED_ASSETS):
        raise SmokeFailure("health: required installation assets missing or unknown")


def _check_mapping(checks) -> None:
    if not isinstance(checks, dict) or set(checks) != _REQUIRED_CHECKS:
        raise SmokeFailure("health: required installation probes missing or failed")
    if not all(_passed(row) for row in checks.values()):
        raise SmokeFailure("health: required installation probes missing or failed")


def _check_runtime(runtime: dict) -> None:
    if runtime.get("modules") != _REQUIRED_MODULES:
        raise SmokeFailure("health: installation probe evidence incomplete")
    assets = runtime.get("package_assets")
    if not _count(assets) or assets != _RUNTIME_ASSETS:
        raise SmokeFailure("health: installation probe evidence incomplete")


def _check_backend(backend: dict) -> None:
    files = backend.get("files_scanned")
    if not _count(files) or files != _BACKEND_FILES:
        raise SmokeFailure("health: installation probe evidence incomplete")
    rejected = backend.get("rejected_violations")
    if not _count(rejected) or rejected != _BACKEND_REJECTIONS:
        raise SmokeFailure("health: installation probe evidence incomplete")


def _check_limits(omitted) -> None:
    if not isinstance(omitted, list) or len(omitted) != len(_REQUIRED_LIMITS):
        raise SmokeFailure("health: installation limitations missing")
    if not all(_limit(row) for row in omitted):
        raise SmokeFailure("health: installation limitations missing")
    if {row.get("check") for row in omitted} != _REQUIRED_LIMITS:
        raise SmokeFailure("health: installation limitations missing")


def check_installation_health(report: dict) -> None:
    """A scoped install PASS must contain every required availability probe."""
    _check_scope(report)
    checks = report.get("checks")
    _check_mapping(checks)
    _check_runtime(checks["runtime"])
    _check_backend(checks["backendquality_fixture"])
    _check_limits(report.get("not_executed"))
