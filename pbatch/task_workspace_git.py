"""Fixed read-only Git argv with a sanitized environment and a shared deadline."""
from __future__ import annotations

import os
import re
import shutil
import time

from .process_exec import run_bounded
from .task_center_io import display

OID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
OUTPUT_CAP = 64 * 1024
MAX_PATH_REFS = 8


def environment() -> dict:
    # No inherited GIT_*, agent settings, config-count injection, SSH or pager commands.
    return {"PATH": os.defpath, "LC_ALL": "C", "HOME": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ATTR_NOSYSTEM": "1"}


def command(layout: dict, args: list[str], deadline: float, allow_detached: bool = False) -> str | None:
    remaining = deadline - time.monotonic()
    binary = shutil.which("git", path=os.defpath)
    if remaining <= 0 or binary is None:
        raise ValueError("Git unavailable or shared workspace-inspection deadline exhausted")
    cmd = [binary, "--no-optional-locks", "--git-dir=" + str(layout["git_dir"]),
           "--work-tree=" + str(layout["workspace"]), "-c", "core.fsmonitor=false",
           "-c", "core.untrackedCache=false", "-c", "core.hooksPath=" + os.devnull,
           "-c", "core.attributesFile=" + os.devnull, "-c", "core.excludesFile=" + os.devnull,
           "-c", "protocol.allow=never",
           "-c", "status.submoduleSummary=false", *args]
    result = run_bounded(cmd, str(layout["workspace"]), remaining, OUTPUT_CAP,
                         False, "read-only workspace Git inspection", env=environment())
    if allow_detached and result.exit_code == 1 and not result.timed_out and not result.stdout and not result.stderr:
        return None
    if not result.ok or result.stderr or "\ufffd" in result.stdout:
        detail = display(result.stderr, 500) or "unknown/invalid Git output"
        raise ValueError("Git inspection refused: " + detail)
    if time.monotonic() > deadline:
        raise ValueError("shared workspace-inspection deadline exhausted")
    return result.stdout


def identity(layout: dict, deadline: float) -> dict:
    head = command(layout, ["rev-parse", "--verify", "HEAD"], deadline).strip()
    if OID.fullmatch(head) is None:
        raise ValueError("Git did not return one complete HEAD object ID")
    branch = command(layout, ["symbolic-ref", "--quiet", "HEAD"], deadline, allow_detached=True)
    if branch is not None:
        if not branch.endswith("\n") or branch.count("\n") != 1 or not branch.startswith("refs/heads/"):
            raise ValueError("Git branch identity is incomplete or unsupported")
        branch = branch[:-1]
    return {"head": head, "branch": display(branch, 160) if branch else None,
            "branch_identity": branch}


def gitlinks(layout: dict, deadline: float) -> int:
    raw = command(layout, ["ls-files", "--stage", "-z"], deadline)
    records = _nul_records(raw)
    count = 0
    for row in records:
        header, sep, path = row.partition("\t")
        fields = header.split(" ")
        if (not sep or not path or len(fields) != 3 or fields[0] not in {"100644", "100755", "120000", "160000"}
                or not OID.fullmatch(fields[1]) or fields[2] not in {"0", "1", "2", "3"}):
            raise ValueError("Git index record is incomplete or unsupported")
        if fields[0] == "160000":
            count += 1
    return count


def _nul_records(raw: str) -> list[str]:
    if not raw:
        return []
    if not raw.endswith("\0"):
        raise ValueError("Git output contains an incomplete NUL record")
    return raw[:-1].split("\0")


def status(layout: dict, deadline: float) -> tuple[str, dict]:
    raw = command(layout, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                           "--ignored=matching", "--ignore-submodules=all"], deadline)
    records = _nul_records(raw)
    counts = {"tracked": 0, "untracked": 0, "ignored": 0, "conflicted": 0}
    refs, index = [], 0
    while index < len(records):
        row = records[index]
        if (len(row) < 4 or row[2] != " " or row[:2] == "  " or (row[:2] not in {"??", "!!"}
                and any(char not in " MADRCUT" for char in row[:2]))):
            raise ValueError("Git status record is incomplete or unsupported")
        code, path = row[:2], row[3:]
        if not path:
            raise ValueError("Git status path is empty")
        kind = "untracked" if code == "??" else "ignored" if code == "!!" else "tracked"
        counts[kind] += 1
        counts["conflicted"] += int("U" in code or code in {"AA", "DD"})
        refs.append({"status": code, "path_display": display(path, 240)})
        if "R" in code or "C" in code:
            index += 1
            if index >= len(records) or not records[index]:
                raise ValueError("Git rename/copy record lacks its second path")
        index += 1
    return raw, {"counts": counts, "reported_entries": len(refs), "path_refs": refs[:MAX_PATH_REFS],
                 "omitted_path_refs": max(0, len(refs) - MAX_PATH_REFS),
                 "path_basis": "redacted_bounded_display_not_routing_identity_or_file_evidence",
                 "ignored_basis": "Git_matching_ignored_entries_not_recursive_descendant_file_count"}
