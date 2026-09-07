#!/usr/bin/env bash
# 24-hour source-first SVERP migration supervisor.
#
# Analysis and meta reviewers run concurrently (jobs=4 and bounded role fan-out).
# Implementation stays serial by design: each passed direction is committed on
# the target branch; parallel implementation worktrees are not auto-merged.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${SVERPWEB_ROOT:-$HOME/workspace/job/sverpweb-stable}"
CONFIG_SOURCE="$ROOT/projects/sverp/aps-porting-24h.yaml"
PIPELINE_SOURCE="$ROOT/projects/sverp/sverpweb-campaign-pipeline.yaml"
SYNC_DIR="$REPO/.pi-batch/sverp"
LOG="${SVERP_PORTING_LOG:-$REPO/logs/aps-porting-24h.supervisor.log}"

[[ -d "$REPO" ]] || { echo "FAIL: target repository is missing: $REPO" >&2; exit 1; }
[[ -f "$CONFIG_SOURCE" && -f "$PIPELINE_SOURCE" ]] || {
  echo "FAIL: campaign assets are missing under $ROOT" >&2
  exit 1
}

mkdir -p "$SYNC_DIR" "$(dirname "$LOG")"
cp "$CONFIG_SOURCE" "$SYNC_DIR/aps-porting.yaml"
cp "$PIPELINE_SOURCE" "$SYNC_DIR/aps-porting-pipeline.yaml"

# Keep the background process observable in one append-only supervisor log.
exec >>"$LOG" 2>&1

echo "[$(date -Is)] APS porting supervisor started; target=$REPO"
echo "[$(date -Is)] provider=openai-codex model=gpt-5.6-luna thinking=max"
echo "[$(date -Is)] deadline=24h; analysis_jobs=4; implementation_concurrency=1"

cd "$REPO"
deadline=$(( $(date +%s) + 86400 ))
child_pid=""
stop() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    echo "[$(date -Is)] stop requested; forwarding SIGTERM to campaign pid=$child_pid"
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 130
}
trap stop INT TERM

while (( $(date +%s) < deadline )); do
  echo "[$(date -Is)] starting/restarting campaign round loop"
  set +e
  PBATCH_SCRIPT_DIR="$ROOT" env -u PI_MODEL -u PI_PROVIDER \
    python "$ROOT/pi-batch.py" campaign \
      --config "$SYNC_DIR/aps-porting.yaml" \
      --jobs 4 \
      --max-directions 1 \
      --rounds 0 \
      --round-delay 120 \
      --reuse \
      --retry-failed \
      --agent-bin pi \
      --model gpt-5.6-luna \
      --provider openai-codex \
      --thinking max \
      --memory-mode auto \
      --stream-output none \
      --wait-lock 30 \
      --log-file "" &
  child_pid=$!
  wait "$child_pid"
  rc=$?
  child_pid=""
  set -e
  echo "[$(date -Is)] campaign exited rc=$rc"

  if (( $(date +%s) >= deadline )); then
    break
  fi
  # Unexpected exits are recoverable; do not spin on a broken provider or
  # malformed config. The campaign's provider breaker and state ledger retain
  # the evidence, and the next attempt starts after a bounded delay.
  if [[ "$rc" == 130 || "$rc" == 143 ]]; then
    break
  fi
  sleep 60
 done

echo "[$(date -Is)] APS porting supervisor finished"
