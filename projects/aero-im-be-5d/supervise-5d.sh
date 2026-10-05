#!/usr/bin/env bash
# aero-im 后端 5 天（120h）无人值守 supervisor。
#
# 职责：
#   1. 硬墙钟：到点即终止（默认 120h，可用 AERO_BE_HOURS 覆盖）。
#   2. provider 熔断看门狗：日志里出现配额/鉴权风暴（默认 15 分钟内 >=8 次
#      provider 失败）立即停机——共享的免费 provider 配额被打爆时继续跑只会
#      空转烧时间，这与仓库自己的 fail-closed 约定一致。
#   3. 绝不自动 merge 进 main：改动留在隔离 worktree 等人审。
#
# 用法：
#   setsid nohup bash supervise-5d.sh > supervisor.log 2>&1 < /dev/null &
#   cat supervisor.pid && tail -f supervisor.log
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${AERO_BE_RUN_DIR:-/tmp/pbatch-aero-im-be-5d}"
HOURS="${AERO_BE_HOURS:-120}"
DEADLINE_SECONDS=$(( HOURS * 3600 ))
# 配额风暴：窗口内 provider 失败次数达到阈值即停机。
STORM_WINDOW_SECONDS="${AERO_BE_STORM_WINDOW:-900}"
STORM_THRESHOLD="${AERO_BE_STORM_THRESHOLD:-8}"
# 单次 campaign invocation 的墙钟上限（略小于总 deadline，留出重启余量）。
SLICE_SECONDS="${AERO_BE_SLICE_SECONDS:-432000}"

# 共享 CARGO_TARGET_DIR：隔离 worktree 各自编译会重复占用 100GB+ 磁盘；共享
# 同一个 target 目录让 cargo 命中缓存。并发已限制为 1，锁竞争可忽略。
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$HOME/aero-im/target}"
export PBATCH_DATA_DIR="$RUN_DIR/runtime"
export PBATCH_PROVIDER_BREAKER_FILE="$RUN_DIR/runtime/provider-breaker.json"
export AERO_IM_REPO="${AERO_IM_REPO:-$HOME/aero-im}"

mkdir -p "$RUN_DIR/runtime"
LOG="$RUN_DIR/campaign.log"
EVENTS="$RUN_DIR/events.jsonl"
CAMPAIGN_STATUS="$RUN_DIR/status"

log() { echo "[$(date -Is)] $*" | tee -a "$RUN_DIR/supervisor.log"; }

START_EPOCH=$(date +%s)
DEADLINE_EPOCH=$(( START_EPOCH + DEADLINE_SECONDS ))
echo $$ > "$RUN_DIR/supervisor.pid"
log "START supervisor pid=$$ hours=$HOURS deadline=$(date -d @"$DEADLINE_EPOCH" -Is)"
log "CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
log "PBATCH_DATA_DIR=$PBATCH_DATA_DIR"

# 计数 provider 失败（provider 熔断器与 agent 退出都写这些标记）。
storm_since() { date -d "@$(( $(date +%s) - STORM_WINDOW_SECONDS ))" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 0; }

QUOTA_PATTERN='provider failure|usage limit|quota|429|rate.?limit|unauthorized|invalid.?api.?key|authentication'

storm_count() {
  # 只统计最近窗口内的 provider 失败行；无日志文件时为 0。
  [ -f "$LOG" ] || { echo 0; return; }
  local since
  since=$(storm_since)
  awk -v since="$since" '
    $0 ~ /'"$QUOTA_PATTERN"'/ {
      # 只取时间戳前缀可解析的行，避免把历史 backlog 计入窗口
      if (match($0, /[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}/)) {
        ts = substr($0, RSTART, RLENGTH)
        gsub("T", " ", ts)
        if (ts >= since) n++
      }
    }
    END { print n + 0 }' "$LOG"
}

# 解析 agent 二进制（每次启动前重新解析）。
#
# 2026-10-01 07:03 的停机就是这么来的：pi 在一次重装期间从 PATH 消失，
# runner 报 `'<path>' not found in PATH`，两个 analysis 任务未启动 → 两轮零进展
# → fail-closed rc=4。那是**环境故障，不是 campaign 无进展**，所以这里必须每次
# 启动前现查，并把它当可重试的环境故障处理，而不是烧掉整个 5 天窗口。
resolve_agent() {
  if [ -n "${AERO_BE_AGENT_BIN:-}" ] && [ -x "${AERO_BE_AGENT_BIN}" ]; then
    echo "$AERO_BE_AGENT_BIN"; return 0
  fi
  command -v pi 2>/dev/null || return 1
}

stop_reason=""
while :; do
  NOW=$(date +%s)
  REMAIN=$(( DEADLINE_EPOCH - NOW ))
  if [ "$REMAIN" -le 0 ]; then
    stop_reason="deadline reached after ${HOURS}h"
    break
  fi
  SLICE=$(( REMAIN < SLICE_SECONDS ? REMAIN : SLICE_SECONDS ))

  # agent 二进制缺失 = 环境故障：等待重试，不计入 campaign 无进展，也不烧窗口。
  AGENT="$(resolve_agent)" || {
    log "ENV: agent binary 'pi' not found in PATH — waiting 300s (environment fault, not a campaign stall)"
    sleep 300
    continue
  }

  log "LAUNCH campaign agent=$AGENT slice=${SLICE}s remaining=${REMAIN}s"
  timeout --signal=TERM --kill-after=300 "$SLICE" \
    bash "$HERE/run-campaign.sh" \
      --focus "Continue hardening the backend Rust domain of aero-im (crates/ and migrations/ only). Prioritize, in order: at-least-once idempotency and re-parking of every claimed row on non-success paths; fail-closed defaults for optional integrations and missing configuration; authorization completeness on room and workspace scoped routes (ImService::assert_room_access with the participant argument first); concurrency and fencing correctness in WebSocket fan-out, call orchestration and SFU paths; honest partial and unavailable states instead of silent success; and missing regression-test coverage for the above. Prefer small, evidence-backed, test-covered fixes inside one crate. ABSOLUTELY NO web/ or frontend work: no web/, no CSS, no SolidJS or Vite, no pnpm. Do not run PostgreSQL, Docker, migrations, or external-network commands; keep PG-gated tests #[ignore]. Do not enable durable-v1 or any account-summary production switch, and do not edit docs/sprint/TODO.md staging or production acceptance items. Read the current code and AGENTS.md first; never re-implement protections that already exist and never weaken an existing gate or test." \
      --reuse --skip-passed --retry-failed --preflight-strict \
      --max-directions 1 --analysis-batch-size 2 \
      --rounds 0 --round-delay 300 \
      --jobs 2 --parallel-pipelines 1 \
      --agent-bin "$AGENT" \
      --provider opencode --model space-bunny-free --thinking max \
      --timeout 14400 --no-progress-timeout 7200 \
      --retries 2 --retry-delay 60 \
      --rate-limit 0.2 --rate-burst 1 \
      --daily-budget 120 --budget-max 1200 \
      --events-file "$EVENTS" \
      --log-file "$LOG" \
      --stream-output none --memory-mode off --no-lock \
      >> "$RUN_DIR/campaign-stdout.log" 2>&1
  RC=$?
  log "campaign exited rc=$RC"

  # campaign 自身 fail-closed 停止（连续无进展 round / preflight 失败）不再重启。
  if [ "$RC" -eq 4 ]; then
    stop_reason="campaign fail-closed stop (rc=4: stalled rounds or preflight)"
    break
  fi
  # 预算上限（rc=3）也算受控停止：让 owner 决定是否加预算。
  if [ "$RC" -eq 3 ]; then
    stop_reason="budget cap reached (rc=3)"
    break
  fi

  STORM=$(storm_count)
  if [ "$STORM" -ge "$STORM_THRESHOLD" ]; then
    stop_reason="provider quota/auth storm: ${STORM} provider failures in ${STORM_WINDOW_SECONDS}s"
    break
  fi
  # campaign 正常结束（方向耗尽）后重启，让 5 天窗口继续找新方向。
  log "RESTART in 60s (storm=${STORM}/${STORM_THRESHOLD})"
  sleep 60
done

printf '%s\n' "stopped: ${stop_reason:-unknown}" > "$CAMPAIGN_STATUS"
printf '%s\n' "$(date -Is)" > "$RUN_DIR/stopped-at"
log "STOP ${stop_reason:-unknown}"
exit 0
