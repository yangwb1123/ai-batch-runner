#!/usr/bin/env python3
"""Deterministic frontend engineering gate (pure stdlib, heuristic).

Machine-checkable half of the engineering spec: what can be verified by
regex heuristics is enforced here (like quality.py for Python); what needs
judgment (race conditions, debounce policy, state placement) is left to the
engineering review roles in prompts/ and the defect patterns in
ui-specs/engineering/defect-patterns.md.

Errors (definite violations, always fail):
- console.log / debugger (no-op logging)
- TS `any` usage (`: any`, `as any`) — escaping the type system
- test skips / eslint-disable (silencing gates)
- unsafe innerHTML / v-html / dangerouslySetInnerHTML

Warnings (complexity/coupling budgets, fail only with --strict):
- file > 400 lines (god-file risk)
- > 12 state hooks, > 8 event handlers, > 5 api calls, > 30 decision points
- nesting depth > 4 (indentation heuristic)
- > 10 TODO/FIXME markers

Usage:
    python scripts/check-frontend-quality.py --dir src [--strict] [--json]
    python scripts/check-frontend-quality.py file.tsx ...

Violations go to stderr (the runner's retry-feedback channel); --json
prints a machine report to stdout and -o writes it to a file.
Exit code: 0 compliant, 1 violations (warnings count only with --strict).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_EXTS = (".tsx", ".jsx", ".ts", ".js", ".dart", ".vue")
DEFAULT_EXCLUDES = {
    "node_modules", "dist", "build", "out", ".git", ".dart_tool",
    ".next", ".nuxt", "coverage", ".pi-batch", ".venv", "venv",
    "logs", "docs", "__pycache__", "test", "tests", "spec", "e2e",
}

MAX_FILE_LINES = 400
MAX_HOOKS = 12
MAX_HANDLERS = 8
MAX_API_CALLS = 5
MAX_DECISIONS = 30
MAX_NESTING = 4
MAX_TODOS = 10

_ANY_RE = re.compile(r"(:|as)\s+any\b|\bany\[\]")
_CONSOLE_RE = re.compile(r"\bconsole\.(log|debug)\s*\(")
_DEBUGGER_RE = re.compile(r"\bdebugger\s*;?")
# Dart's EdgeInsets.only() is a legit API, not a test .only — exclude it.
_SKIP_RE = re.compile(r"\.skip\s*\(|(?<!EdgeInsets)\.only\s*\(")
_DISABLE_RE = re.compile(r"(eslint-disable|@ts-ignore|@ts-nocheck|ignore_for_file)")
_HTML_RE = re.compile(r"(dangerouslySetInnerHTML|v-html|innerHTML\s*=)\s*[^)]")
_SWALLOWED_RE = re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}")
_HOOK_RE = re.compile(r"\buse(State|Effect|Ref|Memo|Callback|Reducer|Context|Query|Mutation)\s*\(")
_HANDLER_RE = re.compile(r"(onClick|onChange|onPress|onSubmit|@click|@change|onPressed|onTap)\s*[=:]")
_API_RE = re.compile(r"\b(api|axios|http|dio|client|service|repository)\.[a-zA-Z]+\(|\bfetch\s*\(|\b(\.get|\.post|\.put|\.delete)\s*\(")
_DECISION_RE = re.compile(r"\b(if|for|while|switch)\s*\(|\b&&\b|\b\|\|\b")
_TODO_RE = re.compile(r"TODO|FIXME|HACK")


def scan_directory(directory: Path, exts: tuple, excludes: set) -> list:
    """Recursively collect frontend sources, skipping vendor/test dirs."""
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


def _nesting_depth(lines: list) -> int:
    """Max indentation depth of control-flow lines (heuristic)."""
    depth = 0
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        if re.search(r"\b(if|for|while|switch|catch)\s*\(", stripped):
            indent = len(line) - len(line.lstrip())
            depth = max(depth, indent // 2)
    return depth


def _max_nesting(rel: str) -> int:
    """Flutter widget trees nest structurally deep — relax the threshold
    for .dart so only real control-flow complexity is flagged."""
    return 8 if rel.endswith(".dart") else MAX_NESTING


def _error_patterns(rel: str) -> list:
    """Always-fail patterns; Dart skips via `test(..., skip:)` params, not
    jest-style .skip(/.only( — List.skip(n)/EdgeInsets.only() are legit."""
    patterns = [(_CONSOLE_RE, "console.log/debug"),
                (_DEBUGGER_RE, "debugger statement"),
                (_DISABLE_RE, "eslint-disable/@ts-ignore"),
                (_HTML_RE, "unsafe innerHTML/v-html"),
                (_SWALLOWED_RE, "swallowed exception (empty catch)"),
                (_ANY_RE, "TS any usage")]
    if not rel.endswith(".dart"):
        patterns.append((_SKIP_RE, "test .skip/.only"))
    return patterns


def _effect_leak_warnings(text: str, rel: str) -> list:
    """useEffect resources without matching cleanup (listener/timer leak)."""
    if not re.search(r"\buseEffect\s*\(", text):
        return []
    created = len(re.findall(r"\b(addEventListener|setInterval|setTimeout)\s*\(", text))
    cleaned = len(re.findall(r"\b(removeEventListener|clearInterval|clearTimeout)\s*\(", text))
    if created and cleaned < created:
        return [("warning", f"{rel}: {created} listener/timer creations but only "
                            f"{cleaned} cleanups — useEffect must return a "
                            f"cleanup that releases them")]
    return []


def _test_warnings(text: str, rel: str) -> list:
    """Assertion-less test files (renders/exists checks prove nothing)."""
    is_test = ".test." in rel or ".spec." in rel \
        or rel.endswith(("_test.tsx", "_test.ts", ".test.tsx", ".spec.tsx", ".spec.ts"))
    if not is_test:
        return []
    if not re.search(r"\b(expect|assert|should\.|toBe|toHave|vi\.|jest\.)\b", text):
        return [f"test file without assertions (renders/exists checks do not "
                f"prove behavior)"]
    return []


def _block_body(text: str, brace_start: int) -> str:
    """The balanced {...} region starting at brace_start (empty on error)."""
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start:i + 1]
    return ""


def _loop_await_warning(text: str) -> str:
    """N+1 signal: await inside the actual balanced loop body — map
    comprehensions, dispose loops and same-function later awaits are not."""
    for match in re.finditer(r"\bfor\s*\([^)]*\)\s*\{", text):
        body = _block_body(text, text.index("{", match.start()))
        if not body:
            continue
        if re.search(r"\bawait\s+\w+", body) and \
                not re.search(r"(Future\.wait|Promise\.all)", body):
            return f"await inside loop body near char {match.start()} " \
                   f"(N+1 request risk — batch/Future.wait)"
    return ""


def check_text(text: str, path: Path, strict: bool) -> list:
    """Violation strings for one file; (severity, message) tuples."""
    lines = text.splitlines()
    findings = []
    rel = str(path)

    def add(severity, message):
        findings.append((severity, f"{path}: {message}"))

    for pattern, label in _error_patterns(rel):
        for match in pattern.finditer(text):
            add("error", f"{label}: {match.group(0).strip()[:60]}")

    if len(lines) > MAX_FILE_LINES:
        add("warning", f"{len(lines)} lines > {MAX_FILE_LINES} (god-file risk)")
    hooks = len(_HOOK_RE.findall(text))
    if hooks > MAX_HOOKS:
        add("warning", f"{hooks} state hooks > {MAX_HOOKS}")
    handlers = len(_HANDLER_RE.findall(text))
    if handlers > MAX_HANDLERS:
        add("warning", f"{handlers} event handlers > {MAX_HANDLERS}")
    calls = len(_API_RE.findall(text))
    if calls > MAX_API_CALLS:
        add("warning", f"{calls} api calls > {MAX_API_CALLS}")
    decisions = len(_DECISION_RE.findall(text))
    if decisions > MAX_DECISIONS:
        add("warning", f"{decisions} decision points > {MAX_DECISIONS}")
    depth = _nesting_depth(lines)
    limit = _max_nesting(rel)
    if depth > limit:
        add("warning", f"nesting depth {depth} > {limit}")
    todos = len(_TODO_RE.findall(text))
    if todos > MAX_TODOS:
        add("warning", f"{todos} TODO/FIXME markers > {MAX_TODOS}")
    loop_await = _loop_await_warning(text)
    if loop_await:
        add("warning", loop_await)
    for message in _test_warnings(text, rel):
        add("warning", message)

    if not strict:
        findings = [item for item in findings if item[0] == "error"]
    findings.extend(_effect_leak_warnings(text, rel))
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
        print(f"UI-QUALITY {item}", file=sys.stderr)
    if violations:
        print(f"UI-QUALITY: {len(violations)} violation(s) in "
              f"{len(paths)} file(s); rejected", file=sys.stderr)
        return 1
    print(f"UI-QUALITY: OK ({len(paths)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
