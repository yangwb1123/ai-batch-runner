#!/usr/bin/env bash
# 24-hour governed implementation loop for the isolated sverpweb approval slice.
#
# Writers are deliberately serialized because one dirty worktree cannot safely
# accept concurrent edits. Reviewers fan out concurrently and are read-only;
# each repair/next-task writer is followed by local checks and an independent
# read-only gate. Nothing is committed or merged automatically.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET="/home/dwp/workspace/job/sverpweb-stable-platform-workflow"
RUN_ROOT="$ROOT/logs/approval-slice-247"
MASTER_LOG="$RUN_ROOT/master.log"
EVENTS="$RUN_ROOT/events.jsonl"
DAILY_STATE="$RUN_ROOT/daily-budget.json"
LOCK_FILE="$RUN_ROOT/controller.lock"
LEDGER="$ROOT/docs/snaplink-platform/phase5/next-task-ledger.md"
GATE_REPORT="$ROOT/docs/snaplink-platform/phase5/code-gate.md"
NEXT_REPORT="$TARGET/docs/workflow/phase1-next-task-report.md"

mkdir -p "$RUN_ROOT" "$TARGET/docs/workflow"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  printf '%s controller already running; refusing a second 24h loop\n' "$(date -u +%FT%TZ)" >>"$MASTER_LOG"
  exit 5
fi

START_EPOCH="$(date +%s)"
DEADLINE=$((START_EPOCH + 24 * 60 * 60))
CYCLE=0
NEXT_TASK_NO=0
CURRENT_PID=""

note() {
  printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >>"$MASTER_LOG"
}

source_status() {
  # Reports and .pi-batch state are intentionally excluded. This answers
  # whether an implementation agent changed source/config, not just its report.
  git -C "$TARGET" status --porcelain -- \
    .env.example README.md apps/api apps/web docs/audit-integration.md \
    | sha256sum | awk '{print $1}'
}

has_target_agent() {
  local pid cwd
  for pid in $(pgrep -x pi 2>/dev/null || true); do
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    if [[ "$cwd" == "$TARGET" ]]; then
      return 0
    fi
  done
  return 1
}

wait_for_target_agents() {
  local waited=0
  while has_target_agent; do
    if (( $(date +%s) >= DEADLINE )); then
      note "deadline reached while waiting for an existing target agent"
      return 1
    fi
    if (( waited == 0 )); then
      note "waiting for an already-running target agent to finish"
    fi
    waited=$((waited + 1))
    sleep 30
  done
  return 0
}

run_step() {
  local label="$1"
  shift
  local log="$RUN_ROOT/cycle-${CYCLE}-${label}.log"
  local rc
  if (( $(date +%s) + 120 >= DEADLINE )); then
    note "skip $label: less than two minutes remain"
    return 124
  fi
  note "start cycle=$CYCLE step=$label"
  # The per-step cap prevents a hung model from consuming the whole window;
  # pbatch itself kills its agent process group on timeout.
  timeout --foreground --signal=TERM --kill-after=90s 2400s "$@" \
    >>"$log" 2>&1 &
  CURRENT_PID=$!
  wait "$CURRENT_PID"
  rc=$?
  CURRENT_PID=""
  note "finish cycle=$CYCLE step=$label rc=$rc log=$log"
  return "$rc"
}

cleanup() {
  local rc=$?
  if [[ -n "$CURRENT_PID" ]] && kill -0 "$CURRENT_PID" 2>/dev/null; then
    kill -TERM "$CURRENT_PID" 2>/dev/null || true
  fi
  note "controller stopped rc=$rc"
  exit "$rc"
}
trap cleanup INT TERM HUP

# Do not race the repair invocation already launched before this controller.
wait_for_target_agents || exit 124

PBATCH=(
  python "$ROOT/pi-batch.py"
  --agent-bin pi
  --model gpt-5.6-luna
  --provider openai-codex
  --thinking max
  --memory-mode off
  --stream-output none
  --timeout 1800
  --retries 1
  --retry-delay 30
  --wait-lock 30
  --daily-invocations 48
  --daily-state "$DAILY_STATE"
  --events-file "$EVENTS"
  --log-max-bytes 5242880
  --log-backups 5
)

run_pipeline() {
  local label="$1"
  local pipeline="$2"
  run_step "$label" "${PBATCH[@]}" "$pipeline" --no-git-commit
}

run_local_checks() {
  run_step "typecheck" bash -lc "cd '$TARGET' && pnpm --filter @sverp/api typecheck" || true
  run_step "api-test" bash -lc "cd '$TARGET' && pnpm --filter @sverp/api test" || true
  run_step "api-build" bash -lc "cd '$TARGET' && pnpm --filter @sverp/api build" || true
  run_step "repo-check" bash -lc "cd '$TARGET' && pnpm check" || true
  run_step "quality" bash -lc "python '$ROOT/quality.py' --strict '$TARGET'" || true
}

gate_verdict() {
  if [[ ! -s "$GATE_REPORT" ]]; then
    printf 'MISSING\n'
    return
  fi
  tail -n 12 "$GATE_REPORT" | grep -E '^VERDICT:' | tail -n 1 || printf 'MISSING\n'
}

write_ledger() {
  local text="$1"
  {
    printf '\n### %s — cycle %s\n\n' "$(date -u +%FT%TZ)" "$CYCLE"
    printf '%s\n' "$text"
  } >>"$LEDGER"
}

# The current repair may have left useful changes even if its supervisor timed
# out. Establish a fresh local baseline and gate before scheduling new work.
CYCLE=0
run_local_checks
run_pipeline "initial-gate" "$ROOT/examples/snaplink-platform/platform-code-gate-pipeline.yaml" || true
VERDICT="$(gate_verdict)"
note "initial gate: $VERDICT"

while (( $(date +%s) < DEADLINE )); do
  CYCLE=$((CYCLE + 1))

  if [[ "$VERDICT" != VERDICT:\ PASS* ]]; then
    # Three independent read-only reviewers run concurrently in one pbatch
    # stage. Their reports feed the next serialized repair writer.
    wait_for_target_agents || break
    run_step "parallel-review" "${PBATCH[@]}" \
      "$ROOT/examples/snaplink-platform/approval-slice-parallel-review-tasks.yaml" \
      --mode parallel --workers 3 --no-git-commit || true
    wait_for_target_agents || break
    run_pipeline "repair-v3" "$ROOT/examples/snaplink-platform/approval-slice-repair-v3.yaml" || true
    wait_for_target_agents || break
    run_local_checks
    run_pipeline "code-gate" "$ROOT/examples/snaplink-platform/platform-code-gate-pipeline.yaml" || true
    VERDICT="$(gate_verdict)"
    note "cycle=$CYCLE gate: $VERDICT"
    write_ledger "Repair/gate cycle completed. Latest verdict: $VERDICT"
    if [[ "$VERDICT" != VERDICT:\ PASS* ]]; then
      sleep 60
      continue
    fi
  fi

  # Once the current slice passes, hand the model exactly one bounded,
  # locally-provable next task. A task that finds only external evidence gaps
  # must leave source unchanged and returns BLOCKED; this is the stop condition
  # rather than an excuse to invent production evidence.
  if (( NEXT_TASK_NO >= 4 )); then
    note "four bounded next tasks completed; no further autonomous scope is queued"
    break
  fi
  NEXT_TASK_NO=$((NEXT_TASK_NO + 1))
  BEFORE_SOURCE="$(source_status)"
  wait_for_target_agents || break
  run_pipeline "next-task-${NEXT_TASK_NO}" "$ROOT/examples/snaplink-platform/approval-slice-next-task.yaml" || true
  wait_for_target_agents || break
  run_local_checks
  run_pipeline "next-gate-${NEXT_TASK_NO}" "$ROOT/examples/snaplink-platform/platform-code-gate-pipeline.yaml" || true
  VERDICT="$(gate_verdict)"
  AFTER_SOURCE="$(source_status)"
  note "next-task=$NEXT_TASK_NO gate: $VERDICT source_before=$BEFORE_SOURCE source_after=$AFTER_SOURCE"
  write_ledger "Next task $NEXT_TASK_NO completed. Verdict: $VERDICT"

  # A failed next-task gate is still a code defect, even when the selector
  # made no edit. Return to the concurrent-review/repair path rather than
  # stopping on an unresolved blocker. Only a PASS with no new local work is
  # a legitimate fail-closed completion condition.
  if [[ "$VERDICT" != VERDICT:\ PASS* ]]; then
    note "next task failed its gate; returning to repair loop"
    sleep 60
    continue
  fi
  if [[ "$BEFORE_SOURCE" == "$AFTER_SOURCE" ]] || \
     grep -Eiq '(^|[^A-Z])(BLOCKED|no safe|external evidence only)' "$NEXT_REPORT" 2>/dev/null; then
    note "no new locally implementable source task was produced; stopping fail-closed"
    break
  fi
  sleep 60
done

note "24h controller finished; cycles=$CYCLE next_tasks=$NEXT_TASK_NO final_verdict=$VERDICT"
exit 0
