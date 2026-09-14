"""Per-turn local capabilities; provider children never receive Hub credentials."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import shlex
import socket
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

from .hub_client import HubError, _read_private_token
from .hub_compute_client import ComputeClient

MAX_BYTES = 65536
WORKSPACE_BYTES = 576 * 1024
OPERATIONS = {"devices", "submit", "status", "cancel", "snapshots", "snapshot-upload", "workspace-output"}


class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def handle(self):
        body = {}
        try:
            raw = self.rfile.readline(WORKSPACE_BYTES + 1)
            body = _decode(raw, maximum=WORKSPACE_BYTES)
            token = body.get("token", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, self.server.token):
                raise ValueError("invalid local capability")
            if body.get("operation") not in OPERATIONS:
                raise ValueError("unsupported compute tool")
            if body["operation"] != "snapshot-upload" and len(raw) > MAX_BYTES:
                raise ValueError("compute tool request exceeds limit")
            data = self.server.client.delegate(self.server.context, body["operation"],
                                                body.get("payload", {}), body.get("key", ""))
            response = {"data": data}
        except HubError as exc:
            response = {"error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            response = {"error": {"code": "tool_unavailable", "message": "compute tool unavailable"}}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
        maximum = WORKSPACE_BYTES if body.get("operation") == "workspace-output" else MAX_BYTES
        if len(encoded) > maximum - 1:
            encoded = b'{"error":{"code":"too_large","message":"tool response exceeds limit"}}'
        self.wfile.write(encoded + b"\n")


def _decode(raw: bytes, *, maximum=MAX_BYTES) -> dict:
    if len(raw) > maximum or not raw.endswith(b"\n"):
        raise ValueError("invalid bounded tool message")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool message must be an object")
    return value


@contextlib.contextmanager
def turn_tools(gateway, managed, turn_id, prompt):
    if not getattr(gateway.options, "compute_tools", False):
        yield prompt
        return
    context = {"instance_id": gateway.instance_id, "generation": gateway.generation,
               "session_id": managed.hub_id, "turn_id": turn_id}
    with tempfile.TemporaryDirectory(prefix="pb-compute-") as directory:
        endpoint = str(Path(directory) / "broker.sock")
        token = secrets.token_urlsafe(32)
        with socketserver.UnixStreamServer(endpoint, _Handler) as server:
            server.token, server.context = token, context
            server.client = ComputeClient(gateway.client)
            path = _capability(directory, endpoint, token, managed.hub_id)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            try:
                yield _tool_prompt(prompt, path)
            finally:
                server.shutdown()
                thread.join(timeout=12)


def _capability(directory, endpoint, token, session_id=""):
    path = Path(directory) / "capability.json"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"socket": endpoint, "token": token, "session_id": session_id}, stream)
    return path


def _tool_prompt(prompt, path):
    entry = Path(__file__).resolve().parent.parent / "pi-batch.py"
    program = [sys.executable, str(entry)] if entry.is_file() else [sys.executable, "-m", "pbatch"]
    command = " ".join(map(shlex.quote, [*program, "fleet", "compute", "--via", str(path)]))
    return ("Available compute tools for this active turn (subject to user and device grants):\n"
            f"{command} devices\n"
            f"{command} submit --argv-json '[\"python\",\"job.py\"]' --cpu-cores 1 --timeout 60\n"
            f"{command} snapshot-upload --directory . --file job.py --file input.json\n"
            f"{command} snapshots\n"
            f"{command} workspace-output TASK_ID --directory NEW_OUTPUT_DIRECTORY\n"
            f"{command} status TASK_ID\n{command} cancel TASK_ID\n"
            "Use devices to inspect project workspaces and resources before choosing a target. "
            "Devices returns up to 4 rows per page; repeat devices --after LAST_DEVICE_ID "
            "until an empty page to inspect all authorized project devices. "
            "Submission supports --target-device-id, --memory-mib, --runtime and --workdir. "
            "For NVIDIA CUDA tasks use --gpu-count and --gpu-memory-mib (minimum per card). "
            "Use --snapshot-id and repeat --output for exact output file paths; snapshot tasks use workdir . "
            "Upload only explicit --file paths (128 files, 64 KiB each, 256 KiB total). "
            "Retain Idempotency-Key for retry. Poll status to collect evidence. "
            "Never display or copy the capability file; it expires when this turn ends.\n\n" + prompt)


def broker_request(path: str, operation: str, payload: dict, key=""):
    capability = json.loads(_read_private_token(path))
    raw = json.dumps({"token": capability["token"], "operation": operation,
                      "payload": payload, "key": key}, ensure_ascii=False).encode() + b"\n"
    maximum = WORKSPACE_BYTES if operation == "snapshot-upload" else MAX_BYTES
    if len(raw) > maximum:
        raise ValueError("compute tool request exceeds limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(120)
        client.connect(capability["socket"])
        client.sendall(raw)
        with client.makefile("rb") as stream:
            maximum = WORKSPACE_BYTES if operation == "workspace-output" else MAX_BYTES
            response = _decode(stream.readline(maximum + 1), maximum=maximum)
    if "error" in response:
        error = response["error"]
        raise HubError(error["message"], code=error["code"])
    return response["data"]
