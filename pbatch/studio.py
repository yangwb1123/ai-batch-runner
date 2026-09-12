"""Structured project discovery/design rehearsal.

Project Studio turns business discussion, domain modeling, UX, architecture,
technology choices and implementation planning into a validated artifact DAG.
It is deliberately read-only unless ``init`` or an explicit ``--out`` is used.

    pi-batch studio plan --project DIR --spec studio.yaml --json
    pi-batch studio validate --project DIR --spec studio.yaml --json
    pi-batch studio rehearse --project DIR --spec studio.yaml --budget-minutes 240
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from . import config
from .config import log, yaml
from .json_schema import SchemaError, load_json_file, validate
from .text_io import read_text_bounded

SCHEMA_VERSION = "studio/v1"
TOP_FIELDS = {"schema_version", "name", "brief", "constraints", "artifacts", "stages", "gates"}
ARTIFACT_FIELDS = {"id", "type", "path", "schema", "required", "evidence_required"}
STAGE_FIELDS = {"id", "role", "artifacts", "depends_on", "effort", "optional"}
GATE_FIELDS = {"id", "description", "command", "timeout"}
READY_STATUSES = {"approved", "verified"}


class StudioSpecError(ValueError):
    """The project studio contract is malformed or unsafe."""


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise StudioSpecError(f"{label} must be a mapping")
    return value


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StudioSpecError(f"{label} must be a non-empty string")
    return value.strip()


def _strings(value, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise StudioSpecError(f"{label} must be a non-empty string list")
    return [item.strip() for item in value]


def _relative(value: str, label: str) -> str:
    value = _text(value, label)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise StudioSpecError(f"{label} must stay inside the project root")
    return path.as_posix()


def _artifact(value, index: int) -> dict:
    label = f"artifacts[{index}]"
    value = _mapping(value, label)
    unknown = sorted(set(value) - ARTIFACT_FIELDS)
    if unknown:
        raise StudioSpecError(f"{label} unknown field(s): {', '.join(unknown)}")
    required = _strings(value.get("required"), f"{label}.required")
    evidence_required = value.get("evidence_required", True)
    if not isinstance(evidence_required, bool):
        raise StudioSpecError(f"{label}.evidence_required must be boolean")
    schema = value.get("schema", "")
    if schema:
        schema = _relative(schema, f"{label}.schema")
    return {
        "id": _text(value.get("id", ""), f"{label}.id"),
        "type": _text(value.get("type", ""), f"{label}.type"),
        "path": _relative(value.get("path", ""), f"{label}.path"),
        "schema": schema,
        "required": required,
        "evidence_required": evidence_required,
    }


def _stage(value, index: int) -> dict:
    label = f"stages[{index}]"
    value = _mapping(value, label)
    unknown = sorted(set(value) - STAGE_FIELDS)
    if unknown:
        raise StudioSpecError(f"{label} unknown field(s): {', '.join(unknown)}")
    effort = value.get("effort", 30)
    if isinstance(effort, bool) or not isinstance(effort, (int, float)) or effort <= 0:
        raise StudioSpecError(f"{label}.effort must be positive minutes")
    optional = value.get("optional", False)
    if not isinstance(optional, bool):
        raise StudioSpecError(f"{label}.optional must be boolean")
    artifacts = _strings(value.get("artifacts"), f"{label}.artifacts")
    if not artifacts:
        raise StudioSpecError(f"{label}.artifacts must not be empty")
    return {"id": _text(value.get("id", ""), f"{label}.id"),
            "role": _text(value.get("role", ""), f"{label}.role"),
            "artifacts": artifacts,
            "depends_on": _strings(value.get("depends_on"), f"{label}.depends_on"),
            "effort": float(effort), "optional": optional}


def _gate(value, index: int) -> dict:
    label = f"gates[{index}]"
    value = _mapping(value, label)
    unknown = sorted(set(value) - GATE_FIELDS)
    if unknown:
        raise StudioSpecError(f"{label} unknown field(s): {', '.join(unknown)}")
    timeout = value.get("timeout", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise StudioSpecError(f"{label}.timeout must be positive")
    return {"id": _text(value.get("id", ""), f"{label}.id"),
            "description": _text(value.get("description", value.get("id", "")), f"{label}.description"),
            "command": _text(value.get("command", ""), f"{label}.command"),
            "timeout": float(timeout)}


def _topological(stages: list[dict]) -> list[str]:
    by_id = {stage["id"]: stage for stage in stages}
    if len(by_id) != len(stages):
        raise StudioSpecError("stages contains duplicate ids")
    for stage in stages:
        missing = sorted(set(stage["depends_on"]) - set(by_id))
        if missing:
            raise StudioSpecError(f"{stage['id']} depends on unknown stage {missing[0]}")
    visiting, visited, order = set(), set(), []

    def visit(stage_id: str) -> None:
        if stage_id in visiting:
            raise StudioSpecError(f"stage dependency cycle at: {stage_id}")
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for dependency in by_id[stage_id]["depends_on"]:
            visit(dependency)
        visiting.remove(stage_id)
        visited.add(stage_id)
        order.append(stage_id)

    for stage in stages:
        visit(stage["id"])
    return order


def _read_spec(path: str) -> tuple[Path, dict]:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise StudioSpecError(f"studio spec is not a regular file: {candidate}")
    try:
        data = yaml.safe_load(read_text_bounded(candidate, config.INPUT_MAX_BYTES, "studio spec")) or {}
    except Exception as exc:
        raise StudioSpecError(f"cannot read studio spec: {exc}") from exc
    data = _mapping(data, "studio spec")
    unknown = sorted(set(data) - TOP_FIELDS)
    if unknown:
        raise StudioSpecError(f"studio spec unknown field(s): {', '.join(unknown)}")
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise StudioSpecError(f"unsupported studio schema: {data.get('schema_version')!r}")
    return candidate, data


def _validate_spec_graph(artifacts: list[dict], stages: list[dict]) -> list[str]:
    artifact_ids = {item["id"] for item in artifacts}
    if len(artifact_ids) != len(artifacts):
        raise StudioSpecError("artifacts contains duplicate ids")
    for stage in stages:
        unknown = sorted(set(stage["artifacts"]) - artifact_ids)
        if unknown:
            raise StudioSpecError(f"{stage['id']} references unknown artifact {unknown[0]}")
    _topological(stages)
    referenced = {artifact for stage in stages for artifact in stage["artifacts"]}
    return sorted(artifact_ids - referenced)


def load_spec(path: str) -> dict:
    candidate, data = _read_spec(path)
    artifacts = [_artifact(item, index) for index, item in enumerate(data.get("artifacts", []))]
    stages = [_stage(item, index) for index, item in enumerate(data.get("stages", []))]
    gates = [_gate(item, index) for index, item in enumerate(data.get("gates", []))]
    unreferenced = _validate_spec_graph(artifacts, stages)
    return {"schema_version": SCHEMA_VERSION,
            "name": _text(data.get("name", candidate.stem), "studio.name"),
            "brief": str(data.get("brief", "")).strip(),
            "constraints": _strings(data.get("constraints"), "studio.constraints"),
            "artifacts": artifacts, "stages": stages, "gates": gates,
            "unreferenced_artifacts": unreferenced}


def _project_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise StudioSpecError(f"project is not a directory: {root}")
    return root


def _artifact_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StudioSpecError(f"artifact path escapes project: {relative}") from exc
    return path


def _load_artifact(root: Path, spec: dict) -> tuple[dict | None, list[str]]:
    path = _artifact_path(root, spec["path"])
    if not path.is_file():
        return None, [f"missing artifact: {spec['id']} ({spec['path']})"]
    try:
        text = read_text_bounded(path, config.INPUT_MAX_BYTES, f"artifact {spec['id']}")
        data = json.loads(text) if path.suffix.lower() == ".json" else (yaml.safe_load(text) or {})
    except Exception as exc:
        return None, [f"invalid artifact {spec['id']}: {exc}"]
    if not isinstance(data, dict):
        return None, [f"artifact {spec['id']} must be an object"]
    errors = []
    for field in spec["required"]:
        if field not in data:
            errors.append(f"artifact {spec['id']} missing field: {field}")
    if data.get("artifact_type") and data["artifact_type"] != spec["type"]:
        errors.append(f"artifact {spec['id']} type mismatch: {data['artifact_type']!r}")
    if spec["evidence_required"] and not isinstance(data.get("evidence"), list):
        errors.append(f"artifact {spec['id']} needs an evidence list")
    if spec["schema"]:
        errors.extend(_schema_errors(root, spec, data))
    return data, errors


def _schema_errors(root: Path, spec: dict, data: dict) -> list[str]:
    schema_path = _artifact_path(root, spec["schema"])
    try:
        schema = load_json_file(schema_path, f"schema {spec['id']}")
        return [f"artifact {spec['id']} schema: {error}" for error in validate(data, schema)]
    except (OSError, SchemaError, ValueError) as exc:
        return [f"artifact {spec['id']} schema unavailable: {exc}"]


def _artifact_report(root: Path, spec: dict) -> dict:
    data, errors = _load_artifact(root, spec)
    status = str((data or {}).get("status", "proposed"))
    verification = (data or {}).get("verification") or {}
    if status == "verified" and verification.get("passed") is not True:
        errors.append(f"artifact {spec['id']} claims verified without verification.passed=true")
    if errors:
        state = "invalid" if data is not None else "missing"
    else:
        state = status if status in {"proposed", "approved", "implemented", "verified", "blocked"} else "invalid"
    return {"id": spec["id"], "type": spec["type"], "path": spec["path"],
            "state": state, "errors": errors, "status": status,
            "evidence_count": len((data or {}).get("evidence", [])),
            "links_count": len((data or {}).get("links", [])) if data else 0}


def _run_gate(gate: dict, root: Path) -> dict:
    try:
        result = subprocess.run(gate["command"], cwd=str(root), shell=True, text=True,
                                capture_output=True, timeout=gate["timeout"])
        return {"id": gate["id"], "passed": result.returncode == 0, "exit": result.returncode,
                "stdout": (result.stdout or "")[-2000:], "stderr": (result.stderr or "")[-2000:]}
    except subprocess.TimeoutExpired:
        return {"id": gate["id"], "passed": False, "exit": None, "timeout": True}
    except OSError as exc:
        return {"id": gate["id"], "passed": False, "exit": None, "error": str(exc)}


def _stage_report(spec: dict, artifacts: dict[str, dict]) -> list[dict]:
    by_id = {stage["id"]: stage for stage in spec["stages"]}
    states = {}
    result = []
    for stage_id in _topological(spec["stages"]):
        stage = by_id[stage_id]
        own_state = _own_stage_state(stage, artifacts)
        dependencies_ready = all(states[item] == "ready" for item in stage["depends_on"])
        state = own_state if dependencies_ready else "waiting"
        states[stage_id] = state
        result.append({"id": stage_id, "role": stage["role"], "artifacts": stage["artifacts"],
                       "depends_on": stage["depends_on"], "effort": stage["effort"],
                       "optional": stage["optional"], "state": state})
    return result


def _own_stage_state(stage: dict, artifacts: dict[str, dict]) -> str:
    states = [artifacts[item]["state"] for item in stage["artifacts"]]
    if any(state in {"missing", "invalid"} for state in states):
        return "blocked" if any(artifacts[item]["errors"] for item in stage["artifacts"]) else "pending"
    if all(state in READY_STATUSES for state in states):
        return "ready"
    return "proposed"


def _waves(stages: list[dict], reports: list[dict]) -> dict:
    by_id = {stage["id"]: stage for stage in stages}
    report_by_id = {stage["id"]: stage for stage in reports}
    waves = {}
    for stage_id in _topological(stages):
        stage = by_id[stage_id]
        if report_by_id[stage_id]["state"] == "ready":
            continue
        dependencies = [dependency for dependency in stage["depends_on"] if dependency in waves]
        waves[stage_id] = 0 if not dependencies else max(waves[item] for item in dependencies) + 1
    grouped = {}
    for stage_id, wave in waves.items():
        grouped.setdefault(wave, []).append(stage_id)
    items = []
    for wave in sorted(grouped):
        for stage_id in sorted(grouped[wave]):
            stage = by_id[stage_id]
            items.append({"id": stage_id, "role": stage["role"], "wave": wave,
                          "effort_minutes": stage["effort"], "state": report_by_id[stage_id]["state"]})
    return {"items": items, "waves": len(grouped), "pending": len(items),
            "serial_minutes": round(sum(item["effort_minutes"] for item in items), 1),
            "critical_path_minutes": round(sum(max((item["effort_minutes"] for item in items if item["wave"] == wave), default=0) for wave in grouped), 1)}


def build_report(project: str, spec_path: str, run_gates: bool = False,
                 concurrency: int = 1, budget_minutes: Optional[float] = None) -> dict:
    root = _project_root(project)
    spec = load_spec(spec_path)
    artifacts = {item["id"]: _artifact_report(root, item) for item in spec["artifacts"]}
    stages = _stage_report(spec, artifacts)
    gates = [_run_gate(gate, root) for gate in spec["gates"]] if run_gates else []
    counts = {state: sum(item["state"] == state for item in artifacts.values())
              for state in ("missing", "invalid", "proposed", "approved", "implemented", "verified", "blocked")}
    total = len(artifacts)
    existing = total - counts["missing"]
    ready = sum(item["state"] in READY_STATUSES for item in artifacts.values())
    hard_failures = [item["id"] for item in artifacts.values() if item["state"] in {"missing", "invalid", "blocked"}]
    hard_failures.extend(f"gate:{gate['id']}" for gate in gates if not gate["passed"])
    plan = _waves(spec["stages"], stages)
    effective_minutes = (plan["critical_path_minutes"] if concurrency > 1
                         else plan["serial_minutes"])
    rehearsal = {"mode": "dry_run", "concurrency": concurrency,
                 "budget_minutes": budget_minutes,
                 "budget_status": "not_set" if budget_minutes is None else
                 ("within" if effective_minutes <= budget_minutes else "exceeded"),
                 "critical_path_minutes": effective_minutes}
    return {"schema_version": SCHEMA_VERSION, "project": str(root), "spec": spec["name"],
            "brief": spec["brief"], "constraints": spec["constraints"],
            "artifacts": list(artifacts.values()), "stages": stages, "plan": plan,
            "gates": gates, "metrics": {"artifacts": total, "existing": existing,
            "ready": ready, "readiness": round(ready / total, 3) if total else 0.0,
            "counts": counts, "hard_failures": hard_failures,
            "implementation_ready": not hard_failures and ready == total},
            "rehearsal": rehearsal, "unreferenced_artifacts": spec["unreferenced_artifacts"],
            "_note": "Artifact completeness and traceability are deterministic; design quality still needs independent review."}


def _write(path: str, payload: dict) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def _print(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    metrics = report["metrics"]
    print(f"# Project Studio: {report['spec']}")
    print(f"project: {report['project']}")
    print(f"artifacts: {metrics['artifacts']} | ready: {metrics['ready']} | "
          f"readiness: {metrics['readiness']:.1%} | implementation_ready: {metrics['implementation_ready']}")
    print(f"plan: {report['plan']['pending']} pending / {report['plan']['waves']} waves")
    for item in report["plan"]["items"]:
        print(f"- W{item['wave']} {item['id']} ({item['role']}) [{item['state']}]")
    for failure in metrics["hard_failures"]:
        print(f"- BLOCKED: {failure}")


def _template(path: str) -> None:
    content = """schema_version: studio/v1
name: project-studio
brief: ""
constraints: []
artifacts:
  - id: requirements
    type: requirements
    path: docs/design/requirements.json
    required: [goal, personas, flows, acceptance, non_goals]
    evidence_required: true
stages:
  - id: requirements
    role: requirement
    artifacts: [requirements]
    depends_on: []
    effort: 30
    optional: false
gates: []
"""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise StudioSpecError(f"refusing to overwrite existing file: {target}")
    target.write_text(content, encoding="utf-8")


def studio_main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-batch.py studio", description="Project Studio artifact DAG validation and rehearsal")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write a starter studio/v1 contract")
    init.add_argument("--out", required=True)
    for name in ("plan", "validate", "rehearse"):
        child = sub.add_parser(name, help=f"{name} without code mutation")
        child.add_argument("--project", required=True)
        child.add_argument("--spec", required=True)
        child.add_argument("--json", action="store_true")
        child.add_argument("--out", default="")
        child.add_argument("--run-gates", action="store_true")
        child.add_argument("--concurrency", type=int, default=1)
        child.add_argument("--budget-minutes", type=float, default=None)
        child.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "init":
        try:
            _template(args.out)
        except StudioSpecError as exc:
            parser.error(str(exc))
        print(f"studio contract initialized: {Path(args.out).expanduser()}")
        return 0
    if args.concurrency < 1 or (args.budget_minutes is not None and args.budget_minutes < 0):
        parser.error("--concurrency must be positive and --budget-minutes must be non-negative")
    try:
        report = build_report(args.project, args.spec, args.run_gates,
                              args.concurrency, args.budget_minutes)
        _write(args.out, report)
        _print(report, args.json)
    except (StudioSpecError, OSError, ValueError) as exc:
        log.error("studio: %s", exc)
        if args.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.strict and not report["metrics"]["implementation_ready"]:
        return 1
    if args.command == "rehearse" and report["rehearsal"]["budget_status"] == "exceeded":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(studio_main())
