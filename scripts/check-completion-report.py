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
    if not _has_yaml():
        violations.append("PyYAML not available; falling back to textual checks")
    import yaml  # noqa: F401
    report = None
    for block in blocks:
        try:
            data = yaml.safe_load(block)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("completion_report"), dict):
            report = data["completion_report"]
            break
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
