"""Configuration provenance, validation and explainability helpers.

The execution path keeps its import-time defaults for compatibility.  This
module gives operators a read-only view of the input documents, the
section/key winner after merging, and the derived values used by the runner.
It deliberately redacts secret-looking values before producing JSON.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from . import config
from . import user_dirs


_SECRET_KEY = re.compile(
    r"(?:secret|token|password|passwd|api[_-]?key|private[_-]?key|"
    r"credential|authorization|webhook)", re.IGNORECASE)
_EXPECTED_MAPPINGS = {
    "agent", "limits", "memory", "session", "logging", "input", "output",
    "evidence", "prompt", "validation", "commands", "project", "commit",
    "classifier", "runtime_policy", "device_fabric", "agent_ledger",
    "model_routing", "tui", "llm_pool",
    "validators",
}
_EXPECTED_SCALARS = {"config_version", "ui_scan_dir"}
_EXPECTED_TOP_LEVEL = _EXPECTED_MAPPINGS | _EXPECTED_SCALARS


def _read_yaml(path: Path, max_bytes: int) -> tuple[dict, list[str]]:
    """Read one bounded YAML document and return data plus diagnostics."""
    try:
        return config._read_config_document(path, max_bytes), []
    except ValueError as exc:
        return {}, [str(exc)]


def _document(name: str, path: Optional[Path], max_bytes: int, *, optional=False) -> dict:
    """Load a source document into an internal, JSON-friendly record."""
    record = {
        "name": name,
        "path": str(path) if path else None,
        "present": bool(path and path.exists()),
        "loaded": False,
        "data": {},
        "errors": [],
    }
    if path is None or (optional and not record["present"]):
        return record
    data, errors = _read_yaml(path, max_bytes)
    record["data"] = data
    record["errors"] = errors
    record["loaded"] = not errors
    return record


def _project_path(explicit: str = "") -> Optional[Path]:
    """Resolve the active project config or an explicitly inspected file."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    path = config._project_config_path()
    return path.resolve() if path else None


def load_documents(config_file: str = "", max_bytes: Optional[int] = None) -> dict:
    """Load project, user and packaged sources without creating directories.

    An explicit ``config_file`` is inspected in isolation.  This makes
    ``config check --file`` deterministic and suitable for CI validation.
    """
    limit = max_bytes or max(config.INPUT_MAX_BYTES, 1)
    project = _document("project", _project_path(config_file), limit)
    user_path = None if config_file else user_dirs.user_settings_path().resolve()
    user = _document("user", user_path, limit, optional=True)
    default_path = None if config_file else config._default_config_path()
    defaults = _document("defaults", default_path, limit, optional=True)
    return {"project": project, "user": user, "defaults": defaults}


def _version_diagnostics(data: dict, source: str) -> tuple[int, list[str], list[str]]:
    """Validate version metadata while retaining a legacy warning."""
    if "config_version" not in data:
        return 0, [], [f"{source}: config_version missing; treating as legacy v0"]
    version = data["config_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        return -1, [f"{source}: config_version must be an integer"], []
    if version < 0:
        return version, [f"{source}: config_version must be >= 0"], []
    if version > config.CONFIG_SCHEMA_VERSION:
        return version, [
            f"{source}: config_version {version} is newer than supported "
            f"version {config.CONFIG_SCHEMA_VERSION}"
        ], []
    return version, [], []


def validate_document(data: Any, source: str = "<memory>") -> dict:
    """Return fail-closed validation diagnostics for one config mapping."""
    if not isinstance(data, dict):
        return {"valid": False, "version": -1,
                "errors": [f"{source}: top-level value must be a mapping"],
                "warnings": []}
    version, errors, warnings = _version_diagnostics(data, source)
    unknown = sorted(
        (key for key in data if key not in _EXPECTED_TOP_LEVEL),
        key=lambda key: str(key),
    )
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        errors.append(f"{source}: unknown top-level key(s): {names}")
    for key in sorted(_EXPECTED_MAPPINGS & set(data)):
        if key != "validators" and not isinstance(data[key], dict):
            errors.append(f"{source}: section '{key}' must be a mapping")
        if key == "validators" and not isinstance(data[key], dict):
            errors.append(f"{source}: section 'validators' must be a mapping")
    errors.extend(_pool_reference_errors(data.get("llm_pool"), source))
    return {"valid": not errors, "version": version,
            "errors": errors, "warnings": warnings}


def _pool_reference_errors(value, source: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    if set(value) - {"config_file"}:
        return [f"{source}: llm_pool accepts only config_file"]
    if "config_file" in value and not isinstance(value["config_file"], str):
        return [f"{source}: llm_pool.config_file must be a string"]
    return []


def _source_node(value: Any, source: str) -> dict:
    node = {"source": source}
    if isinstance(value, dict):
        node["children"] = {key: _source_node(item, source)
                            for key, item in value.items()}
    return node


def _source_tree(project: dict, user: dict, defaults: Optional[dict] = None) -> dict:
    """Mirror section/key merging; nested mappings come from one winning key."""
    tree = {}
    for source, document in (("defaults", defaults or {}), ("user", user),
                             ("project", project)):
        for key, value in document.items():
            node = _source_node(value, source)
            previous = tree.get(key, {})
            if "children" in previous and "children" in node:
                node["children"] = {**previous["children"], **node["children"]}
                sources = {child["source"] for child in node["children"].values()}
                node["source"] = ("mixed" if len(sources) > 1 else
                                  next(iter(sources), source))
            tree[key] = node
    return tree


def _redact(value: Any, key: str = "") -> Any:
    """Return a JSON-safe copy with secret-looking mapping keys redacted."""
    if key and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


_RUNTIME_ATTRS = {
    "agent.bin": "AGENT_BIN",
    "agent.default_model": "AGENT_DEFAULT_MODEL",
    "agent.default_timeout": "AGENT_DEFAULT_TIMEOUT",
    "agent.default_workers": "AGENT_DEFAULT_WORKERS",
    "project.frontend_root": "PROJECT_FRONTEND_ROOT",
    "project.backend_root": "PROJECT_BACKEND_ROOT",
    "project.report_root": "PROJECT_REPORT_ROOT",
    "commit.prefix": "COMMIT_PREFIX_DEFAULT",
    "logging.max_bytes": "LOG_MAX_BYTES",
    "logging.backups": "LOG_BACKUP_COUNT",
    "logging.stream_output": "STREAM_OUTPUT",
    "session.max_bytes": "SESSION_MAX_BYTES",
    "session.line_max_bytes": "SESSION_LINE_MAX_BYTES",
    "output.max_bytes": "OUTPUT_MAX_BYTES",
    "input.max_bytes": "INPUT_MAX_BYTES",
    "evidence.max_bytes": "EVIDENCE_MAX_BYTES",
    "evidence.max_sources": "EVIDENCE_MAX_SOURCES",
    "prompt.max_bytes": "PROMPT_MAX_BYTES",
    "validation.output_max_bytes": "VALIDATION_OUTPUT_MAX_BYTES",
    "commands.timeout": "COMMAND_TIMEOUT",
    "commands.output_max_bytes": "COMMAND_OUTPUT_MAX_BYTES",
    "tui.journal_enabled": "TUI_JOURNAL_ENABLED",
    "tui.journal_max_bytes": "TUI_JOURNAL_MAX_BYTES",
    "memory.mode": "MEMORY_MODE",
    "memory.agent_names": "MEMORY_AGENT_NAMES",
    "memory.manifest_recent": "MEMORY_MANIFEST_RECENT",
    "memory.manifest_scan": "MEMORY_MANIFEST_SCAN",
    "memory.manifest_max_bytes": "MEMORY_MANIFEST_MAX_BYTES",
    "memory.read_max_bytes": "MEMORY_READ_MAX_BYTES",
    "limits.per_second": "RATE_LIMIT_PER_SECOND",
    "limits.burst": "RATE_LIMIT_BURST",
    "classifier.frontend_pipeline": "CLASSIFIER_FRONTEND_PIPELINE",
    "classifier.backend_pipeline": "CLASSIFIER_BACKEND_PIPELINE",
    "classifier.min_score": "CLASSIFIER_MIN_SCORE",
    "classifier.frontend_ratio": "CLASSIFIER_FRONTEND_RATIO",
    "classifier.backend_ratio": "CLASSIFIER_BACKEND_RATIO",
    "classifier.tie_policy": "CLASSIFIER_TIE_POLICY",
    "device_fabric.enabled": "DEVICE_FABRIC_ENABLED",
    "device_fabric.mode": "DEVICE_FABRIC_MODE",
    "device_fabric.security.mtls_required": "DEVICE_FABRIC_MTLS_REQUIRED",
    "device_fabric.security.short_lived_credentials":
        "DEVICE_FABRIC_SHORT_LIVED_CREDENTIALS",
}


def _runtime_values() -> dict:
    """Collect derived values actually held by the imported runner config."""
    resolved = {}
    for path, attr in _RUNTIME_ATTRS.items():
        value = getattr(config, attr, None)
        if isinstance(value, tuple):
            value = list(value)
        resolved[path] = _redact(value, path)
    resolved["runtime.data_dir"] = str(config.data_dir())
    resolved["validators.names"] = sorted(config.VALIDATORS)
    resolved["validators.specs"] = {
        name: {
            "scope": spec.scope,
            "judge": spec.judge,
            "requires_any": list(spec.requires_any),
        }
        for name, spec in sorted(config.VALIDATOR_SPECS.items())
    }
    return resolved


def _source_output(record: dict) -> dict:
    """Strip internal YAML data fields into a safe source summary."""
    return {key: record[key] for key in
            ("name", "path", "present", "loaded", "errors")}


def explain(config_file: str = "", max_bytes: Optional[int] = None) -> dict:
    """Build a complete, redacted configuration explanation."""
    documents = load_documents(config_file, max_bytes)
    project = documents["project"]["data"]
    user = documents["user"]["data"]
    defaults = documents["defaults"]["data"]
    checks = {
        name: validate_document(record["data"], record["name"])
        for name, record in documents.items() if record["loaded"]
    }
    errors = [error for record in documents.values() for error in record["errors"]]
    errors.extend(error for check in checks.values() for error in check["errors"])
    warnings = [warning for check in checks.values() for warning in check["warnings"]]
    merged = config._merge_config(project, config._merge_config(user, defaults))
    return {
        "schema_version": config.CONFIG_SCHEMA_VERSION,
        "status": "invalid" if errors else "ok",
        "precedence": ["CLI/runtime override", "project pi-batch.yaml",
                       "user settings.yaml", "packaged defaults", "built-in defaults"],
        "sources": [_source_output(record) for record in documents.values()],
        "validation": {"sources": checks, "errors": errors, "warnings": warnings},
        "provenance": _source_tree(project, user, defaults),
        "effective_file_config": _redact(merged),
        "resolved_runtime": _runtime_values(),
        "cli_override_contract": _cli_override_contract(),
    }


def _cli_override_contract() -> list[dict]:
    """Document CLI flags that supersede file-derived defaults."""
    return [
        {"flag": "--agent-bin", "config_path": "agent.bin"},
        {"flag": "--workers", "config_path": "agent.default_workers"},
        {"flag": "--timeout", "config_path": "agent.default_timeout"},
        {"flag": "--memory-mode", "config_path": "memory.mode"},
        {"flag": "--stream-output", "config_path": "logging.stream_output"},
        {"flag": "--data-dir", "config_path": "runtime.data_dir"},
        {"flag": "--provider", "config_path": "task/stage provider"},
        {"flag": "--validate", "config_path": "task/stage validator"},
    ]


def config_check(config_file: str = "", max_bytes: Optional[int] = None) -> dict:
    """Return only the validation-oriented result for automation."""
    report = explain(config_file, max_bytes)
    return {
        "valid": report["status"] == "ok",
        "schema_version": report["schema_version"],
        "sources": report["sources"],
        "validation": report["validation"],
    }
