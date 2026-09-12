#!/usr/bin/env bash
# Guarded: check the isolated slice, merge it into main, commit, then execute
# the local verification/runtime smoke check. Never push or deploy.
set -euo pipefail

TARGET="${1:-/home/dwp/workspace/job/sverpweb-stable-platform-workflow}"
MAIN="${2:-/home/dwp/workspace/job/sverpweb-stable}"
TARGET_BRANCH="${TARGET_BRANCH:-platform/workflow-phase1}"
HEALTH_URL="${APPROVAL_SLICE_HEALTH_URL:-http://127.0.0.1:13114/health}"
FAST_CHECK="/home/dwp/ai-batch-runner/examples/snaplink-platform/approval-slice-fast-check.sh"
COMMIT_MESSAGE="${COMMIT_MESSAGE:-merge: integrate approval workflow slice}"

fail() { echo "merge approval slice: $*" >&2; exit 2; }

TARGET_ROOT="$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null)" ||
  fail "target is not a git worktree: $TARGET"
MAIN_ROOT="$(git -C "$MAIN" rev-parse --show-toplevel 2>/dev/null)" ||
  fail "main is not a git worktree: $MAIN"
[ "$TARGET_ROOT" = "$TARGET" ] || fail "unexpected target root: $TARGET_ROOT"
[ "$MAIN_ROOT" = "$MAIN" ] || fail "unexpected main root: $MAIN_ROOT"
[ "$(git -C "$TARGET" branch --show-current)" = "$TARGET_BRANCH" ] ||
  fail "target branch is not $TARGET_BRANCH"
[ "$(git -C "$MAIN" branch --show-current)" = "main" ] ||
  fail "main worktree is not on main"

git -C "$TARGET" diff --check
git -C "$TARGET" diff --quiet || fail "target has unstaged tracked changes"
[ -z "$(git -C "$TARGET" status --porcelain)" ] || fail "target is not clean"
TARGET_COMMIT="$(git -C "$TARGET" rev-parse HEAD)"

# Run the expensive part once, before touching the main index.
"$FAST_CHECK" "$TARGET"

git -C "$MAIN" diff --check
git -C "$MAIN" diff --quiet || fail "main has tracked changes; refusing to overwrite them"
BEFORE_UNTRACKED="$(mktemp)"
AFTER_UNTRACKED="$(mktemp)"
trap 'rm -f "$BEFORE_UNTRACKED" "$AFTER_UNTRACKED"' EXIT
git -C "$MAIN" ls-files --others --exclude-standard | sort >"$BEFORE_UNTRACKED"
while IFS= read -r path; do
  if git -C "$MAIN" cat-file -e "$TARGET_COMMIT:$path" 2>/dev/null; then
    fail "main untracked path collides with target commit: $path"
  fi
done <"$BEFORE_UNTRACKED"

if git -C "$MAIN" merge-base --is-ancestor "$TARGET_COMMIT" main; then
  echo "target is already integrated into main: $TARGET_COMMIT"
else
  git -C "$MAIN" merge --no-commit --no-ff "$TARGET_COMMIT" || {
    git -C "$MAIN" merge --abort 2>/dev/null || true
    fail "merge conflict; merge was aborted and no commit was created"
  }
  git -C "$MAIN" add -u
  git -C "$MAIN" diff --cached --check
  if git -C "$MAIN" diff --cached --name-only | grep -E '(^|/)(\.env$|.*\.(key|pem)$|id_rsa|credentials)' >/tmp/approval-slice-merge-secret-paths; then
    git -C "$MAIN" merge --abort 2>/dev/null || true
    fail "possible secret path in merge"
  fi
  git -C "$MAIN" commit -m "$COMMIT_MESSAGE"
fi

git -C "$MAIN" ls-files --others --exclude-standard | sort >"$AFTER_UNTRACKED"
diff -u "$BEFORE_UNTRACKED" "$AFTER_UNTRACKED"

# Execute after commit: complete deterministic checks and a bounded local smoke.
"$FAST_CHECK" "$MAIN"
if [ -n "$HEALTH_URL" ]; then
  curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/tmp/approval-slice-health.json
  printf 'runtime health: '
  head -c 300 /tmp/approval-slice-health.json
  printf '\n'
fi

git -C "$MAIN" show --check --stat --oneline HEAD
git -C "$MAIN" status --short
