#!/usr/bin/env bash
# Backend done-gate for the aero-id 5-day campaign.
#
# Two independent refusals, both fail-closed:
#   1. no web/ frontend path may appear in the direction's committed diff, nor
#      as an untracked/unstaged web/ path in the worktree;
#   2. the target repository's full documented Go done-gate must be green.
#
# Run with the direction worktree as cwd (the runner's validator contract).
set -uo pipefail

BASE_REF="${AERO_ID_BACKEND_BASE:-aero-id-backend-5d-20260930}"
WEB_PATH_RE='^web/'

fail() { printf 'REPO-GATE REFUSED: %s\n' "$*" >&2; exit 1; }

# 1a. committed diff against the campaign base
if git rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
  if ! git merge-base --is-ancestor "$BASE_REF" HEAD; then
    fail "branch is not descended from $BASE_REF"
  fi
  web_committed="$(git diff --name-only "$BASE_REF"...HEAD | grep -E "$WEB_PATH_RE" || true)"
  [ -n "$web_committed" ] && fail "committed diff touches web/:
$web_committed"
fi

# 1b. uncommitted or untracked web/ paths
web_pending="$(git status --porcelain=v1 --untracked-files=all -- web/ | sed 's/^...//' || true)"
[ -n "$web_pending" ] && fail "worktree has uncommitted web/ paths:
$web_pending"

# 1c. frontend toolchain must not be part of the change
if git diff --name-only "$BASE_REF"...HEAD 2>/dev/null | grep -qE '(^|/)(package\.json|pnpm-lock\.yaml|vite\.config|tsconfig)'; then
  fail "committed diff changes frontend build/toolchain files"
fi

# 2. the repository's own done-gate
for target in \
  test \
  lint \
  lint-strict \
  check-file-sizes \
  check-audit-swallow \
  check-anchored-event-ids \
  check-audit-manifest \
  check-load-scenarios \
  check-migration-set \
  check-db-startup-hardening \
  check-composition-root \
  check-proto-sync \
  check-architecture \
  check-integration-compile \
  build
do
  printf '--- make %s ---\n' "$target"
  if ! make "$target"; then
    fail "make $target is red"
  fi
done

printf 'repo-gate: done-gate green, no web/ paths\n'
