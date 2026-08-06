#!/usr/bin/env python3
"""Backend Experience Intelligence gate (pure stdlib).

Deterministic half of the "业务闭环" checks — catches backend code that
is a bare CRUD slice of a database instead of a business lifecycle:

1. State machine discipline — status values should come from a defined
   enum/constant set, not scattered raw string literals ("active" vs
   `const StatusActive = "active"`).
2. Audit/history — write/update operations should have changelog/
   version/audit/event signals nearby (never bare `UPDATE ... SET`).
3. Async lifecycle — long operations (upload/process/import/export)
   should expose a processing/queued/done/failed status instead of a
   blocking call.
4. Aggregate context — search/list endpoints that serve page-level
   experiences should return nested context (not bare rows).

Scans Go/Dart/TS/Python. Exits 0 clean, 1 violations, 2 usage error.
`--json` emits a report.

Usage:
  python check-backend-experience.py [--dir DIR] [--json] [-o OUT]
  python check-backend-experience.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------

# Raw status string literals assigned to status-ish fields.
_STATUS_LITERAL = re.compile(
    r"""(?:status|state|phase)\s*[:=]\s*["']([a-z_]+)["']""",
    re.IGNORECASE,
)
# Async lifecycle states are managed by the queue/worker machinery — they
# are not "scattered literals" (upload.go Status: 'queued' is fine).
_ASYNC_STATE_NAMES = {
    "queued", "processing", "done", "failed", "pending",
    "succeeded", "completed", "ready", "running", "error",
}
# A defined status set: enum / const declarations with status members.
_STATUS_SET = re.compile(
    r"enum\s+\w*(?:Status|State|Phase)\w*|"
    r"(?:const|final)\s+\w*(?:Status|State|Phase)\w*\s*=",
)
# Write operations without version/audit nearby.
_UPDATE_OP = re.compile(
    r"\b(?:UPDATE|update)\s+\w+|\.(?:update|Update|save|Save|patch|Patch)\(",
)
_AUDIT_SIGNAL = re.compile(
    r"changelog|change_log|version|audit|history|event\b|"
    r"updated_at|updatedAt|revision|approval",
    re.IGNORECASE,
)
# Long-operation lifecycle markers.
_LONG_OP = re.compile(
    r"upload|process|import|export|generate|render|batch|sync|"
    r"ingest|analyze|convert|scan",
    re.IGNORECASE,
)
_ASYNC_STATUS = re.compile(
    r"(?:status|state)\w*\s*[:=]\s*(?:[\"'](?:queued|processing|done|failed|"
    r"pending|succeeded|completed)[\"']|\w*(?:Status|State)\w*\.\w+)",
    re.IGNORECASE,
)
_ASYNC_MECHANISM = re.compile(
    r"async|queue|worker|event\b|job|task_id|taskId|poll|202|"
    r"processing|进度|任务",
    re.IGNORECASE,
)
# Aggregate context: search/list endpoints returning nested payloads.
_SEARCH_ENDPOINT = re.compile(
    r"(?:search|list|overview|summary|dashboard|context)\w*",
    re.IGNORECASE,
)
_NESTED_RETURN = re.compile(
    r"\{\s*[\"']?\w+[\"']?\s*:\s*\{|Map<String, dynamic>\{.*:.*\{|"
    r"context\s*[:=]|summary\s*[:=]|aggregate\s*[:=]",
    re.IGNORECASE,
)
# Files that are purely infrastructure (DB migrations, configs) skip.
_INFRA_FILE = re.compile(
    r"(migration|schema|config|Dockerfile|Makefile|\.sql$)",
    re.IGNORECASE,
)


def _check_state_machine(rel: str, source: str) -> list[dict]:
    status_hits = [
        m.group(1).lower()
        for m in _STATUS_LITERAL.finditer(source)
        if m.group(1).lower() not in _ASYNC_STATE_NAMES
    ]
    if not status_hits or _STATUS_SET.search(source):
        return []
    return [{
        "file": rel,
        "check": "status_literals_without_state_set",
        "detail": (
            f"{rel}: raw status string literals without a defined "
            "status set — states must live in an enum/const set "
            "(domain-lifecycle)"
        ),
    }]


def _check_audit(rel: str, source: str) -> list[dict]:
    if not _UPDATE_OP.search(source) or _AUDIT_SIGNAL.search(source):
        return []
    return [{
        "file": rel,
        "check": "write_without_audit_signal",
        "detail": (
            f"{rel}: write/update operations without version/audit/"
            "history signals — business updates need changelog traces"
        ),
    }]


def _check_async_lifecycle(rel: str, source: str) -> list[dict]:
    if not _LONG_OP.search(source):
        return []
    if _ASYNC_STATUS.search(source) or _ASYNC_MECHANISM.search(source):
        return []
    return [{
        "file": rel,
        "check": "long_op_without_async_lifecycle",
        "detail": (
            f"{rel}: long-running operation (upload/process/import) "
            "without async status (queued/processing/done/failed) — "
            "long tasks must not block requests"
        ),
    }]


def _check_search_context(rel: str, source: str) -> list[dict]:
    if not _SEARCH_ENDPOINT.search(rel) or _NESTED_RETURN.search(source):
        return []
    return [{
        "file": rel,
        "check": "search_endpoint_without_context",
        "detail": (
            f"{rel}: search/list endpoint without nested context — "
            "page-level searches should return aggregates "
            "(orders/amount/risk), not bare rows"
        ),
    }]


def _check_file(path: Path, source: str) -> list[dict]:
    rel = str(path)
    if _INFRA_FILE.search(rel):
        return []
    findings: list[dict] = []
    findings.extend(_check_state_machine(rel, source))
    findings.extend(_check_audit(rel, source))
    findings.extend(_check_async_lifecycle(rel, source))
    findings.extend(_check_search_context(rel, source))
    return findings


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _collect_files(root: Path, skip: str) -> list[Path]:
    skipped = [s for s in skip.split(",") if s]
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".go", ".dart", ".ts", ".tsx", ".py", ".js"}
        and not any(part in skipped for part in p.parts)
        and ".dart_tool" not in p.parts
        and "node_modules" not in p.parts
        and "build" not in p.parts
        and "vendor" not in p.parts
    ]


_SELFCHECK_CASES = [
    # (name, source, expected_check_or_None_for_clean)
    ("orders/service.py",
     "def update(o): o.status = 'active'; o.status = 'cancelled'",
     "status_literals_without_state_set"),
    ("orders/status.py",
     "class OrderStatus: ACTIVE = 'active'; CANCELLED = 'cancelled'",
     None),
    ("orders/repo.go",
     "func (r *Repo) Update(o Order) { db.Update(o) }",
     "write_without_audit_signal"),
    ("orders/repo.go",
     "func (r *Repo) Update(o Order) { db.Update(o); r.audit.Log(o) }",
     None),
    ("files/upload.go",
     "func Upload(f File) { scan(f); ocr(f); index(f); return }",
     "long_op_without_async_lifecycle"),
    ("files/upload.go",
     "func Upload(f File) { q.Enqueue(job{Id: newId(), Status: 'queued'}) }",
     None),
    ("search/customers.go",
     "func Search(q string) []Customer { return rows }",
     "search_endpoint_without_context"),
    ("search/customers.go",
     "func Search(q string) CustomerView { return CustomerView{"
     "Customer: c, Context: OrderSummary{Orders: 238}} }",
     None),
    ("db/migration_001.sql", "UPDATE orders SET status='active'", None),
]


def _selfcheck() -> int:
    bad = 0
    for name, source, expected in _SELFCHECK_CASES:
        found = [v["check"] for v in _check_file(Path(name), source)]
        if expected is None and found:
            bad += 1
            print(f"selfcheck FAIL: {name} should be clean: {found[0]}")
        elif expected is not None and expected not in found:
            bad += 1
            print(f"selfcheck FAIL: {name} expected {expected}")
    print("selfcheck:", "OK" if bad == 0 else f"{bad} FAILURES")
    return 0 if bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help="directory to scan")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-o", "--output", help="write report to file")
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument(
        "--skip", default="", help="comma-separated path fragments"
    )
    args = parser.parse_args(argv)

    if args.selfcheck:
        return _selfcheck()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"error: {args.dir} is not a directory", file=sys.stderr)
        return 2

    violations: list[dict] = []
    for path in _collect_files(root, args.skip):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        violations.extend(_check_file(path, source))

    report = {
        "directory": args.dir,
        "files_scanned": len(_collect_files(root, args.skip)),
        "violations": violations,
        "total": len(violations),
    }
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for v in violations:
            print(v["detail"])
        print(f"{len(violations)} violation(s) found")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
