#!/usr/bin/env python3
"""Deterministic, pure-stdlib backend engineering gate.

Errors cover unsafe constructs, swallowed failures, scattered state and
dependency-direction violations. Strict warnings cover god services,
over-abstraction, query/persistence risks and weak tests.

``--dir`` is manifest-aware: the nearest go.mod, Cargo.toml, pom.xml or
package.json selects the language. A detected/forced manifest with zero
sources fails closed; no manifest skips the gate visibly. ``--skip`` adds
directory exclusions, and JSON mode keeps stdout machine-readable.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_EXTS = (".ts", ".tsx", ".js", ".go", ".py", ".java", ".php", ".rs", ".sql")

# Manifest-aware --dir mode: fixed precedence, first manifest present at
# the nearest level wins; each manifest pins the scan language.
MANIFEST_SPECS = (
    ("go.mod", (".go",)), ("Cargo.toml", (".rs",)),
    ("pom.xml", (".java", ".kt")), ("package.json", (".ts", ".tsx", ".js")),
)
_MANIFEST_BY_NAME = dict(MANIFEST_SPECS)
_MANIFEST_ALIASES = {
    "go.mod": "go.mod", "go": "go.mod",
    "cargo.toml": "Cargo.toml", "rust": "Cargo.toml",
    "pom.xml": "pom.xml", "java": "pom.xml",
    "package.json": "package.json", "node": "package.json",
}
MANIFEST_WALK_UP = 6
DEFAULT_EXCLUDES = {
    # Tooling dirs are not business services (their own gates are tests).
    "scripts", "tools", "checks", "tests", "harness", "ai",

    "node_modules", "dist", "build", "out", ".git", ".venv", "venv",
    "coverage", ".pi-batch", "logs", "docs", "__pycache__", "vendor",
    "target", "generated", "migrations",
    ".dart_tool", ".next", ".nuxt",  # frontend build artifacts in hybrid repos
    "ephemeral",  # Flutter/IDE scratch dirs (ios/Flutter/ephemeral/...)
    "ai-dev-gates",  # the checker's own copies must not be scanned
    ".claude", ".cursor",  # agent worktree/scratch dirs, not business code
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
_DOMAIN_IMPORT_RE = re.compile(r"from\s+['\"][^'\"]*(infrastructure|controller|orm|prisma|"
                               r"typeorm|mongoose|sequelize|redis|kafka|axios|fetch)[^'\"]*['\"]")
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


def _git_boundary(directory: Path) -> Path | None:
    """Nearest Git root; ``.git`` may be a directory or worktree gitfile."""
    for probe in (directory, *directory.parents):
        if (probe / ".git").exists():
            return probe
    return None


def detect_manifest(directory: Path) -> tuple:
    """Find a manifest within Git; outside Git, use bounded parent lookup."""
    boundary = _git_boundary(directory)
    probe = directory
    for _ in range(MANIFEST_WALK_UP + 1):
        for name, exts in MANIFEST_SPECS:
            if (probe / name).is_file():
                return name, exts
        if probe == boundary:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return None, DEFAULT_EXTS


def resolve_manifest(directory: Path, forced: str) -> tuple:
    """(manifest name, extensions, forced_only) for --dir mode. Auto-detect
    unless ``forced`` names a known manifest (asserts the language, label
    gets a '(--manifest)' suffix); unknown names fall back to the union
    extension set (still fail-closed on 0 files)."""
    if forced is None:
        name, exts = detect_manifest(directory)
        return name, exts, name is None
    name = _MANIFEST_ALIASES.get(forced.casefold())
    if name is None:
        return None, DEFAULT_EXTS, False
    return name, _MANIFEST_BY_NAME[name], True


def resolve_dir_scan(directory: Path, forced: str, skip: str = "") -> tuple:
    """(--dir mode) (files, manifest name, status, scan_error). Fail-closed:
    missing dir -> scan_error; manifest with 0 files -> empty; none -> skip.
    ``skip``: extra excludes (union, never replace)."""
    if not directory.exists() or not directory.is_dir():
        reason = "directory not found" if not directory.exists() else "not a directory"
        return [], "", "scan_error", f"scan error: {reason}: {directory}"
    manifest_name, exts, forced_only = resolve_manifest(directory, forced)
    if manifest_name is None and forced is None:
        return [], "", "skipped", ""
    files = scan_directory(directory, exts, DEFAULT_EXCLUDES | {s for s in skip.split(",") if s})
    if files:
        return files, manifest_name, "ok", ""
    if manifest_name:
        label = f"{manifest_name} (--manifest)" if forced_only else manifest_name
        return [], manifest_name, "empty", f"manifest {label} found but 0 backend files scanned"
    return [], "", "empty", f"--manifest {forced}: 0 backend files scanned"


def _class_metrics(text: str) -> dict:
    """Heuristic class signals: constructor deps, public methods, extends."""
    deps = max(len(re.findall(
        r"constructor\s*\(\s*[^)]*?\b(private|public|readonly|protected)\b", text)), 0)
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


def _scrub_string_literals(text: str) -> str:
    """剥离单行字符串字面量后再扫 DDL——关键词表/提示文案里的
    \"drop table\" 不是真实 DDL（dogfooding：profile.py 硬触发器关键词表
    曾被自家扫描器误报为危险 DDL）。"""
    return re.sub(r'"[^"\n]{0,200}"|\'[^\'\n]{0,200}\'', "", text)


def _ddl_warnings(text: str, rel: str) -> list:
    """Dangerous DDL outside migration files (data-loss risk)."""
    if any(part in rel.lower() for part in ("migration", "migrate", "seed")):
        return []
    path = Path(rel)
    is_test_file = (".test." in rel.lower()
                    or path.name.lower().endswith(("_test.rs", "_tests.rs",
                                                   "_test.py", "_spec.ts",
                                                   "_test.go", "_test.ts"))
                    or any(part in ("test", "tests", "spec", "e2e")
                           for part in path.parts[:-1]))
    if is_test_file:
        # Test files embed DDL in string literals (rollback assertions,
        # throwaway schemas) — those never touch a production database.
        return []
    # ALTER TABLE … DROP CONSTRAINT/DEFAULT/NOT NULL drops metadata, not rows;
    # only data-loss forms are flagged.
    matches = re.findall(
        r"\b(DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE\s+TABLE)\b|"
        r"ALTER\s+TABLE[^;]{0,80}?\bDROP\s+(?!CONSTRAINT\b|DEFAULT\b|NOT\s+NULL\b)",
        _scrub_string_literals(text), re.IGNORECASE)
    # findall: single capture group -> group text ("" for ALTER..DROP alt);
    # never index into an empty match (""[0] raises IndexError).
    if matches:
        display = matches[0] or "ALTER TABLE ... DROP"
        return [("error", f"{rel}: dangerous DDL outside a migration file: {display}"
                          f" (data-loss risk; migrations must be versioned)")]
    return []


def _test_warnings(text: str, rel: str) -> list:
    """Strict-mode test-quality signals: assertion-less test files."""
    suffixes = ("_test.go", "_test.py", "_test.ts", "_spec.ts")
    is_test = ".test." in rel or rel.endswith(suffixes)
    if not is_test:
        return []
    has_assertion = bool(re.search(
        r"\b(expect|assert|assertThat|should\.|t\.Error|t\.Fatal|assertion)\b", text)
        or re.search(r"test\(['\"]", text) and re.search(r"lambda\s*:", text)
        or re.search(r"\bcheck\(\s*['\"]", text))  # check(label, condition)
    if not has_assertion:
        return [("warning", f"{rel}: test file without assertions "
                            f"(renders/exists checks do not prove behavior)")]
    return []


def _reliability_warnings(text: str, rel: str) -> list:
    """Swallowed exceptions: empty JS catch is always an error; Python
    except-pass is an error unless it only swallows OSError-family cleanup
    paths (warning)."""
    findings = []
    if re.search(r"catch\s*\([^)]*\)\s*\{\s*\}", text):
        msg = (f"{rel}: swallowed exception (empty catch) — "
               f"failure path invisible")
        findings.append(("error", msg))
    non_os = len(_EXCEPT_PASS_RE.findall(text)) - len(_OS_EXCEPT_PASS_RE.findall(text))
    if non_os > 0:
        findings.append(("error", f"{rel}: {non_os} except-pass swallowing "
                                  f"non-OSError exceptions — failure path hidden"))
    elif len(_OS_EXCEPT_PASS_RE.findall(text)) > 0:
        msg = (f"{rel}: except-pass on OSError cleanup paths — "
               f"verify it is intentional")
        findings.append(("warning", msg))
    clients = (len(re.findall(r"axios\.create\(|new\s+HttpClient\(|http\.Client\{", text))
               + len(re.findall(r"Dio\(", text)))
    if clients >= 2:
        findings.append(("warning", f"{rel}: {clients} HTTP client creations — reuse a "
                                    f"single client with a connection pool"))
    busy_poll = re.search(
        r"while\s*\([^)]*\)\s*\{[\s\S]{0,200}?(?:sleep|Sleep|setTimeout|Thread\.sleep)",
        text)
    if busy_poll:
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
        findings.append((
            "warning", f"{rel}: {len(lines)} lines > {MAX_FILE_LINES} (god-file risk)"))
    if metrics["deps"] > MAX_DEPS:
        findings.append((
            "warning", f"{rel}: {metrics['deps']} constructor deps > {MAX_DEPS} "
            "(god-service signal)"))
    if metrics["methods"] > MAX_METHODS:
        findings.append(("warning", f"{rel}: {metrics['methods']} public methods > {MAX_METHODS}"))
    for base in metrics["bases"]:
        findings.append((
            "warning", f"{rel}: Base class inheritance: {base} (composition preferred)"))
    if _FUZZY_RE.search(Path(rel).stem):
        findings.append((
            "warning", f"{rel}: fuzzy name '{Path(rel).stem}' "
            "(Common/Manager/Helper/Utils)"))
    for name in _single_implementation_interfaces(text):
        findings.append(("warning", f"{rel}: single-implementation interface '{name}' "
                                    f"(no replacement/test value? consider the concrete class)"))
    if len(_TX_RE.findall(text)) >= 2 and "use-case" not in rel.lower() \
            and "application" not in rel.lower():
        findings.append((
            "warning", f"{rel}: manual transaction calls outside "
            "use-case/application layer"))
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

    error_patterns = _ERROR_PATTERNS
    if rel.endswith((".py", ".rs", ".sql", ".go", ".java", ".php")):
        # TS `any` only exists in TypeScript: Python `lambda: any(...)` uses
        # the builtin, Rust `Box<dyn Any>`/`.downcast_ref::<T>()` and Go
        # `interface{}`-style patterns are legitimate — none is a type
        # annotation leak from the TS world.
        error_patterns = [p for p in _ERROR_PATTERNS if p[1] != "TS any usage"]
    if not rel.endswith((".html", ".tsx", ".jsx", ".js", ".ts", ".vue",
                         ".dart", ".htm", ".hbs", ".ejs", ".jinja", ".liquid")):
        # innerHTML/v-html/html: template injection only exists in frontend
        # template languages; Rust struct fields like `html: &'static str`
        # and Python dict keys are legitimate data, not DOM sinks.
        error_patterns = [p for p in error_patterns if p[1] != "unsafe innerHTML/v-html"]
    if rel.endswith(".rs"):
        error_patterns = [p for p in error_patterns if p[1] != "direct status assignment"]
    for pattern, label in error_patterns:
        for match in pattern.finditer(text):
            add("error", f"{label}: {match.group(0).strip()[:60]}")

    if not rel.endswith(".rs") and _DIRECT_STATUS_RE.search(text):
        m = _DIRECT_STATUS_RE.search(text)
        add("error", f"direct status assignment (bypasses domain methods): "
            f"{m.group(0).strip()[:60]}")
    if "/domain/" in rel and _DOMAIN_IMPORT_RE.search(text):
        m = _DOMAIN_IMPORT_RE.search(text)
        add("error", f"domain layer imports framework/ORM/SDK (dependency direction): "
            f"{m.group(0).strip()[:60]}")

    findings.extend(_warning_findings(lines, rel, text, _class_metrics(text)))

    if not strict:
        findings = [item for item in findings if item[0] == "error"]
    return [message for severity, message in findings]


def parse_argv(argv: list) -> tuple:
    """(targets, directory, strict, want_json, report_path, forced_manifest, skip)."""
    values = {"--dir": None, "--manifest": None, "-o": "", "--skip": ""}
    targets, strict, want_json = [], False, False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in values and i + 1 < len(argv):
            nxt = argv[i + 1]
            if nxt.startswith("-") and nxt != "-":
                # Flag-like value: argparse-family rejects it (exit 2);
                # swallowing it would silently drop a flag (--skip --json
                # -> human mode, exit 0 false pass).
                print(f"error: {arg}: expected one argument (got {nxt})",
                      file=sys.stderr)
                raise SystemExit(2)
            values[arg] = nxt
            i += 1
        elif arg == "--strict":
            strict = True
        elif arg == "--json":
            want_json = True
        else:
            targets.append(arg)  # trailing value flag -> visible missing target
        i += 1
    return (targets, Path(values["--dir"]) if values["--dir"] is not None else None,
            strict, want_json, values["-o"], values["--manifest"], values["--skip"])


def _scan_paths(paths: list, strict: bool) -> list:
    """Violations for every target; missing/unreadable targets are reported."""
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
    return violations


def _emit_report(report: dict, want_json: bool, report_path: str) -> None:
    """Write the JSON report (-o) and/or print it (--json)."""
    if not (want_json or report_path):
        return
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(payload + "\n", encoding="utf-8")
    if want_json:
        print(payload)


def _finish(status: str, scan_error: str, violations: list, n_files: int,
            want_json: bool = False) -> int:
    """Summary lines + exit code; fail closed (scan_error/empty/violations).
    Under --json all summaries go to stderr; stdout = the JSON report."""
    if scan_error:
        print(f"BACKEND-QUALITY: {scan_error}", file=sys.stderr)
    for item in violations:
        print(f"BACKEND-QUALITY {item}", file=sys.stderr)
    if violations:
        print(f"BACKEND-QUALITY: {len(violations)} violation(s) in "
              f"{n_files} file(s); rejected", file=sys.stderr)
        return 1
    if status in ("scan_error", "empty"):
        print(f"BACKEND-QUALITY: {scan_error}; rejected", file=sys.stderr)
        return 1
    msg = ("BACKEND-QUALITY: no backend manifest detected; gate skipped"
           if status == "skipped" else
           f"BACKEND-QUALITY: OK ({n_files} file(s) scanned)")
    print(msg, file=sys.stderr if want_json else None)
    return 0


def main(argv: list) -> int:
    targets, directory, strict, want_json, report_path, forced, skip = parse_argv(argv)
    paths = [Path(t) for t in targets]
    manifest_name, status, scan_error = "", "ok", ""
    if directory is not None:
        # Resolve first: walk-up needs the real path (Path('.') is its own
        # parent), so relative and absolute --dir behave identically.
        dir_files, manifest_name, status, scan_error = \
            resolve_dir_scan(directory.resolve(), forced, skip)
        paths.extend(dir_files)

    violations = _scan_paths(paths, strict)
    report = {
        "strict": strict,
        "directory": str(directory) if directory else "",
        "files_scanned": len(paths),
        "violations": [{"file": item.split(":")[0], "detail": item}
                       for item in violations],
        "total": len(violations),
        "manifest": manifest_name or "",
        "status": status,
    }
    if scan_error:
        report["scan_error"] = scan_error
    _emit_report(report, want_json, report_path)
    return _finish(status, scan_error, violations, len(paths), want_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
