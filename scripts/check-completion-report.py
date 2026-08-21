#!/usr/bin/env python3
"""Definition-of-Done gate (pure stdlib): enforces the completion-evidence
spec (product-specs/completion-evidence.md) mechanically.

An agent task is complete only when its artifact carries a completion
report with an honest verification record:

    ```yaml
    completion_report:
      summary: ...
      changed_files: [...]
      commands_executed:
        - command: "pnpm lint"
          result: passed          # passed | failed | not_executed
      not_executed:
        - check: e2e
          reason: no browser environment
      residual_risks: [...]
      assumptions: [...]
    ```

Failures (reject the artifact, stderr feeds the retry prompt):
- no completion_report block at all
- result outside {passed, failed, not_executed}
- not_executed entries without a reason
- empty commands_executed / changed_files (nothing verified / nothing changed)
- fabricated-pass signals: "理论上应该通过", "should pass", "assume passed"
  used as verification evidence

Usage:
    python scripts/check-completion-report.py <artifact> [--json]
Register as validator: completion = "python {cwd}/scripts/check-completion-report.py {output}"
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

VALID_RESULTS = {"passed", "failed", "not_executed"}
_FABRICATED_RE = re.compile(
    r"理论上应该|应该可以通过|assume.{0,20}pass|should pass|假装|mock.{0,10}pass")


def _extract_blocks(text: str) -> list:
    """YAML blocks fenced with ```yaml ... ``` containing completion_report."""
    blocks = []
    for match in re.finditer(r"```yaml\s*(.*?)```", text, re.S):
        block = match.group(1)
        if "completion_report" in block:
            blocks.append(block)
    if not blocks and "completion_report" in text:
        # tolerate an unfenced inline report
        blocks.append(text[text.index("completion_report"):][:4000])
    return blocks


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


def _scalar_list(value: str) -> list:
    """Parse the small inline-list subset needed by the stdlib fallback."""
    value = value.strip()
    if not value or value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        if not body:
            return []
        return [item.strip().strip("\"'") for item in body.split(",")
                if item.strip()]
    return [value.strip("\"'")]


_TEXTUAL_LIST_SECTIONS = {"changed_files", "residual_risks", "assumptions"}
_TEXTUAL_MAP_SECTIONS = {"commands_executed", "not_executed"}


def _textual_section(report: dict, key: str, value: str) -> str:
    """Store a top-level completion field and return the active section."""
    if key in _TEXTUAL_LIST_SECTIONS:
        report[key] = _scalar_list(value)
    elif key in _TEXTUAL_MAP_SECTIONS:
        report[key] = []
    else:
        report[key] = value.strip("\"'")
    return key


def _textual_nested(report: dict, section: str, key: str, value: str,
                    current: Optional[dict]) -> Optional[dict]:
    """Apply a nested command/not-executed field."""
    if section not in _TEXTUAL_MAP_SECTIONS:
        return current
    value = value.strip("\"'")
    if key in {"command", "check"}:
        current = {key: value}
        report[section].append(current)
    elif key in {"result", "reason"} and current is not None:
        current[key] = value
    return current


def _textual_item(report: dict, section: str, content: str,
                  current: Optional[dict]) -> Optional[dict]:
    """Apply one YAML-style list item in the fallback grammar."""
    if section in _TEXTUAL_LIST_SECTIONS:
        report.setdefault(section, []).append(content.strip("\"'"))
        return current
    if section not in _TEXTUAL_MAP_SECTIONS:
        return current
    pair = re.match(r"(command|check):\s*(.*)$", content)
    if not pair:
        return current
    current = {pair.group(1): pair.group(2).strip("\"'")}
    report[section].append(current)
    return current


def _textual_report(block: str) -> Optional[dict]:
    """Parse the deliberately narrow completion schema without PyYAML."""
    if not any(line.strip() == "completion_report:" for line in block.splitlines()):
        return None
    report, section, current = {}, "", None
    for raw in block.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "completion_report:":
            continue
        match = re.match(r"^( {2,})([A-Za-z_][\w-]*):(?:[ \t]*(.*))?$", raw)
        if match:
            key, value = match.group(2), (match.group(3) or "").strip()
            if len(match.group(1)) <= 2:
                section, current = _textual_section(report, key, value), None
            else:
                current = _textual_nested(report, section, key, value, current)
            continue
        item = re.match(r"^\s+-\s+(.*)$", raw)
        if item:
            current = _textual_item(report, section, item.group(1), current)
    return report


def _parse_report_block(block: str) -> Optional[dict]:
    """Load one report with PyYAML when present, otherwise use the fallback."""
    try:
        import yaml
    except ImportError:
        return _textual_report(block)
    try:
        data = yaml.safe_load(block)
    except Exception:
        return None
    return data.get("completion_report") if isinstance(data, dict) else None


def _check_commands(commands, violations) -> None:
    """commands_executed: non-empty list, result in the enum."""
    if not isinstance(commands, list) or not commands:
        violations.append("commands_executed missing or empty "
                          "(nothing verified)")
        return
    for item in commands:
        result = item.get("result") if isinstance(item, dict) else None
        if result not in VALID_RESULTS:
            violations.append(f"invalid result {result!r} in commands_executed "
                              f"(must be {sorted(VALID_RESULTS)})")


def _check_not_executed(not_run, violations) -> None:
    """not_executed entries must carry a reason (honest gaps). Absent or
    empty means no unrun checks — legitimate."""
    if not_run is None or not_run == []:
        return
    if not isinstance(not_run, list):
        violations.append("not_executed must be a list")
        return
    for item in not_run:
        reason = item.get("reason") if isinstance(item, dict) else None
        if not reason or not str(reason).strip():
            violations.append(f"not_executed entry without reason: {item!r}")


def check_report(text: str) -> list:
    """Violation strings for one artifact."""
    violations = []
    blocks = _extract_blocks(text)
    if not blocks:
        return ["missing completion_report block (see "
                "product-specs/completion-evidence.md)"]
    if _FABRICATED_RE.search(text):
        violations.append("fabricated-pass phrasing used as verification "
                          f"evidence: {_FABRICATED_RE.search(text).group(0)[:40]}")
    report = None
    for block in blocks:
        report = _parse_report_block(block)
        if isinstance(report, dict):
            break
        report = None
    if report is None:
        return violations + ["completion_report is not valid YAML mapping"]
    commands = report.get("commands_executed")
    _check_commands(commands, violations)
    _check_not_executed(report.get("not_executed"), violations)
    changed = report.get("changed_files")
    if not isinstance(changed, list) or not changed:
        violations.append("changed_files missing or empty")
    for key in ("residual_risks", "assumptions"):
        if key not in report:
            violations.append(f"completion_report missing '{key}'")
    return violations


def main(argv: list) -> int:
    targets, want_json = [], False
    for arg in argv:
        if arg == "--json":
            want_json = True
        else:
            targets.append(arg)
    if not targets:
        print("usage: check-completion-report.py <artifact...> [--json]",
              file=sys.stderr)
        return 2
    violations = []
    for name in targets:
        path = Path(name)
        if not path.exists():
            violations.append(f"{path}: missing artifact")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"{path}: unreadable ({exc})")
            continue
        for item in check_report(text):
            violations.append(f"{path}: {item}")
    if want_json:
        print(json.dumps({"total": len(violations),
                          "violations": violations}, ensure_ascii=False, indent=2))
    for item in violations:
        print(f"COMPLETION {item}", file=sys.stderr)
    if violations:
        print(f"COMPLETION: {len(violations)} violation(s); rejected",
              file=sys.stderr)
        return 1
    print("COMPLETION: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
