#!/usr/bin/env bash
# Guarded commit-and-execute flow for the isolated approval slice.
# It never merges, pushes, deploys, or touches the APS-bearing main worktree.
set -euo pipefail

WORKTREE="${1:-/home/dwp/workspace/job/sverpweb-stable-platform-workflow}"
COMMIT_MESSAGE="${2:-feat: isolate purchase approval workflow slice}"
EXPECTED_BRANCH="platform/workflow-phase1"
FAST_CHECK="/home/dwp/ai-batch-runner/examples/snaplink-platform/approval-slice-fast-check.sh"

cd "$WORKTREE"
ROOT="$(git rev-parse --show-toplevel)"
BRANCH="$(git branch --show-current)"

[ "$ROOT" = "$WORKTREE" ] || { echo "unexpected worktree root: $ROOT" >&2; exit 2; }
[ "$BRANCH" = "$EXPECTED_BRANCH" ] || {
  echo "unexpected branch: $BRANCH (expected $EXPECTED_BRANCH)" >&2
  exit 2
}

echo "== pre-commit deterministic check =="
"$FAST_CHECK" "$WORKTREE"

echo "== stage and inspect diff =="
git add -A

git diff --cached --check
if git diff --cached --name-only | grep -E '(^|/)(\.env$|.*\.(key|pem)$|id_rsa|credentials)' >/tmp/approval-slice-secret-paths; then
  echo "refusing to commit possible secret paths:" >&2
  cat /tmp/approval-slice-secret-paths >&2
  exit 2
fi

git diff --cached --stat

echo "== commit =="
git commit -m "$COMMIT_MESSAGE"

echo "== post-commit execution =="
"$FAST_CHECK" "$WORKTREE"
git show --check --stat --oneline HEAD
echo "== final status =="
git status --short
