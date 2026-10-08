"""Local protection and explicit bounded byte copies; never a restore/disposal executor."""
from __future__ import annotations

import json

from .task_center_io import display
from .task_retention_schema import KINDS
from . import task_retention_store as store


def add_parser(sub) -> None:
    parser = sub.add_parser("retention", help="显式登记/有界文件与目录字节副本；没有完整恢复或删除器")
    actions = parser.add_subparsers(dest="retention_action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--source", default="")
    listing.add_argument("--json", action="store_true")
    showing = actions.add_parser("show")
    showing.add_argument("resource_id")
    showing.add_argument("--source", default="")
    registering = actions.add_parser("register")
    registering.add_argument("--source", required=True)
    registering.add_argument("--path", required=True, help="absolute strict descendant of registered runtime only")
    registering.add_argument("--kind", required=True, choices=sorted(KINDS))
    registering.add_argument("--run-id", default="", help="optional actual pipeline checkpoint association")
    registering.add_argument("--confirm", action="store_true")
    pinning = actions.add_parser("pin", help="permanent protection marker; no unpin")
    extending = actions.add_parser("extend", help="increase review date; never reset execution limits")
    extending.add_argument("--until", required=True, help="timezone-aware ISO date after previous review and now")
    for action in (pinning, extending):
        action.add_argument("resource_id")
        action.add_argument("--if-version", required=True, type=int)
    copying = actions.add_parser("preserve", help="explicit private regular-file copy; no complete worktree archive")
    directory = actions.add_parser("preserve-directory", help="explicit bounded directory USTAR copy; no worktree/database recovery or restore")
    for action in (copying, directory):
        action.add_argument("resource_id")
        action.add_argument("--if-version", required=True, type=int)
        action.add_argument("--confirm", action="store_true")
    verifying = actions.add_parser("verify-copy", help="explicit full payload hash; no source/cleanup verification")
    verifying.add_argument("preservation_id")
    verifying.add_argument("--confirm", action="store_true")
    for action in (registering, pinning, extending, copying, directory):
        action.add_argument("--by", required=True, help="unauthenticated local audit label")
        action.add_argument("--reason", required=True)


def command(args, _cwd: str) -> int:
    try:
        if args.retention_action in {"list", "show"}:
            return _read(args)
        if args.retention_action in {"preserve", "preserve-directory", "verify-copy"}:
            return _copy(args)
        if args.retention_action == "register":
            value = store.register(args.source, args.path, args.kind, args.by, args.reason,
                                   confirm=args.confirm, run_id=args.run_id)
        else:
            value = store.protect(args.resource_id, args.by, args.reason, args.if_version,
                                  getattr(args, "until", ""))
        print(json.dumps({"resource_id": value["resource_id"], "version": value["version"],
            "pinned": value["pinned"], "review_at": value["review_at"], "cleanup_authority": "not_granted"},
            ensure_ascii=False, allow_nan=False))
        return 0  # metadata write success only; never disposal clearance
    except (ValueError, OSError, TypeError) as exc:
        print("ops retention: " + display(str(exc), 1024))
        return 2


def _copy(args) -> int:
    from .task_preservation import preserve, verify_copy
    if args.retention_action == "verify-copy":
        result = verify_copy(args.preservation_id, confirm=args.confirm)
    else:
        copier = preserve
        if args.retention_action == "preserve-directory":
            from .task_directory_preservation import preserve_directory
            copier = preserve_directory
        value = copier(args.resource_id, args.by, args.reason, args.if_version, confirm=args.confirm)
        result = {"preservation_id": value["preservation_id"], "resource_id": value["resource_id"],
            "payload": value["payload"], "directory_copy": value.get("directory_copy"), "created_at": value["created_at"],
            "verification": "full_copy_readback_at_creation_not_future_freshness_or_complete_archive",
            "cleanup_authority": "not_granted", "recoverable_archive_verified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0  # copy/hash success is not resource-disposal admission


def _read(args) -> int:
    from .task_center import snapshot
    view = snapshot(source_id=args.source)
    if args.retention_action == "show":
        value = next((row for row in view["resources"] if row["resource_id"] == args.resource_id), None)
        if value is None:
            print("resource not found in selected registered view; resource deletion was not inferred")
            return 1
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    elif args.json:
        print(json.dumps({"schema_version": 1, "resources": view["resources"], "diagnostics": view["diagnostics"]},
            ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for value in view["resources"]:
            print(value["resource_id"] + " " + value["project"] + " " + value["relative_path_display"])
            print("  " + value["policy"] + "; 保留；复核日期=" + value["review_at"] + "; 未授清场权")
        for note in view["diagnostics"]:
            print("NOTE: " + note)
    return 0
