#!/usr/bin/env python3
"""Change-budget gate (L9, pure stdlib): per-iteration change budget with
allowlist/denylist — iris-ui's ≤5 files / ≤300 lines (machine-reported by
change-budget) and snaplink-console's login-header 9-file whitelist.

Budget source (searched in order):
  1. --budget "files=5,lines=300"
  2. DIR/docs/evolution/constitution.yaml `change_budget:` block
  3. defaults: files=0 (unlimited), lines=0 (unlimited)

Constitution shape:
    change_budget:
      max_files: 5
      max_lines: 300
      allowlist: ["lib/screens/oidc_login/**"]   # non-empty => ONLY these
      denylist: ["docs/**", "*.lock"]

Scope: `git diff` against BASE (default: HEAD) restricted to a PATH
(default: cwd, or a pipeline-declared source root via --path). Untracked
files under PATH count as changed (they are new work). Binary/lock files
are counted in files but excluded from line accounting.

Exit 0 = within budget; exit 1 = over budget / allowlist violation /
denylist violation (stderr names the offending files, feeding the retry
prompt). --json emits the report.

Usage:
    python scripts/check-changebudget.py [--dir DIR] [--path SUBPATH]
        [--base HEAD] [--budget files=5,lines=300] [--json]
Register as validator: changebudget = "python {cwd}/scripts/check-changebudget.py --dir {cwd}"
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULTS = {"max_files": 0, "max_lines": 0, "allowlist": [], "denylist": []}


def _constitution_budget(root: Path) -> dict:
    for name in ("docs/evolution/constitution.yaml", "evolution.yaml"):
        candidate = root / name
        if not candidate.is_file():
            continue
        budget = _parse_budget_block(candidate.read_text(encoding="utf-8",
                                                         errors="replace"))
        if budget is not None:
            return budget
    return dict(DEFAULTS)


def _parse_budget_block(text: str) -> Optional[dict]:
    """从 constitution 文本解析 change_budget 块；无块返回 None。"""
    block = re.search(r"change_budget:\s*\n((?:[ \t]+.*\n)*)", text)
    if not block:
        return None
    budget = dict(DEFAULTS)
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"(\w+):\s*(.+)$", stripped)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key in ("max_files", "max_lines"):
            try:
                budget[key] = int(value)
            except ValueError:
                pass
        elif key in ("allowlist", "denylist"):
            budget[key] = [item.strip("'\" []") for item in
                           value.strip("[]").split(",") if item.strip()]
    return budget


def _git(workdir: Path, args: list) -> str:
    proc = subprocess.run(["git", "-C", str(workdir)] + args,
                          capture_output=True, text=True)
    return proc.stdout


def _changed_files(workdir: Path, base: str, path: str) -> list:
    """Tracked changes + untracked files under PATH (relative paths)."""
    changed = set()
    diff = _git(workdir, ["diff", "--name-only", base])
    for line in diff.splitlines():
        if line.strip():
            changed.add(line.strip())
    staged = _git(workdir, ["diff", "--cached", "--name-only"])
    for line in staged.splitlines():
        if line.strip():
            changed.add(line.strip())
    untracked = _git(workdir, ["ls-files", "--others", "--exclude-standard"])
    for line in untracked.splitlines():
        if line.strip():
            changed.add(line.strip())
    rel_path = path.rstrip("/")
    if rel_path and rel_path != ".":
        return sorted(p for p in changed
                      if p == rel_path or p.startswith(rel_path + "/"))
    return sorted(changed)


def _file_lines(workdir: Path, rel: str) -> int:
    try:
        text = (workdir / rel).read_text(encoding="utf-8", errors="replace")
        return len(text.splitlines())
    except OSError:
        return 0


def _match_glob(rel: str, patterns: list) -> bool:
    for pattern in patterns:
        if not pattern:
            continue
        regex = re.escape(pattern).replace(r"\*\*", "__DOUBLE__").replace(r"\*", "[^/]*")
        regex = regex.replace("__DOUBLE__", ".*")
        if re.fullmatch(regex, rel):
            return True
    return False


def _budget_violations(changed: list, total_lines: int, budget: dict) -> list:
    """预算违规：denylist / allowlist / 文件数 / 行数。"""
    violations = []
    if budget["denylist"]:
        for rel in changed:
            if _match_glob(rel, budget["denylist"]):
                violations.append(f"denylisted file changed: {rel}")
    if budget["allowlist"]:
        for rel in changed:
            if not _match_glob(rel, budget["allowlist"]):
                violations.append(f"file outside allowlist changed: {rel}")
    if budget["max_files"] and len(changed) > budget["max_files"]:
        violations.append(f"{len(changed)} files changed > budget {budget['max_files']}")
    if budget["max_lines"] and total_lines > budget["max_lines"]:
        violations.append(f"{total_lines} lines changed > budget {budget['max_lines']}")
    return violations


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=".", help="git worktree root (default: cwd)")
    parser.add_argument("--path", default=".", help="path scope for diff (default: whole tree)")
    parser.add_argument("--base", default="HEAD", help="diff base (default: HEAD)")
    parser.add_argument("--budget", default="", help='inline budget "files=5,lines=300"')
    parser.add_argument("--json", action="store_true", help="emit machine JSON")
    args = parser.parse_args(argv)

    root = Path(args.dir).resolve()
    if not (root / ".git").exists() and not (root / ".git").is_file():
        print(f"check-changebudget: not a git worktree: {root}", file=sys.stderr)
        return 1
    budget = _constitution_budget(root)
    if args.budget:
        for token in args.budget.split(","):
            key, _, value = token.partition("=")
            if key == "files":
                budget["max_files"] = int(value)
            elif key == "lines":
                budget["max_lines"] = int(value)

    changed = _changed_files(root, args.base, args.path)
    total_lines = sum(_file_lines(root, rel) for rel in changed)
    violations = _budget_violations(changed, total_lines, budget)

    report = {"changed": changed, "files": len(changed), "lines": total_lines,
              "budget": budget, "violations": violations}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    if violations:
        for item in violations:
            print(f"check-changebudget: {item}", file=sys.stderr)
        print("check-changebudget: FAIL — change-budget (L9): keep iterations "
              "small and scoped (≤5 files / ≤300 lines, allowlist/denylist); "
              "no drive-by refactors.", file=sys.stderr)
        return 1
    print(f"check-changebudget: OK — {len(changed)} files / {total_lines} lines "
          f"(budget files={budget['max_files'] or '∞'} lines={budget['max_lines'] or '∞'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
