#!/usr/bin/env bash
# Deterministic local handoff check.  This intentionally does not start an AI
# agent; use the read-only reviewer only once after this check is green.
set -euo pipefail

WORKTREE="${1:-/home/dwp/workspace/job/sverpweb-stable-platform-workflow}"
cd "$WORKTREE"

pnpm --filter @sverp/api test
pnpm --filter @sverp/api typecheck
pnpm --filter @sverp/api build
pnpm check
python /home/dwp/ai-batch-runner/quality.py --strict "$WORKTREE"
git diff --check

python3 - <<'PY'
from pathlib import Path

root = Path("apps/api/src/workflow")
for path in sorted(root.glob("*.ts")):
    if path.name.endswith(".test.ts"):
        continue
    source = path.read_text(encoding="utf-8")
    for forbidden in ("@/purchase", "PurchaseAggregate", "PurchaseRequest", "purchase_request", "purchase-approval"):
        if forbidden in source:
            raise SystemExit(f"workflow kernel boundary violation: {path}: {forbidden}")
print("workflow kernel boundary: PASS")
PY

echo "approval slice fast check: PASS"
