#!/usr/bin/env bash
# 平台 7×24 无人值守脚本（6 仓库轮巡）
#
# 用法:
#   bash examples/snaplink-platform/platform-247.sh [轮数] [间隔秒] [目标仓库...]
#   bash examples/snaplink-platform/platform-247.sh 3 300          # 3 轮，每轮间隔 5 分钟
#   bash examples/snaplink-platform/platform-247.sh 0 600 snaplink aero-vault  # 无限轮，指定仓库
#
# 每轮对每个仓库依次执行:
#   1. 滚动分析 1 轮（--reuse --retry-failed，共享会话延续上下文）
#   2. 失败任务重试（--max-rounds 1，熔断保护）
#   3. 决策日志追加 + git 提交 + 归档（流水线内已配置，此脚本只汇总）
# 所有输出落 logs/platform-247-<仓库>.log，末尾汇总到 logs/platform-247-summary.log。
# 配合 systemd timer / cron / nohup 常驻即可 7×24。

set -u
ROUNDS="${1:-1}"
INTERVAL="${2:-120}"
shift 2 2>/dev/null || true

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLATFORM_REPOS=(
  "$HOME/workspace/demo/snaplink"
  "$HOME/workspace/demo/snaplink-console"
  "$HOME/workspace/demo/snaplink-audit-governance"
  "$HOME/aero-id"
  "$HOME/aero-vault"
  "$HOME/aero-im"
)
if [ "$#" -gt 0 ]; then
  SELECTED=("$@")
  FILTERED=()
  for name in "${SELECTED[@]}"; do
    for repo in "${PLATFORM_REPOS[@]}"; do
      case "$repo" in
        *"$name") FILTERED+=("$repo") ;;
      esac
    done
  done
  PLATFORM_REPOS=("${FILTERED[@]}")
fi

mkdir -p "$ROOT/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
SUMMARY="$ROOT/logs/platform-247-summary.log"

run_repo_round() {
  local repo="$1" round="$2"
  local name
  name="$(basename "$repo")"
  local log="$ROOT/logs/platform-247-${name}.log"
  echo "[$(date +%H:%M:%S)] round=$round repo=$name start" >>"$SUMMARY"

  # 1) 仓库内必须有 pi-batch.py（薄壳）。无则复制主仓库版本。
  if [ ! -f "$repo/pi-batch.py" ]; then
    echo "  [SKIP] $name has no pi-batch.py — sync ai-batch-runner first" >>"$SUMMARY"
    return
  fi

  # 2) 滚动分析一轮（共享会话、复用指纹、失败重试、决策日志、归档）
  (cd "$repo" && python pi-batch.py \
    -p "基于当前代码库与平台角色（snaplink=IdP, console=控制面, audit-governance=审计事实源, aero-id=账户, aero-vault=文件, aero-im=消息/通知）分析 1 个高价值改进方向，附证据与验收标准" \
    --output "docs/requirements/runs/run-$(date +%Y%m%d-%H%M%S).md" \
    --session-mode shared --session-name "platform-247-${name}" \
    --min-interval 60 --retries 3 --reuse --retry-failed \
    --log-file "$log" --decision-log docs/DECISIONS.md \
    --git-commit --archive-dir docs/archive) >>"$SUMMARY" 2>&1
  local rc1=$?

  # 3) 失败任务补跑（最多 1 轮，防止毒任务烧配额）
  (cd "$repo" && python pi-batch.py \
    -p "补跑上一轮失败方向：如无失败任务直接输出 SKIPPED" \
    --output "docs/requirements/runs/retry-$(date +%Y%m%d-%H%M%S).md" \
    --session-mode shared --session-name "platform-247-${name}-retry" \
    --retries 2 --max-rounds 1 --min-interval 30 \
    --log-file "$log" --decision-log docs/DECISIONS.md \
    --git-commit --archive-dir docs/archive) >>"$SUMMARY" 2>&1
  local rc2=$?

  echo "[$(date +%H:%M:%S)] round=$round repo=$name done (rc=$rc1/$rc2)" >>"$SUMMARY"
}

round_no=1
while true; do
  echo "===== PLATFORM 247 ROUND $round_no $(date -u +%FT%TZ) =====" >>"$SUMMARY"
  for repo in "${PLATFORM_REPOS[@]}"; do
    run_repo_round "$repo" "$round_no"
  done
  if [ "$ROUNDS" -ne 0 ] && [ "$round_no" -ge "$ROUNDS" ]; then
    break
  fi
  round_no=$((round_no + 1))
  echo "sleep ${INTERVAL}s ..." >>"$SUMMARY"
  sleep "$INTERVAL"
done
echo "===== PLATFORM 247 FINISHED after $round_no round(s) =====" >>"$SUMMARY"
