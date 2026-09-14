"""Structured compute requests, usable directly or through an active-turn broker."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from .hub_client import HubClient, HubError
from .hub_compute_client import ComputeClient


def parser():
    root = argparse.ArgumentParser(prog="pi-batch fleet compute")
    root.add_argument("--hub", default="")
    root.add_argument("--via", default="", help="private active-turn capability file")
    actions = root.add_subparsers(dest="operation", required=True)
    devices = actions.add_parser("devices")
    devices.add_argument("--project-id", default="")
    devices.add_argument("--after", default="", help="continue after this device ID")
    tasks = actions.add_parser("tasks")
    tasks.add_argument("--session-id", default="")
    for name in ("status", "cancel"):
        actions.add_parser(name).add_argument("task_id")
    submit = actions.add_parser("submit")
    submit.add_argument("--session-id", default="")
    submit.add_argument("--argv-json", required=True, help='JSON argv, e.g. ["python","job.py"]')
    submit.add_argument("--workdir", default=".")
    submit.add_argument("--timeout", type=int, default=60)
    submit.add_argument("--cpu-cores", type=int, default=1)
    submit.add_argument("--memory-mib", type=int, default=0)
    submit.add_argument("--gpu-count", type=int, default=0, help="NVIDIA CUDA GPU count (0..16)")
    submit.add_argument("--gpu-memory-mib", type=int, default=0,
                        help="minimum available memory per GPU in MiB")
    submit.add_argument("--os", default="")
    submit.add_argument("--architecture", default="")
    submit.add_argument("--runtime", action="append", default=[])
    submit.add_argument("--target-device-id", default="")
    submit.add_argument("--idempotency-key", default="")
    from .hub_workspace_cli import add_arguments
    add_arguments(actions, submit)
    actions.add_parser("tui")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = _run(args)
        if args.operation != "tui":
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (HubError, ValueError, RuntimeError, OSError) as exc:
        print(f"fleet compute: {exc}", file=sys.stderr)
        return 2


def _submission(args):
    from .hub_compute_models import request_body
    from .hub_workspace_cli import submission
    return request_body(submission(args, {"argv": json.loads(args.argv_json), "workdir": args.workdir,
                         "timeout": args.timeout, "target_device_id": args.target_device_id,
                         "resources": {"cpu_cores": args.cpu_cores, "memory_bytes": args.memory_mib * 1048576,
                                       "gpu_count": args.gpu_count,
                                       "gpu_memory_bytes": args.gpu_memory_mib * 1048576},
                         "requirements": {"os": args.os, "architecture": args.architecture,
                                          "runtimes": args.runtime}}))


def _run(args):
    from .hub_workspace_cli import OPERATIONS, run
    if args.operation in OPERATIONS:
        client = None if args.via else ComputeClient(HubClient.from_env(args.hub, timeout=120))
        return run(args, client)
    payload, key = {}, ""
    if args.operation == "submit":
        payload = _submission(args)
        key = args.idempotency_key or uuid.uuid4().hex
        print("Idempotency-Key: " + key, file=sys.stderr)
    elif args.operation in {"status", "cancel"}:
        payload = {"task_id": args.task_id}
    elif args.operation == "devices" and args.after:
        payload = {"after": args.after}
    if args.via:
        from .hub_compute_broker import broker_request
        if args.operation not in {"devices", "submit", "status", "cancel"}:
            raise ValueError("active-turn tools support devices, submit, status and cancel")
        return broker_request(args.via, args.operation, payload, key)
    client = ComputeClient(HubClient.from_env(args.hub))
    if args.operation == "devices":
        return client.devices(args.project_id, after=args.after)
    if args.operation == "tasks":
        return client.tasks(args.session_id)
    if args.operation == "status":
        return client.get(args.task_id)
    if args.operation == "cancel":
        return client.cancel(args.task_id)
    if args.operation == "tui":
        from .hub_compute_tui import ComputeTUI
        return ComputeTUI(client).run()
    if not args.session_id:
        raise ValueError("--session-id is required for direct submission")
    return client.submit(args.session_id, payload, key)
