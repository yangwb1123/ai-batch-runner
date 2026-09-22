"""Pinned input distribution and independent output archival for Hub tasks."""

from __future__ import annotations

from .hub_errors import HubAccessError
from .hub_store_helpers import identifier
from .hub_workspace_store import WorkspaceStore
from .workspace_bundle import bundle_bytes, bundle_digest, validate_bundle, validate_outputs

WORKSPACE_HTTP_BYTES = 576 * 1024


def request_limit(path):
    parts = path.split("?", 1)[0].strip("/").split("/")
    if parts[:3] == ["api", "v1", "agent"]:
        parts = parts[3:]
    upload = len(parts) == 3 and parts[0] == "sessions" and parts[2] == "snapshots"
    return WORKSPACE_HTTP_BYTES if upload or parts == ["gateway", "compute"] else 65536


def workspace_request(value):
    if not isinstance(value, dict) or set(value) != {"snapshot_id", "outputs"}:
        raise ValueError("workspace requires snapshot_id and outputs")
    return {"snapshot_id": identifier(value["snapshot_id"], "snapshot_id", 128),
            "outputs": validate_outputs(value["outputs"])}


def archive_client(connection):
    from .hub_vault_workspace import VaultWorkspaceArchive
    if connection is None or connection.archive is None:
        raise HubAccessError("workspace_unavailable", "workspace requires configured Vault", 503)
    return VaultWorkspaceArchive(connection.archive)


class HubWorkspace:
    def __init__(self, compute):
        self.compute = compute
        self.store = WorkspaceStore(compute.store.hub)

    def upload(self, principal, session, bundle, key, *, turn_id=""):
        bundle = validate_bundle(bundle)
        client = archive_client(self.compute.archive_connection(principal.tenant_id))
        row = self.store.reserve(principal, session, key, bundle_digest(bundle),
                                 len(bundle_bytes(bundle)), len(bundle["files"]), turn_id=turn_id)
        if row["artifact_ref"]:
            return row
        try:
            ref = client.put(row["snapshot_id"], bundle)
        except RuntimeError as exc:
            raise HubAccessError("workspace_unavailable", "Vault workspace upload unavailable", 503) from exc
        return self.store.ready(principal.tenant_id, row["snapshot_id"], ref)

    def bind(self, tenant, session, payload, device_id=""):
        if "workspace" not in payload:
            return None
        archive_client(self.compute.archive_connection(tenant, device_id))
        row = self.store.get(tenant, session["session_id"], payload["workspace"]["snapshot_id"])
        if row["project_id"] != session["project_id"]:
            raise HubAccessError("workspace_project_changed", "snapshot project no longer matches", 409)
        return row

    @staticmethod
    def dispatch(task, connection):
        if "workspace" not in task:
            return {}
        snapshot = task["_workspace_input"]
        bundle = archive_client(connection).read(snapshot["artifact_ref"])
        if bundle_digest(bundle) != snapshot["sha256"]:
            raise ValueError("workspace input digest mismatch")
        return {"workspace": {"bundle": bundle, "sha256": snapshot["sha256"],
                              "outputs": task["workspace"]["outputs"]}}

    @staticmethod
    def observe(task, remote, state):
        if "workspace" not in task:
            return state, {}
        result = dict(task["workspace_result"])
        if state not in {"completed", "failed", "cancelled", "lost"}:
            return state, {}
        output = (remote.get("evidence") or {}).get("workspace")
        if not output:
            result["state"] = "unavailable"
            if state == "completed":
                result["state"] = "failed"
                return "failed", {"workspace_result": result, "error": "workspace_evidence_missing"}
            return state, {"workspace_result": result}
        try:
            bundle = validate_output(output, result["input_sha256"], task["workspace"]["outputs"])
        except (ValueError, TypeError, KeyError):
            if state != "completed" and _empty_failure(output, result["input_sha256"]):
                result["state"] = "unavailable"
                return state, {"workspace_result": result}
            result["state"] = "failed"
            final = "failed" if state == "completed" else state
            return final, {"workspace_result": result, "error": "workspace_evidence_invalid"}
        result.update(output_sha256=bundle_digest(bundle), state="pending")
        return state, {"workspace_result": result, "_workspace_output": bundle}

    @staticmethod
    def archive(task, connection):
        if "workspace" not in task or task["workspace_result"]["state"] in {"ready", "unavailable", "failed"}:
            return {}
        result = dict(task["workspace_result"])
        if "_workspace_output" not in task:
            result["state"] = "unavailable"
            return {"workspace_result": result}
        ref = archive_client(connection).put(task["task_id"], task["_workspace_output"])
        result.update(state="ready", artifact_ref=ref)
        return {"workspace_result": result, "_workspace_output": None}

    def download(self, tenant, task):
        result = task.get("workspace_result", {})
        if result.get("state") != "ready":
            raise HubAccessError("workspace_not_ready", "task output is not archived", 409)
        try:
            bundle = archive_client(self.compute.archive_connection(tenant, task.get("device_id", ""))).read(
                result["artifact_ref"])
        except RuntimeError as exc:
            raise HubAccessError("workspace_unavailable", "Vault workspace download unavailable", 503) from exc
        if bundle_digest(bundle) != result["output_sha256"]:
            raise ValueError("workspace output digest mismatch")
        return {"input_sha256": result["input_sha256"], "output_sha256": result["output_sha256"],
                "bundle": bundle}


def validate_output(output, input_digest, paths):
    if not isinstance(output, dict) or set(output) != {"input_sha256", "output_sha256", "bundle"}:
        raise ValueError("invalid workspace evidence")
    bundle = validate_bundle(output["bundle"])
    if output["input_sha256"] != input_digest or output["output_sha256"] != bundle_digest(bundle):
        raise ValueError("workspace evidence digest mismatch")
    if [row["path"] for row in bundle["files"]] != paths:
        raise ValueError("workspace output paths differ from request")
    return bundle


def _empty_failure(output, input_digest):
    try:
        validate_output(output, input_digest, [])
        return True
    except (ValueError, TypeError, KeyError):
        return False
