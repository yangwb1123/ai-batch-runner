#!/usr/bin/env python3
"""Deterministic backend engineering gate (pure stdlib, heuristic).

The machine-checkable half of the backend spec: over-engineering signals,
god-file/god-service signals, scattered state, and dependency-direction
violations are enforced here; judgment calls go to the review roles
(backend_engineer / design_pattern_reviewer) and backend-specs/.

Errors (always fail):
- console.log / debugger (no-op logging)
- TS any usage
- unsafe innerHTML / v-html (also backend-adjacent templates)
- test .skip/.only, eslint-disable/@ts-ignore
- direct state assignment (`order.status = "paid"`) — bypasses domain methods
- domain layer importing framework/ORM/SDK (dependency direction)

Warnings (fail only with --strict):
- file > 400 lines, class > 300 lines (god-file signals)
- > 7 constructor dependencies (god-service signal)
- > 12 public methods
- fuzzy names: file/class matching Common|Manager|Helper|Utils|BaseService
- one-implementation interfaces (no replacement/test value)
- Base class inheritance (BaseService/BaseController/...)
- scattered begin/commit/rollback outside use-case layers
- > 10 TODO/FIXME markers

Usage:
    python scripts/check-backend-quality.py --dir src [--strict] [--json]
    python scripts/check-backend-quality.py file.go file.ts ...
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_EXTS = (".ts", ".tsx", ".js", ".go", ".py", ".java", ".php", ".rs", ".sql")
DEFAULT_EXCLUDES = {
    "node_modules", "dist", "build", "out", ".git", ".venv", "venv",
    "coverage", ".pi-batch", "logs", "docs", "__pycache__", "vendor",
    "target", "generated", "migrations",
    ".dart_tool", ".next", ".nuxt",  # frontend build artifacts in hybrid repos
    "ephemeral",  # Flutter/IDE scratch dirs (ios/Flutter/ephemeral/...)
    "ai-dev-gates",  # the checker's own copies must not be scanned
}

MAX_FILE_LINES = 400
MAX_CLASS_LINES = 300
MAX_DEPS = 7
MAX_METHODS = 12
MAX_TODOS = 10

_ERROR_PATTERNS = [
    (re.compile(r"\bconsole\.(log|debug)\s*\("), "console.log/debug"),
    (re.compile(r"(:|as)\s+any\b|\bany\[\]"), "TS any usage"),
    (re.compile(r"(dangerouslySetInnerHTML|v-html|innerHTML\s*=|html:)\s*[^)]"),
     "unsafe innerHTML/v-html"),
    (re.compile(r"\.(skip|only)\s*\("), "test .skip/.only"),
    (re.compile(r"(eslint-disable|@ts-ignore|@ts-nocheck)"), "eslint-disable/@ts-ignore"),
]
_FUZZY_RE = re.compile(r"(common|manager|helper|utils?|base\w*service|util)\b",
                       re.IGNORECASE)
_DIRECT_STATUS_RE = re.compile(r"\b[\w.]+\.status\s*=\s*[\"']")
_DOMAIN_IMPORT_RE = re.compile(
    r"from\s+['\"][^'\"]*(infrastructure|controller|orm|prisma|typeorm|mongoose|"
    r"sequelize|redis|kafka|axios|fetch)[^'\"]*['\"]")
_BASE_CLASS_RE = re.compile(r"class\s+\w+\s+extends\s+Base\w+")
_INTERFACE_RE = re.compile(r"interface\s+(\w+)\b")
_IMPLEMENTS_RE = re.compile(r"class\s+\w+\s+implements\s+([\w,\s]+)")
_TX_RE = re.compile(r"\b(beginTransaction|commit\(|rollback\()\b")
# except-pass swallowing: OSError cleanup paths are tolerated (warning),
# swallowing any other exception type is an error (failure path hidden).
_EXCEPT_PASS_RE = re.compile(r"except\s*[^:]*:\s*\n\s*pass\b(?!\s*#)")
_OS_EXCEPT_PASS_RE = re.compile(
    r"except\s*\((?:OSError|FileNotFoundError|PermissionError|"
    r"subprocess\.TimeoutExpired|ChildProcessError)[^)]*\)\s*:\s*\n\s*pass\b(?!\s*#)"
    r"|except\s*(?:OSError|FileNotFoundError|PermissionError)\s*:\s*\n\s*pass\b(?!\s*#)")
_TODO_RE = re.compile(r"TODO|FIXME|HACK")


def scan_directory(directory: Path, exts: tuple, excludes: set) -> list:
    """Recursively collect backend sources, skipping vendor/generated dirs."""
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_dir():
            continue
        parts = path.parts[:-1]
        if any(part in excludes for part in parts):
            continue
        if path.suffix.lower() in exts:
            files.append(path)
    return files


def _class_metrics(text: str) -> dict:
    """Heuristic class signals: constructor deps, public methods, extends."""
    deps = max(len(re.findall(r"constructor\s*\(\s*[^)]*?\b(private|public|readonly|protected)\b", text)),
               0)
    methods = len(re.findall(r"^\s{4}(public\s+)?(async\s+)?\w+\s*\(", text, re.M))
    base = _BASE_CLASS_RE.findall(text)
    return {"deps": deps, "methods": methods, "bases": base}


def _single_implementation_interfaces(text: str) -> list:
    """Interfaces with exactly one implementation and no mock — candidates
    for over-abstraction (no replacement/test value)."""
    interfaces = _INTERFACE_RE.findall(text)
    implemented = set()
    for match in _IMPLEMENTS_RE.finditer(text):
        implemented.update(name.strip() for name in match.group(1).split(","))
    flagged = []
    for name in interfaces:
        impls = [i for i in implemented if i.casefold() == name.casefold() or
                 i.casefold() == name.casefold() + "impl"]
        if impls and "mock" not in text.lower():
            flagged.append(name)
    return flagged


def _ddl_warnings(text: str, rel: str) -> list:
    """Dangerous DDL outside migration files (data-loss risk)."""
    if any(part in rel.lower() for part in ("migration", "migrate", "seed")):
        return []
    matches = re.findall(
        r"\b(DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE\s+TABLE)\b|ALTER\s+TABLE[^;]{0,80}?\bDROP\b",
        text, re.IGNORECASE)
    return [("error", f"{rel}: dangerous DDL outside a migration file: "
                      f"{matches[0][0] or matches[0][1] or 'ALTER ... DROP'} "
                      f"(data-loss risk; migrations must be versioned)")] \
        if matches else []


def _test_warnings(text: str, rel: str) -> list:
    """Strict-mode test-quality signals: assertion-less test files."""
    is_test = ".test." in rel or rel.endswith(("_test.go", "_test.py", "_test.ts", "_spec.ts"))
    if not is_test:
        return []
    if not re.search(r"\b(expect|assert|assertThat|should\.|t\.Error|t\.Fatal|assertion)\b", text):
        return [("warning", f"{rel}: test file without assertions "
                            f"(renders/exists checks do not prove behavior)")]
    return []


def _reliability_warnings(text: str, rel: str) -> list:
    """Swallowed exceptions: empty JS catch is always an error; Python
    except-pass is an error unless it only swallows OSError-family cleanup
    paths (warning)."""
    findings = []
    if re.search(r"catch\s*\([^)]*\)\s*\{\s*\}", text):
        findings.append(("error", f"{rel}: swallowed exception (empty catch) "
                                  f"— failure path invisible"))
    non_os = len(_EXCEPT_PASS_RE.findall(text)) - len(_OS_EXCEPT_PASS_RE.findall(text))
    if non_os > 0:
        findings.append(("error", f"{rel}: {non_os} except-pass swallowing "
                                  f"non-OSError exceptions — failure path hidden"))
    elif len(_OS_EXCEPT_PASS_RE.findall(text)) > 0:
        findings.append(("warning", f"{rel}: except-pass on OSError cleanup "
                                    f"paths — verify it is intentional"))
    clients = (len(re.findall(r"axios\.create\(|new\s+HttpClient\(|http\.Client\{", text))
               + len(re.findall(r"Dio\(", text)))
    if clients >= 2:
        findings.append(("warning", f"{rel}: {clients} HTTP client creations — reuse a "
                                    f"single client with a connection pool"))
    if re.search(r"while\s*\([^)]*\)\s*\{[\s\S]{0,200}?(?:sleep|Sleep|setTimeout|Thread\.sleep)", text):
        findings.append(("warning", f"{rel}: busy-polling loop without backoff/cap — "
                                    f"use exponential backoff + max attempts"))
    return findings


def _persistence_warnings(text: str, rel: str) -> list:
    """Strict-mode persistence/query signals: float money, SELECT *, LIKE
    %-scans, tenant-less WHERE id queries, VARCHAR(255) sprawl."""
    findings = []
    if re.search(r"(?:price|amount|total|fee|balance|cost)\s+\w*(?:double|float|real)\b",
                 text, re.IGNORECASE):
        findings.append(("warning", f"{rel}: money stored as float/double "
                                    f"(use minor-unit BIGINT or DECIMAL)"))
    if re.search(r"\bSELECT\s+\*", text, re.IGNORECASE):
        findings.append(("warning", f"{rel}: SELECT * — list explicit columns "
                                    f"(read model + tenant safety)"))
    if re.search(r"LIKE\s*'%|LIKE\s*\"%", text, re.IGNORECASE):
        findings.append(("warning", f"{rel}: LIKE '%...%' full scan — evaluate "
                                    f"full-text/inverted index for search"))
    if re.search(r"WHERE\s+id\s*=\s*\?|WHERE\s+id\s*=\s*[a-zA-Z_]+\b", text) \
            and not re.search(r"tenant", text, re.IGNORECASE):
        findings.append(("warning", f"{rel}: WHERE id without tenant scoping "
                                    f"(cross-tenant access risk in shared tables)"))
    if len(re.findall(r"varchar\s*\(\s*255\s*\)", text, re.IGNORECASE)) >= 5:
        findings.append(("warning", f"{rel}: VARCHAR(255) sprawl — fields need "
                                    f"deliberate lengths"))
    return findings


def _algorithm_warnings(text: str, rel: str) -> list:
    """Strict-mode complexity signals: N+1 loops, O(n) shift queues,
    full-sort Top K, nested linear searches."""
    findings = []
    for match in re.finditer(r"\bfor\s*\(", text):
        window = text[match.end():match.end() + 400]
        if re.search(r"\bawait\s+\w+", window) and not re.search(r"Promise\.all", window):
            findings.append(("warning", f"{rel}: await inside loop near char {match.start()} "
                                        f"(N+1 I/O risk — batch/JOIN/Promise.all)"))
            break
    if re.search(r"\.shift\(\)", text):
        findings.append(("warning", f"{rel}: array.shift() is O(n) — "
                                    f"use a deque/head index for queues"))
    if re.search(r"\.sort\([\s\S]{1,150}?\)\s*\.slice\(0,", text):
        findings.append(("warning", f"{rel}: full sort then slice — "
                                    f"Top K should use a heap (O(n log k))"))
    if re.search(r"\bfor\s*\([^)]*\)[^{]*\{[^{}]*\bfor\s*\(", text):
        findings.append(("warning", f"{rel}: nested loops with inner linear "
                                    f"search risk (O(n²) — preload a Map)"))
    return findings


def _warning_findings(lines: list, rel: str, text: str, metrics: dict) -> list:
    """Strict-mode warnings: god-file/god-service signals, fuzzy names,
    over-abstraction candidates, scattered transactions."""
    findings = []
    if len(lines) > MAX_FILE_LINES:
        findings.append(("warning", f"{rel}: {len(lines)} lines > {MAX_FILE_LINES} (god-file risk)"))
    if metrics["deps"] > MAX_DEPS:
        findings.append(("warning", f"{rel}: {metrics['deps']} constructor deps > {MAX_DEPS} (god-service signal)"))
    if metrics["methods"] > MAX_METHODS:
        findings.append(("warning", f"{rel}: {metrics['methods']} public methods > {MAX_METHODS}"))
    for base in metrics["bases"]:
        findings.append(("warning", f"{rel}: Base class inheritance: {base} (composition preferred)"))
    if _FUZZY_RE.search(Path(rel).stem):
        findings.append(("warning", f"{rel}: fuzzy name '{Path(rel).stem}' (Common/Manager/Helper/Utils)"))
    for name in _single_implementation_interfaces(text):
        findings.append(("warning", f"{rel}: single-implementation interface '{name}' "
                                    f"(no replacement/test value? consider the concrete class)"))
    if len(_TX_RE.findall(text)) >= 2 and "use-case" not in rel.lower() \
            and "application" not in rel.lower():
        findings.append(("warning", f"{rel}: manual transaction calls outside use-case/application layer"))
    todos = len(_TODO_RE.findall(text))
    if todos > MAX_TODOS:
        findings.append(("warning", f"{rel}: {todos} TODO/FIXME markers > {MAX_TODOS}"))
    findings.extend(_algorithm_warnings(text, rel))
    findings.extend(_persistence_warnings(text, rel))
    findings.extend(_reliability_warnings(text, rel))
    findings.extend(_test_warnings(text, rel))
    findings.extend(_ddl_warnings(text, rel))
    return findings


def check_text(text: str, path: Path, strict: bool) -> list:
    """Violation strings for one file; (severity, message) tuples."""
    lines = text.splitlines()
    findings = []
    rel = str(path)

    def add(severity, message):
        findings.append((severity, f"{rel}: {message}"))

    for pattern, label in _ERROR_PATTERNS:
        for match in pattern.finditer(text):
            add("error", f"{label}: {match.group(0).strip()[:60]}")

    if _DIRECT_STATUS_RE.search(text):
        add("error", "direct status assignment (bypasses domain methods): "
            + _DIRECT_STATUS_RE.search(text).group(0).strip()[:60])
    if "/domain/" in rel and _DOMAIN_IMPORT_RE.search(text):
        add("error", "domain layer imports framework/ORM/SDK (dependency direction): "
            + _DOMAIN_IMPORT_RE.search(text).group(0).strip()[:60])

    findings.extend(_warning_findings(lines, rel, text, _class_metrics(text)))

    if not strict:
        findings = [item for item in findings if item[0] == "error"]
    return [message for severity, message in findings]


def parse_argv(argv: list) -> tuple:
    """(targets, directory, strict, want_json, report_path)."""
    targets, directory, strict, want_json, report_path = [], None, False, False, ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dir" and i + 1 < len(argv):
            directory = Path(argv[i + 1])
            i += 1
        elif arg == "--strict":
            strict = True
        elif arg == "--json":
            want_json = True
        elif arg == "-o" and i + 1 < len(argv):
            report_path = argv[i + 1]
            i += 1
        else:
            targets.append(arg)
        i += 1
    return targets, directory, strict, want_json, report_path


def main(argv: list) -> int:
    targets, directory, strict, want_json, report_path = parse_argv(argv)
    paths = [Path(t) for t in targets]
    if directory is not None:
        paths.extend(scan_directory(directory, DEFAULT_EXTS, DEFAULT_EXCLUDES))

    violations = []
    for path in paths:
        if not path.exists():
            violations.append(f"{path}: missing target")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"{path}: unreadable ({exc})")
            continue
        violations.extend(check_text(text, path, strict))

    report = {
        "strict": strict,
        "directory": str(directory) if directory else "",
        "files_scanned": len(paths),
        "violations": [{"file": item.split(":")[0], "detail": item}
                       for item in violations],
        "total": len(violations),
    }
    if want_json or report_path:
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if report_path:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(payload + "\n", encoding="utf-8")
        if want_json:
            print(payload)
    for item in violations:
        print(f"BACKEND-QUALITY {item}", file=sys.stderr)
    if violations:
        print(f"BACKEND-QUALITY: {len(violations)} violation(s) in "
              f"{len(paths)} file(s); rejected", file=sys.stderr)
        return 1
    print(f"BACKEND-QUALITY: OK ({len(paths)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
