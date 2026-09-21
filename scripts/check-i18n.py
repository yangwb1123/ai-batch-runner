#!/usr/bin/env python3
"""Deterministic no-new-i18n-debt gate for SVERP web applications.

The repository is still allowed to contain historical hard-coded copy.  This
check therefore ratchets only additions relative to ``--base`` while always
checking the locale catalog's structural invariants:

* every application locale has an Iris month-picker dictionary;
* every month-picker message is non-empty and preserves its placeholders;
* newly added UI copy is expressed through a translation call, not a literal;
* a newly added fixed UI locale (for example ``locale=\"zh-CN\"``) is rejected.

This is intentionally a conservative heuristic.  It catches common JSX/Vue
copy surfaces and leaves data labels, tests, comments, and the catalog itself
alone.  A review is still required for dynamic copy and domain terminology.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CATALOG = Path("apps/web/src/i18n.ts")
SOURCE_ROOT = Path("apps/web/src")
SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".vue"}
TEST_PARTS = {"__testkit__", "__tests__", "test", "tests", "spec", "e2e"}
CATALOG_FILES = {CATALOG.as_posix(), "apps/web/src/i18n.test.ts"}

MONTH_MESSAGES = {
    "monthPicker.placeholder": (),
    "monthPicker.previousYear": (),
    "monthPicker.nextYear": (),
    "monthPicker.months": ("year",),
}

# These are the copy-bearing props used by the Vue JSX surface.  ``label`` and
# ``title`` are included deliberately: they are easy places for new UI copy to
# bypass the catalog.  Technical identifiers such as P01 are ignored below.
_COPY_KEYS = (
    "aria-label",
    "ariaLabel",
    "aria-description",
    "ariaDescription",
    "caption",
    "description",
    "emptyText",
    "errorText",
    "helperText",
    "label",
    "loadingText",
    "placeholder",
    "subtitle",
    "title",
    "tooltip",
)
_COPY_LITERAL_RE = re.compile(
    r"\b(?P<key>" + "|".join(re.escape(key) for key in _COPY_KEYS) + r")"
    r"\s*(?:=|:)\s*(?:\{\s*)?"
    r"(?P<quote>[\"'`])(?P<value>.*?)(?P=quote)"
)
_FIXED_LOCALE_RE = re.compile(
    r"\blocale\s*(?:=|:)\s*(?:\{\s*)?([\"'`])(?P<value>[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)\1"
)
_FIXED_INTL_LOCALE_RE = re.compile(
    r"\bIntl\.(?:DateTimeFormat|NumberFormat|RelativeTimeFormat)\s*\(\s*"
    r"([\"'`])(?P<value>[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)\1"
)
_JSX_TEXT_RE = re.compile(
    r">\s*(?P<value>[A-Za-z\u00c0-\u024f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
    r"[^<>{}\n]*)<"
)
_TEMPLATE_COPY_RE = re.compile(
    r"\b(?:aria-label|ariaLabel|title|description|placeholder)\s*=\s*\{\s*`"
    r"(?P<value>[^`]*[A-Za-z\u00c0-\u024f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af][^`]*)`"
)
_TRANSLATION_CALL_RE = re.compile(
    r"\b(?:t|\$t|i18n\.t|translate|tr)\s*\(\s*([\"'`])(?P<key>[^\"'`]+)\1"
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result.stdout.decode("utf-8", "surrogateescape")


def _changed_files(root: Path, base: str) -> tuple[list[str], set[str]]:
    tracked = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=ACMRTUXB",
        base,
        "--",
    )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    tracked_files = [item for item in tracked.split("\0") if item]
    untracked_files = {item for item in untracked.split("\0") if item}
    return sorted(set(tracked_files) | untracked_files), untracked_files


def _added_lines(root: Path, rel: str, base: str, untracked: set[str]) -> list[tuple[int, str]]:
    path = root / rel
    if rel in untracked:
        try:
            return list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))
        except (OSError, UnicodeError):
            return []

    diff = _git(root, "diff", "--unified=0", "--no-color", base, "--", rel)
    result: list[tuple[int, str]] = []
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            if match:
                new_line = int(match.group(1))
            continue
        if raw.startswith("+++"):
            continue
        if raw.startswith("+"):
            result.append((new_line, raw[1:]))
            new_line += 1
        elif not raw.startswith("-"):
            new_line += 1
    return result


def _skip_quoted(text: str, start: int) -> int:
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            return index + 1
        else:
            index += 1
    return len(text)


def _skip_non_code(text: str, start: int) -> int:
    if text.startswith("//", start):
        end = text.find("\n", start + 2)
        return len(text) if end < 0 else end
    if text.startswith("/*", start):
        end = text.find("*/", start + 2)
        return len(text) if end < 0 else end + 2
    if text[start] in "\"'`":
        return _skip_quoted(text, start)
    return start


def _code_without_literals(text: str) -> str:
    """Mask strings/comments so braces in messages do not affect nesting."""
    masked = list(text)
    index = 0
    while index < len(text):
        end = _skip_non_code(text, index)
        if end <= index:
            index += 1
            continue
        for position in range(index, end):
            if text[position] != "\n":
                masked[position] = " "
        index = end
    return "".join(masked)


def _object_body(text: str, anchor: str) -> str:
    start = text.find(anchor)
    if start < 0:
        return ""
    code = _code_without_literals(text)
    opening = code.find("{", start)
    if opening < 0:
        return ""
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1]
    return ""


def _locale_entries(body: str) -> dict[str, str]:
    """Extract the simple top-level ``\"locale\": { ... }`` entries."""
    entries: dict[str, str] = {}
    matches = list(
        re.finditer(r'^\s*["\']([^"\']+)["\']\s*:\s*\{\s*$', body, re.MULTILINE)
    )
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        entries[match.group(1)] = body[match.start() : end]
    return entries


def _catalog_entries(text: str, anchor: str) -> dict[str, str]:
    return _locale_entries(_object_body(text, anchor))


def _locale_set_findings(locales: set[str], iris_entries: dict[str, str]) -> list[str]:
    findings: list[str] = []
    if not locales:
        findings.append("i18n catalog has no application locales")
    missing = sorted(locales - set(iris_entries))
    extra = sorted(set(iris_entries) - locales)
    if missing:
        findings.append(f"Iris locale catalog missing locales: {', '.join(missing)}")
    if extra:
        findings.append(f"Iris locale catalog has unsupported locales: {', '.join(extra)}")
    return findings


def _month_message_findings(locales: set[str], iris_entries: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for locale in sorted(locales):
        entry = iris_entries.get(locale, "")
        for key, placeholders in MONTH_MESSAGES.items():
            match = re.search(
                r"[\"']" + re.escape(key) + r"[\"']\s*:\s*[\"']([^\"']*)[\"']",
                entry,
            )
            if not match or not match.group(1).strip():
                findings.append(f"{locale}: missing/empty Iris message {key}")
                continue
            value = match.group(1)
            for placeholder in placeholders:
                if "{" + placeholder + "}" not in value:
                    findings.append(
                        f"{locale}: Iris message {key} must preserve {{{placeholder}}}"
                    )
    return findings


def _catalog_findings(root: Path) -> list[str]:
    path = root / CATALOG
    if not path.is_file():
        return [f"missing i18n catalog: {CATALOG}"]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"cannot read i18n catalog {CATALOG}: {exc}"]

    locales = set(_catalog_entries(text, "const messages"))
    iris_entries = _catalog_entries(text, "const irisLocaleMessages")
    return _locale_set_findings(locales, iris_entries) + _month_message_findings(
        locales, iris_entries
    )


def _is_source_file(rel: str) -> bool:
    path = Path(rel)
    return (
        rel.startswith(SOURCE_ROOT.as_posix() + "/")
        and path.suffix in SOURCE_EXTENSIONS
        and not any(part in TEST_PARTS for part in path.parts)
        and not path.stem.endswith((".test", ".spec"))
        and rel not in CATALOG_FILES
    )


def _looks_like_copy(value: str) -> bool:
    if not value.strip() or not re.search(
        r"[A-Za-z\u00c0-\u024f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", value
    ):
        return False
    # Backend/domain identifiers are data, not copy.  A sentence or CJK text
    # never matches this allowlist and remains protected by the gate.
    return not re.fullmatch(r"[A-Z0-9_./:#-]+", value.strip())


def _line_findings(rel: str, line_no: int, line: str) -> list[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "/*", "*", "<!--", "#")):
        return []
    findings: list[str] = []
    if "i18n-ignore" in line:
        findings.append(f"{rel}:{line_no}: i18n-ignore is not permitted in UI source")
        return findings

    # Do not interpret the tail of a line comment as JSX/prop source.
    code = line.split("//", 1)[0]
    if _TRANSLATION_CALL_RE.search(code):
        # A translated expression is already the required form.  Static
        # literal props on the same line are still checked below only when the
        # literal is not part of the translation call.
        pass
    for match in _COPY_LITERAL_RE.finditer(code):
        value = match.group("value")
        if _looks_like_copy(value):
            findings.append(
                f"{rel}:{line_no}: hard-coded {match.group('key')} copy; use an i18n key"
            )
    for match in _TEMPLATE_COPY_RE.finditer(code):
        value = match.group("value")
        if "t(" not in value and "$t(" not in value and _looks_like_copy(value):
            findings.append(f"{rel}:{line_no}: hard-coded template copy; use an i18n key")
    for match in _JSX_TEXT_RE.finditer(code):
        if _looks_like_copy(match.group("value")):
            findings.append(f"{rel}:{line_no}: hard-coded JSX text; use an i18n key")
    for pattern, label in (
        (_FIXED_LOCALE_RE, "fixed locale"),
        (_FIXED_INTL_LOCALE_RE, "fixed Intl locale"),
    ):
        if pattern.search(code):
            findings.append(f"{rel}:{line_no}: {label}; use the active locale")
    return findings


def _source_findings(root: Path, files: list[str], base: str, untracked: set[str]) -> list[str]:
    findings: list[str] = []
    for rel in files:
        if not _is_source_file(rel) or not (root / rel).is_file():
            continue
        for line_no, line in _added_lines(root, rel, base, untracked):
            findings.extend(_line_findings(rel, line_no, line))
    return findings


def run(root: Path, base: str) -> list[str]:
    files, untracked = _changed_files(root, base)
    findings = _catalog_findings(root)
    findings.extend(_source_findings(root, files, base, untracked))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="HEAD")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        findings = run(root, args.base)
    except (OSError, RuntimeError) as exc:
        print(f"FAIL: i18n gate could not run: {exc}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"FAIL: {finding}", file=sys.stderr)
        print(f"FAIL: i18n gate ({len(findings)} finding(s))", file=sys.stderr)
        return 1
    print("PASS: i18n gate (catalog complete; no new hard-coded UI copy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
