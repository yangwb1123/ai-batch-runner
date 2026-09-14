"""Fabric workspace DTO validation and assignment-bound result evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re

from .workspace_bundle import (
    WorkspaceBundleError,
    bundle_bytes,
    bundle_digest,
    validate_bundle,
    validate_outputs,
)

RUNNER_CAPABILITY = "task.workspace.v1"
HUB_CAPABILITY = "hub.compute.workspace.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def workspace_filesystem_supported() -> bool:
    """True only when the host offers dir-fd, no-follow filesystem primitives."""
    if not (os.name == "posix" and getattr(os, "O_NOFOLLOW", 0)
            and getattr(os, "O_DIRECTORY", 0)):
        return False
    dir_fd_calls = {os.open, os.mkdir, os.stat}
    return (dir_fd_calls <= getattr(os, "supports_dir_fd", set())
            and os.scandir in getattr(os, "supports_fd", set()))


def normalize_workspace_request(value: object) -> dict:
    """Validate the complete input bundle and expected output allowlist."""
    if not isinstance(value, dict) or set(value) != {"bundle", "sha256", "outputs"}:
        raise ValueError("workspace must contain bundle, sha256, and outputs")
    bundle = validate_bundle(value["bundle"])
    digest = value.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("workspace.sha256 must be a lowercase SHA-256 digest")
    if bundle_digest(bundle) != digest:
        raise ValueError("workspace input digest does not match bundle")
    outputs = validate_outputs(value.get("outputs"))
    return {"bundle": bundle, "sha256": digest, "outputs": outputs}


def workspace_request_digest(value: dict) -> str:
    """Hash content identity without copying the large bundle into task keys."""
    normalized = normalize_workspace_request(value)
    raw = json.dumps({"sha256": normalized["sha256"],
                      "outputs": normalized["outputs"]},
                     ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def workspace_metadata(value: object) -> dict | None:
    """Return only non-sensitive input metadata for submit/status projections."""
    if not isinstance(value, dict):
        return None
    return {"sha256": value.get("sha256", ""),
            "outputs": list(value.get("outputs", []))}


def workspace_task_projection(task: dict, view: dict, *,
                              include_input: bool,
                              include_result: bool) -> dict:
    """Add private workspace input/result only to the requested task view."""
    workspace = task.get("workspace")
    if isinstance(workspace, dict):
        view["workspace"] = workspace_metadata(workspace)
        if include_input:
            view["workspace"]["bundle"] = workspace.get("bundle")
    evidence = view.get("evidence")
    if not include_result and isinstance(evidence, dict):
        result = evidence.get("workspace")
        if isinstance(result, dict):
            evidence = dict(evidence)
            result = dict(result)
            result.pop("bundle", None)
            evidence["workspace"] = result
            view["evidence"] = evidence
    return view


def workspace_evidence_error(task_workspace: object, evidence: object) -> str:
    """Validate the output bundle and bind it to the admitted task input."""
    if not isinstance(task_workspace, dict):
        return _unexpected_workspace_evidence(evidence)
    if not isinstance(evidence, dict):
        return "workspace task evidence must be an object"
    result = evidence.get("workspace")
    if result is None and evidence.get("result") in ("failed", "inconclusive"):
        return ""
    if not isinstance(result, dict) or set(result) != {
            "input_sha256", "output_sha256", "bundle"}:
        return "workspace evidence has an invalid shape"
    expected_input = str(task_workspace.get("sha256", ""))
    if (result.get("input_sha256") != expected_input
            or evidence.get("input_digest") != expected_input):
        return "workspace evidence input digest does not match task"
    return _workspace_output_error(task_workspace, evidence, result)


def _unexpected_workspace_evidence(evidence: object) -> str:
    return ("unexpected workspace evidence on a non-workspace task"
            if isinstance(evidence, dict) and "workspace" in evidence else "")


def _workspace_output_error(task_workspace: dict, evidence: dict,
                            result: dict) -> str:
    try:
        bundle = validate_bundle(result["bundle"])
        digest = bundle_digest(bundle)
        expected_paths = validate_outputs(task_workspace.get("outputs"))
    except (WorkspaceBundleError, TypeError, ValueError):
        return "workspace evidence output bundle is invalid"
    if result.get("output_sha256") != digest:
        return "workspace evidence output digest does not match bundle"
    actual_paths = [item["path"] for item in bundle["files"]]
    if evidence.get("result") == "passed" and actual_paths != expected_paths:
        return "workspace evidence output paths do not match request"
    if actual_paths and actual_paths != expected_paths:
        return "workspace evidence output paths do not match request"
    return ""


def workspace_input_bytes(value: object) -> bytes:
    """Return canonical input bytes for admission checks and size accounting."""
    return bundle_bytes(normalize_workspace_request(value)["bundle"])


__all__ = ["HUB_CAPABILITY", "RUNNER_CAPABILITY", "normalize_workspace_request",
           "workspace_evidence_error", "workspace_filesystem_supported",
           "workspace_input_bytes", "workspace_metadata",
           "workspace_request_digest", "workspace_task_projection"]
