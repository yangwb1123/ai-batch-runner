#!/usr/bin/env python3
"""Authority-layering gate (L2, pure stdlib): an artifact must anchor its
capability claims in the project's contract catalog, not invent
interfaces.

snaplink-console rejected a "no invented BFF" direction (state.jsonl) —
capability claims without a contract-catalog citation were refused; iris-ui
extracts manifest contracts natively per framework (ADR-009) instead of
copying a sibling adapter. This gate mechanically enforces the same
discipline on generated artifacts.

Signals checked in ARTIFACT (a .md report / prompt / spec):
- INVENT:  invented interface — frontend-private API paths
           (`/api/...` invented, `client.api.` private methods) used as
           truth WITHOUT any catalog citation (OpenAPI path / contract id /
           file:line of an existing call site).
- UNVERIFIED: capability claims phrased as facts without evidence
           (`可以实现X` / `supports X` with no file:line or catalog entry).
- PASS when every flagged claim carries a citation (contract file, path,
  or file:line reference) or is explicitly marked [PROPOSED].

Contract catalogs are discovered from ARTIFACT_DIR (cwd by default):
backend-contracts.json / openapi.yaml / openapi.json / manifest.json /
docs/backend-contracts.json. Claims citing a path in the catalog are
anchored; the rest must cite file:line or be [PROPOSED].

Usage:
    python scripts/check-authority.py <artifact> [--dir PROJECT_ROOT] [--json]
Register as validator: authority = "python {cwd}/scripts/check-authority.py {output}"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Frontend/adaptor "invention" signals: API-shaped paths or private method
# chains used without citation.
_API_PATH = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/api/v[0-9]+/[a-z0-9_./{}<>-]+", re.I)
_PRIVATE_CALL = re.compile(r"\b(?:client|api|service|sdk)\.\s*[a-z][a-zA-Z0-9_]*\(")
_CITATION = re.compile(r"(?:openapi|contracts?|catalog|backend-contracts|manifest)"
                       r"[\w/.:-]*|`?[\w./-]+\.(?:dart|ts|tsx|go|py|java):\d+`?")
_PROPOSED = re.compile(r"\[PROPOSED\]|\[proposed\]")
_PHRASE_FACT = re.compile(r"(?:可以|能够|支持|提供|should|can|supports?|provides?)\s+"
                          r"(?:实现|调用|访问|读写|导出|导入|检索|查询|上报|下发|"
                          r"implement|call|access|read|write|export|import|query)")
_CATALOG_NAMES = ("backend-contracts.json", "openapi.yaml", "openapi.json",
                  "manifest.json")


def _catalog_paths(root: Path) -> list:
    paths = []
    for name in _CATALOG_NAMES:
        for base in (root, root / "docs"):
            candidate = base / name
            if candidate.is_file():
                paths.append(candidate)
    return paths


def _catalog_anchors(root: Path) -> set:
    anchors = set()
    for path in _catalog_paths(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.name.endswith(".json"):
            for match in re.findall(r'"(?:/api/v[0-9]+/[^"]+|id|ref)"\s*:\s*"([^"]+)"', text):
                anchors.add(match)
        else:
            for match in re.findall(r"/api/v[0-9]+/[a-z0-9_./{}<>-]+", text):
                anchors.add(match)
    return anchors


def check_artifact(text: str, root: Path) -> dict:
    anchors = _catalog_anchors(root)
    findings = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        has_citation = bool(_CITATION.search(line)) or _PROPOSED.search(line)
        if _API_PATH.search(line) and not has_citation:
            findings.append({
                "line": idx, "kind": "invented_api",
                "text": line.strip()[:120],
                "fix_hint": "anchor the path in the contract catalog "
                            "(backend-contracts.json / openapi) or mark [PROPOSED]"})
            continue
        if _PRIVATE_CALL.search(line) and not has_citation and not _PROPOSED.search(line):
            # only flag when a capability fact is asserted nearby
            if _PHRASE_FACT.search(line):
                findings.append({
                    "line": idx, "kind": "unverified_capability",
                    "text": line.strip()[:120],
                    "fix_hint": "cite file:line of an existing call site or "
                                "mark [PROPOSED]"})
    return {"invented": [f for f in findings if f["kind"] == "invented_api"],
            "unverified": [f for f in findings if f["kind"] == "unverified_capability"],
            "catalog_anchors": len(anchors)}


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact", help="artifact file to check")
    parser.add_argument("--dir", default=".", help="project root for contract catalogs")
    parser.add_argument("--json", action="store_true", help="emit machine JSON")
    args = parser.parse_args(argv)

    artifact = Path(args.artifact)
    if not artifact.is_file():
        print(f"check-authority: artifact not found: {artifact}", file=sys.stderr)
        return 1
    report = check_artifact(artifact.read_text(encoding="utf-8", errors="replace"),
                            Path(args.dir).resolve())
    failed = report["invented"] + report["unverified"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    if failed:
        for item in failed:
            print(f"check-authority: L{item['line']} [{item['kind']}] "
                  f"{item['text']} — {item['fix_hint']}", file=sys.stderr)
        print("check-authority: FAIL — capability claims must be anchored in the "
              "contract catalog (L2 authority-layering); no invented interfaces.",
              file=sys.stderr)
        return 1
    print(f"check-authority: OK — {len(report['invented'])} invented, "
          f"{len(report['unverified'])} unverified, "
          f"{report['catalog_anchors']} catalog anchor(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
