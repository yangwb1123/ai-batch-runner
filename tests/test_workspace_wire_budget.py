"""Worst-case signed evidence bodies stay within the Fabric HTTP limit."""

from __future__ import annotations

import base64
import json

from pbatch.fabric_rpc import BODY_MAX_BYTES, build_request
from pbatch.workspace_bundle import SCHEMA, bundle_digest, validate_bundle


def test_max_unicode_workspace_and_control_output_fit_signed_http_body():
    paths = [f"{index:03d}" + "é" * 126 + "/" + "é" * 127 + "a"
             for index in range(128)]
    bundle = validate_bundle({
        "schema": SCHEMA,
        "files": [{"path": path, "content_b64": base64.b64encode(
            b"x" * 2048).decode("ascii")} for path in paths],
    })
    input_digest, output_digest = "a" * 64, bundle_digest(bundle)
    stdout = stderr = "\x00" * (32 * 1024)
    evidence = {
        "attempt_id": "attempt-1", "target_id": "local",
        "label": "task:wire-budget", "result": "passed", "exit_code": 0,
        "timed_out": False, "output_overflow": False,
        "stdout": stdout, "stderr": stderr,
        "stdout_bytes": len(stdout.encode()),
        "stderr_bytes": len(stderr.encode()),
        "input_digest": input_digest,
        "workspace": {"input_sha256": input_digest,
                      "output_sha256": output_digest, "bundle": bundle},
    }
    request = build_request("test-secret", "POST", "/evidence", {
        "device_id": "runner-1", "task_id": "task-1", "lease_id": "lease-1",
        "fencing_token": "fence-1", "evidence": evidence,
    })
    wire = json.dumps(request).encode("utf-8")

    assert len(bundle_digest(bundle)) == 64
    assert (evidence["input_digest"]
            == evidence["workspace"]["input_sha256"] == input_digest)
    assert evidence["workspace"]["output_sha256"] == output_digest
    assert max(len(path.encode()) for path in paths) == 511
    assert len(wire) <= BODY_MAX_BYTES
