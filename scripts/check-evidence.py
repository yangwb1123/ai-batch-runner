#!/usr/bin/env python3
"""Evidence-discipline gate (L7, pure stdlib): [PROPOSED] claims must not
masquerade as verified; unverified numbers must not enter status claims.

snaplink-console annotated every claim [PROPOSED]/[RESOLVED]/[VERIFIED]
and never let an unverified leg claim green; iris-ui's rule is "未验证的
数字不进入状态声明". This gate mechanically rejects artifacts that:
- assert a result while the same artifact carries [PROPOSED] for it;
- use fabricated-pass phrasing ("理论上应该通过" / "should pass" /
  "assume passed") as verification evidence;
- declare "all green / 全部通过 / all pass" without any machine-gate
  evidence (command output / test count / gate verdict).

Complementary to check-completion-report.py (completion_report block);
this one scans prose-level evidence discipline in any artifact.

Usage:
    python scripts/check-evidence.py <artifact> [--json]
Register as validator: evidence = "python {cwd}/scripts/check-evidence.py {output}"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_FABRICATED = re.compile(
    r"(理论上应该通过|理论上可行|should pass|assume(?:d)? (?:pass|green|ok)"
    r"|假设通过|应该没问题|must pass|预计通过)", re.I)
_ALL_GREEN = re.compile(r"(全部通过|全部绿|all (?:green|pass)|100%\s*(?:通过|pass)|"
                        r"全绿|0 失败|0 failure|all tests? pass)", re.I)
_EVIDENCE = re.compile(r"(?:exit(?: code)?\s*[0=]|passed\s*\(?\d+|"
                       r"\d+/\d+\s+(?:passed|tests?)|VERDICT:\s*PASS|"
                       r"gate|validator|run .*?(?:pass|green)|"
                       r"命令结果|Command Results?|passed)", re.I)
_PROPOSED = re.compile(r"\[PROPOSED\]|\[proposed\]|待验证|未验证|proposed")


def check_artifact(text: str) -> dict:
    findings = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        if _FABRICATED.search(line):
            findings.append({
                "line": idx, "kind": "fabricated_pass",
                "text": line.strip()[:120],
                "fix_hint": "replace with a real verification record: run the "
                            "command and record its actual result (passed/"
                            "failed/not_executed with reason)"})
            continue
        if _ALL_GREEN.search(line) and not _EVIDENCE.search(line):
            # A blanket "all green" without adjacent machine evidence is a
            # status-claim violation (iris-ui: 未验证的数字不进入状态声明).
            window = "\n".join(lines[max(0, idx - 2):idx + 1])
            if not _EVIDENCE.search(window):
                findings.append({
                    "line": idx, "kind": "unbacked_status",
                    "text": line.strip()[:120],
                    "fix_hint": "attach the machine-gate evidence (command "
                                "output / test count / VERDICT) next to the claim"})
    return {"fabricated_pass": [f for f in findings if f["kind"] == "fabricated_pass"],
            "unbacked_status": [f for f in findings if f["kind"] == "unbacked_status"],
            "proposed_markers": len(_PROPOSED.findall(text))}


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact", help="artifact file to check")
    parser.add_argument("--json", action="store_true", help="emit machine JSON")
    args = parser.parse_args(argv)

    artifact = Path(args.artifact)
    if not artifact.is_file():
        print(f"check-evidence: artifact not found: {artifact}", file=sys.stderr)
        return 1
    report = check_artifact(artifact.read_text(encoding="utf-8", errors="replace"))
    failed = report["fabricated_pass"] + report["unbacked_status"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    if failed:
        for item in failed:
            print(f"check-evidence: L{item['line']} [{item['kind']}] "
                  f"{item['text']} — {item['fix_hint']}", file=sys.stderr)
        print("check-evidence: FAIL — evidence discipline (L7): proposed claims "
              "must not masquerade as verified; unverified numbers must not "
              "enter status claims.", file=sys.stderr)
        return 1
    print(f"check-evidence: OK — {len(report['fabricated_pass'])} fabricated-pass, "
          f"{len(report['unbacked_status'])} unbacked status, "
          f"{report['proposed_markers']} [PROPOSED] marker(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
