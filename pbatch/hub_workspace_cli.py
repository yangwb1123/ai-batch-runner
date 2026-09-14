"""Explicit local file selection and safe recovery to a fresh directory."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .hub_workspace import WORKSPACE_HTTP_BYTES, validate_output
from .workspace_bundle import bundle_digest, validate_bundle

OPERATIONS = {"snapshots", "snapshot-upload", "workspace-output"}


def add_arguments(actions, submit):
    submit.add_argument("--snapshot-id", default="", help="immutable snapshot in this session")
    submit.add_argument("--output", action="append", default=[], help="explicit output file path in snapshot")
    listing = actions.add_parser("snapshots")
    listing.add_argument("--session-id", default="")
    listing.add_argument("--after", default="")
    upload = actions.add_parser("snapshot-upload")
    upload.add_argument("--session-id", default="")
    upload.add_argument("--idempotency-key", default="")
    source = upload.add_mutually_exclusive_group(required=True)
    source.add_argument("--directory", help="directory containing explicitly selected files")
    source.add_argument("--bundle-file", help="bounded pbatch.workspace.v1 JSON file")
    upload.add_argument("--file", action="append", default=[], help="file relative to --directory; repeatable")
    output = actions.add_parser("workspace-output")
    output.add_argument("task_id")
    output.add_argument("--directory", required=True, help="new directory to create; never overwrite")
    output.add_argument("--input-sha256", default="", help="require this input baseline hash")


def submission(args, body):
    if args.snapshot_id:
        body["workspace"] = {"snapshot_id": args.snapshot_id, "outputs": args.output}
    elif args.output:
        raise ValueError("--output requires --snapshot-id")
    return body


def _upload_bundle(args):
    if args.directory:
        if not args.file:
            raise ValueError("--directory requires explicit --file paths")
        from .workspace_files import pack_directory
        return pack_directory(args.directory, paths=args.file)
    if args.file:
        raise ValueError("--file requires --directory")
    with Path(args.bundle_file).open("rb") as stream:
        raw = stream.read(WORKSPACE_HTTP_BYTES + 1)
    if len(raw) > WORKSPACE_HTTP_BYTES:
        raise ValueError("bundle file exceeds upload limit")
    from .hub_http import _unique_object
    return validate_bundle(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))


def run(args, client=None):
    def call(operation, payload=None, key=""):
        if args.via:
            from .hub_compute_broker import broker_request
            return broker_request(args.via, operation, payload or {}, key)
        if operation == "snapshot-upload":
            return client.upload_snapshot(args.session_id, payload["bundle"], key)
        if operation == "snapshots":
            return client.snapshots(args.session_id, after=args.after)
        if operation == "status":
            return client.get(args.task_id)
        return client.workspace_output(args.task_id)

    if args.operation in {"snapshots", "snapshot-upload"} and not args.via and not args.session_id:
        raise ValueError("--session-id is required")
    if args.operation == "snapshots":
        return call("snapshots", {"after": args.after})
    if args.operation == "snapshot-upload":
        bundle = _upload_bundle(args)
        # A deterministic key makes retry after a lost ACK safe across CLI restarts.
        session_id = args.session_id
        if args.via:
            from .hub_client import _read_private_token
            session_id = json.loads(_read_private_token(args.via)).get("session_id", "")
            if not session_id:
                raise ValueError("workspace upload requires a current session-bound capability")
        key = args.idempotency_key or str(uuid.uuid5(uuid.NAMESPACE_URL,
              session_id + ":" + bundle_digest(bundle)))
        return call("snapshot-upload", {"bundle": bundle}, key)
    task = call("status", {"task_id": args.task_id})
    data = call("workspace-output", {"task_id": args.task_id})
    digest = task.get("workspace_result", {}).get("input_sha256", "")
    if args.input_sha256 and args.input_sha256 != digest:
        raise ValueError("task input baseline differs from --input-sha256")
    bundle = validate_output(data, digest, task.get("workspace", {}).get("outputs", []))
    from .workspace_files import restore_bundle
    restore_bundle(bundle, args.directory)
    return {"directory": str(Path(args.directory).absolute()), "input_sha256": digest,
            "output_sha256": bundle_digest(bundle), "file_count": len(bundle["files"])}
