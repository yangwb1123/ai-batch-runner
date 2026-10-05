#!/usr/bin/env bash
# aero-id backend-only 5-day campaign supervisor.
#
#   run-5d.sh            # run until the outer timeout fires (5 days)
#
# Shape:
#   * configs are owned by ~/ai-batch-runner/projects/aero-id/backend5d/ and are
#     synced into the isolated campaign worktree's gitignored .pi-batch/ tree;
#   * the campaign runs against an isolated worktree/branch, so the main
#     repository is never modified;
#   * auto-merge targets the campaign branch only, and the merge gate is the
#     full Go done-gate plus a fail-closed web/ refusal;
#   * a provider outage must not consume the window: rounds that produce no
#     progress back off exponentially and the campaign is relaunched, keeping
#     durable state so completed work is never repeated.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_ROOT="$(cd "$HERE/../../.." && pwd)"
REPO="${AERO_ID_BACKEND_REPO:-/home/u1/aero-id-backend-5d}"
BASE_BRANCH="${AERO_ID_BACKEND_BASE:-aero-id-backend-5d-20260930}"
SYNC_DIR="$REPO/.pi-batch/aero-id-backend5d"
RUNTIME="$SYNC_DIR/runtime"
LOG_DIR="$SYNC_DIR"
BACKOFF_MINUTES=5
MAX_BACKOFF_MINUTES=45

MODEL_ARGS=(--agent-bin /usr/local/bin/pi --provider opencode --model space-bunny-free
            --thinking max --memory-mode off --stream-output none)

FOCUS='backend internal/app internal/config internal/connector internal/domain internal/event internal/handler internal/idempotency internal/middleware internal/outbox internal/pkg internal/region internal/repository internal/service internal/sso internal/storage cmd configs scripts api/proto migrations; prioritize authority and tenant scope enforcement, source capability and audience binding, durable audit event outbox correctness, operation and sync unknown outcomes and reconciliation, home region routing fencing replication safety, callback SSRF and delivery invariants, migration correctness, startup fail-closed validation, production operability; absolutely no frontend work'

# A campaign must survive the death of the session that launched it: ignore
# hangup, and treat terminate/interrupt as a logged event rather than an exit.
trap 'printf "[%s] supervisor received SIGHUP\n" "$(date -Is)"' HUP
trap 'printf "[%s] supervisor received SIGTERM/SIGINT; continuing\n" "$(date -Is)"' TERM INT

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

sync_configs() {
  mkdir -p "$SYNC_DIR" "$RUNTIME"
  cp "$HERE/campaign.yaml" "$SYNC_DIR/campaign.yaml"
  cp "$HERE/campaign-pipeline.yaml" "$SYNC_DIR/campaign-pipeline.yaml"
  cp "$HERE/done-gate.sh" "$SYNC_DIR/done-gate.sh"
  chmod 700 "$SYNC_DIR/done-gate.sh"
  export PBATCH_PROJECT_CONFIG="$HERE/pi-batch.yaml"
}

cd "$REPO" || { log "FATAL: campaign repo $REPO missing"; exit 20; }
sync_configs
# Seed the durable state from the repository's own history so the campaign can
# see which directions were already completed, and create the state file so the
# round accounting below can always read it.
if [ ! -s "$SYNC_DIR/state.jsonl" ] && [ -f "$REPO/docs/auto/state.jsonl" ]; then
  cp "$REPO/docs/auto/state.jsonl" "$SYNC_DIR/state.jsonl"
  log "seeded campaign state from $REPO/docs/auto/state.jsonl ($(wc -l < "$SYNC_DIR/state.jsonl") events)"
fi
: >> "$SYNC_DIR/state.jsonl"

log "repo=$REPO base=$BASE_BRANCH head=$(git rev-parse --short HEAD)"
log "backend-only: discovery excludes web/; done-gate.sh refuses any web/ path"

# Baseline: never start a 5-day window on a red tree.
if ! bash "$SYNC_DIR/done-gate.sh" >"$LOG_DIR/baseline-gate.log" 2>&1; then
  log "FATAL: baseline done-gate is red; see $LOG_DIR/baseline-gate.log"
  tail -30 "$LOG_DIR/baseline-gate.log"
  exit 21
fi
log "baseline done-gate green"

backoff=$BACKOFF_MINUTES
while true; do
  log "campaign launch (backoff ${backoff}m between rounds)"
  before="$(wc -l < "$SYNC_DIR/state.jsonl" 2>/dev/null || echo 0)"
  python3 "$RUNNER_ROOT/pi-batch.py" campaign \
    --config "$SYNC_DIR/campaign.yaml" \
    --focus "$FOCUS" \
    --data-dir "$RUNTIME" \
    --output-dir "$SYNC_DIR/auto" \
    --state-file "$SYNC_DIR/state.jsonl" \
    --summary-file "$SYNC_DIR/SUMMARY.md" \
    --events-file "$SYNC_DIR/events.jsonl" \
    --log-file "$LOG_DIR/campaign.log" \
    --reuse --skip-passed --retry-failed \
    --rounds 1 --round-delay 120 --no-progress-timeout 5400 \
    --jobs 2 --parallel-pipelines 2 \
    --retries 2 --retry-delay 60 \
    --rate-limit 0.2 --rate-burst 2 --wait-lock 30 \
    --auto-merge --merge-squash --merge-wait-lock 30 \
    --merge-gate "bash $SYNC_DIR/done-gate.sh" \
    "${MODEL_ARGS[@]}" >>"$LOG_DIR/campaign-stdout.log" 2>&1
  rc=$?
  after="$(wc -l < "$SYNC_DIR/state.jsonl" 2>/dev/null || echo 0)"
  log "campaign exited rc=$rc; state events $before -> $after"
  git -C "$REPO" log --oneline "$BASE_BRANCH"..HEAD 2>/dev/null | head -20 | sed 's/^/[landed] /'

  passed="$(grep -c '"status": "PASSED"' "$SYNC_DIR/state.jsonl" 2>/dev/null || echo 0)"
  if [ "$after" -le "$before" ] || [ "$rc" -ne 0 ]; then
    log "no progress this round (rc=$rc); sleeping ${backoff}m before relaunch"
    sleep $((backoff * 60))
    backoff=$((backoff * 2))
    [ "$backoff" -gt "$MAX_BACKOFF_MINUTES" ] && backoff=$MAX_BACKOFF_MINUTES
  else
    backoff=$BACKOFF_MINUTES
    sleep 120
  fi
  log "cumulative PASSED directions in state: $passed"
done
