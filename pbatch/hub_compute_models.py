"""Compute request bounds, durable schema and public task projection."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from .hub_store_helpers import canonical_json, identifier

COMPUTE_SCOPES = frozenset({"agent.devices:read", "agent.tasks:read",
                            "agent.tasks:write", "agent.tasks:cancel"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "lost"})
AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_compute_authority(
 turn_id TEXT PRIMARY KEY, authority_json TEXT NOT NULL);
"""
TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_compute_tasks(
 task_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
 session_id TEXT NOT NULL, instance_id TEXT NOT NULL, generation INTEGER NOT NULL,
 state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 retry_at REAL NOT NULL DEFAULT 0, data_json TEXT NOT NULL,
 UNIQUE(tenant,actor,idempotency_key));
CREATE INDEX IF NOT EXISTS hub_compute_page ON hub_compute_tasks(tenant,task_id);
CREATE INDEX IF NOT EXISTS hub_compute_pending ON hub_compute_tasks(retry_at,state);
"""


def integer(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def fields(value, allowed: set) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("unsupported compute fields")
    return value


def request_body(body: dict) -> dict:
    fields(body, {"argv", "workdir", "timeout", "resources", "requirements",
                  "target_device_id", "turn_id", "workspace"})
    argv = body.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 128:
        raise ValueError("argv must contain 1..128 strings")
    if any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]:
        raise ValueError("argv must contain valid arguments")
    if len(canonical_json(argv).encode()) > 16384:
        raise ValueError("argv exceeds 16 KiB")
    workdir = identifier(body.get("workdir", "."), "workdir", 512)
    if PurePosixPath(workdir).is_absolute() or ".." in workdir.split("/") or "\\" in workdir:
        raise ValueError("workdir must remain beneath the configured project root")
    requirements = fields(body.get("requirements", {}), {"os", "architecture", "runtimes"})
    result = {"argv": argv, "workdir": str(PurePosixPath(workdir)),
              "timeout": integer(body.get("timeout", 60), "timeout", 1, 3600),
              "resources": resource_request(body.get("resources", {})),
              "requirements": _requirements(requirements)}
    for name in ("target_device_id", "turn_id"):
        result[name] = identifier(body[name], name) if body.get(name, "") != "" else ""
    if "workspace" in body:
        from .hub_workspace import workspace_request
        if result["workdir"] != ".":
            raise ValueError("snapshot tasks execute at workdir '.'")
        result["workspace"] = workspace_request(body["workspace"])
    if len(canonical_json(result).encode("utf-8")) > 65536:
        raise ValueError("compute request exceeds 64 KiB")
    return result


def resource_request(value: dict) -> dict:
    resources = fields(value, {"cpu_cores", "memory_bytes", "gpu_count", "gpu_memory_bytes"})
    result = {
        "cpu_cores": integer(resources.get("cpu_cores", 1), "cpu_cores", 1, 256),
        "memory_bytes": integer(resources.get("memory_bytes", 0), "memory_bytes", 0, 16 * 1024**4)}
    gpu_count = integer(resources.get("gpu_count", 0), "gpu_count", 0, 16)
    gpu_memory = integer(resources.get("gpu_memory_bytes", 0), "gpu_memory_bytes", 0, 1024**4)
    if not gpu_count and gpu_memory:
        raise ValueError("gpu_memory_bytes requires a positive gpu_count")
    # Preserve historical CPU request fingerprints, including explicit zero defaults.
    if gpu_count:
        result.update(gpu_count=gpu_count, gpu_memory_bytes=gpu_memory)
    return result


def _requirements(value: dict) -> dict:
    runtimes = value.get("runtimes", [])
    if not isinstance(runtimes, list) or len(runtimes) > 32:
        raise ValueError("runtimes must be a bounded list")
    result = {"runtimes": sorted({identifier(item, "runtime", 64) for item in runtimes})}
    for name in ("os", "architecture"):
        result[name] = identifier(value[name], name, 64) if value.get(name, "") != "" else ""
    return result


def snapshot(principal) -> dict:
    return {"actor": principal.subject, "client_id": principal.client_id,
            "scopes": sorted(principal.scopes & COMPUTE_SCOPES),
            "instances": sorted(principal.compute_targets)}


def save_authority(conn, turn_id: str, authority: dict | None) -> None:
    conn.execute("INSERT INTO hub_compute_authority VALUES(?,?)",
                 (turn_id, canonical_json(authority or {})))


def task_record(row, *, private: bool = False) -> dict:
    result = json.loads(row["data_json"])
    result.update({key: row[key] for key in (
        "task_id", "actor", "session_id", "instance_id", "state", "created_at", "updated_at")})
    if private:
        result.update(tenant=row["tenant"], generation=row["generation"], retry_at=row["retry_at"])
    else:
        result = {key: value for key, value in result.items() if not key.startswith("_")}
    return result
