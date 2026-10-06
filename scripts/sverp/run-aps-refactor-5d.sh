#!/usr/bin/env bash
# APS 页面优化：5 天（432000s）无人值守外层循环。
#
# 为什么需要外层循环：runner 自带的 round loop 在**一轮全部通过后就 return**
# （--max-rounds 0 的语义是"失败就重试到通过为止"，不是"持续优化 N 轮"）。
# 要真正跑满 5 天，必须由外层反复调用，因此这里是外层 while + 单轮硬超时。
#
# 每轮做什么：
#   1) 源码重构 aps-refactor-pipeline.yaml —— 四个路由页面 + components/ +
#      phase-schedule/ 的 god-file / 判断点 / 嵌套 / api 调用收敛；
#   2) 通过后再跑测试债务 aps-test-debt-pipeline.yaml —— 共享测试基建与
#      >400 行的测试文件拆分（保证 5 天的后续轮次有真实工作，而不是反复
#      重验已收敛的源码）。
#
# 无人值守期间的安全边界：
#   - git_commit: false，HEAD 恒为 367c987。每轮 gate 都拿**整个工作树**对 HEAD
#     做 diff，所以跨轮累积的行为漂移会在**每一轮**被重新抓到，而不是只抓增量；
#   - gate fail closed：没有可解析的 `VERDICT:` 行即阻断（所以提示词里明确禁止
#     写成 `# VERDICT:` 标题）；
#   - gate_fix_rounds: 1，修复循环不会自振荡；
#   - 每轮真跑 typecheck + 全部 vitest，行为回归不由模型自证；
#   - 不 commit、不动生产、不碰 settings/ 与任何 CSS。
#
# 中止：pkill -f 'aps-refactor-pipeline\|aps-test-debt-pipeline\|run-aps-refactor-5d'
# 恢复：/tmp/aps-route-snapshot（重构前）、/tmp/aps-route-snapshot-2（4/9 时）、/tmp/aps-settings-gate-passed（settings 页已过 gate 态）
#
# 环境变量：
#   APS_REFACTOR_BUDGET_SECONDS  总预算秒数（默认 432000 = 5 天）
#   APS_REFACTOR_DEADLINE_EPOCH   绝对终点（epoch 秒）。优先于 BUDGET_SECONDS；
#                                 用来在“中断后重启”时把**剩余**窗口接上，
#                                 而不是重新给满 5 天
#   APS_REFACTOR_ROUND_SECONDS   单轮硬超时秒数（默认 21600 = 6 小时）
#   APS_REFACTOR_ROUND_DELAY     轮间休息秒数（默认 600）
#   APS_REFACTOR_WAIT_PID        启动前等待这个 PID 结束（可选）
#   APS_REFACTOR_SKIP_TEST_DEBT  置 1 则跳过测试债务流水线
set -uo pipefail

REPO="${APS_REFACTOR_REPO:-/home/dwp/workspace/job/sverpweb-stable}"
RUNNER="${APS_REFACTOR_RUNNER:-/home/dwp/ai-batch-runner/pi-batch.py}"
# 流水线定义**跟随目标仓库**（被 git 跟踪），runner 仓库的 projects/ 已被
# commit cd512a6 有意移出索引，不能把唯一副本放在那里。
SRC_PIPELINE="${APS_REFACTOR_SRC_PIPELINE:-$REPO/pi-batch-aps-refactor-pipeline.yaml}"
DEBT_PIPELINE="${APS_REFACTOR_DEBT_PIPELINE:-$REPO/pi-batch-aps-test-debt-pipeline.yaml}"

BUDGET_SECONDS="${APS_REFACTOR_BUDGET_SECONDS:-432000}"
ROUND_SECONDS="${APS_REFACTOR_ROUND_SECONDS:-21600}"
ROUND_DELAY="${APS_REFACTOR_ROUND_DELAY:-600}"
WAIT_PID="${APS_REFACTOR_WAIT_PID:-0}"
SKIP_DEBT="${APS_REFACTOR_SKIP_TEST_DEBT:-0}"

DEADLINE_EPOCH="${APS_REFACTOR_DEADLINE_EPOCH:-0}"
log() { printf '%s [5d] %s\n' "$(date '+%F %T')" "$*"; }
if [[ "$DEADLINE_EPOCH" =~ ^[0-9]+$ ]] && [[ "$DEADLINE_EPOCH" -gt 0 ]]; then
  DEADLINE="$DEADLINE_EPOCH"
  log "使用绝对终点 deadline=$(date -d @"$DEADLINE" '+%F %T')"
else
  DEADLINE=$(( $(date +%s) + BUDGET_SECONDS ))
fi



# 每轮开始前把待判定的工作树快照写到 .pi-batch/round-baseline/。
# gate 的所有 diff 判据以此为基线——**不是 HEAD**。工作树在本项目里本来就是
# 脏的（多轮未提交的成果都在里面），拿 HEAD 当基线会把上一轮已验收的改动
# 误判为本轮越界并回滚（2026-10-01/02 连续 4 轮空转的直接原因）。
BASELINE_DIR="$REPO/.pi-batch/round-baseline"
write_baseline() {
  local round="$1"
  rm -rf "$BASELINE_DIR"
  mkdir -p "$BASELINE_DIR"
  # 只快照 gate 会做 diff 判据的范围；仓库里其余脏文件与此无关。
  for path in apps/web/src/pages/aps apps/web/src/i18n.ts; do
    cp -a "$path" "$BASELINE_DIR/$(echo "$path" | tr '/' '_')" 2>/dev/null || true
  done
  printf 'round=%s\ncreated=%s\nhead=%s\n' \
    "$round" "$(date -Is)" "$(git -C "$REPO" rev-parse HEAD)" \
    > "$BASELINE_DIR/MANIFEST"
  log "基线已写入 ${BASELINE_DIR#$REPO/}（$(find "$BASELINE_DIR" -type f | wc -l) 个文件）"
}

if [[ "$WAIT_PID" =~ ^[0-9]+$ ]] && [[ "$WAIT_PID" -gt 0 ]]; then
  log "等待在跑的单轮 PID=$WAIT_PID 结束"
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 30; done
  log "接力完成，开始计入 5 天预算"
fi

cd "$REPO" || { log "FATAL: repo missing"; exit 2; }

run_pipeline() {
  local pipeline="$1" logfile="$2" tag="$3"
  local remaining=$(( DEADLINE - $(date +%s) ))
  if (( remaining <= 0 )); then return 99; fi
  local round_limit="$ROUND_SECONDS"
  (( remaining < round_limit )) && round_limit=$remaining
  log "$tag 启动（单轮上限 ${round_limit}s，预算剩余 ${remaining}s）"
  timeout --signal=TERM --kill-after=600 "$round_limit" \
    env PBATCH_SCRIPT_DIR="$REPO" python3 "$RUNNER" "$pipeline" \
      --max-rounds 3 \
      --retries 2 \
      --reuse \
      --kill-orphans \
      --log-file "$logfile"
  local rc=$?
  case "$rc" in
    0)  log "$tag 通过" ;;
    124) log "$tag 单轮超时终止（124）" ;;
    *)  log "$tag 失败，exit=${rc}" ;;
  esac
  return 0
}

round=0
while (( $(date +%s) < DEADLINE )); do
  round=$(( round + 1 ))
  log "===== 第 ${round} 轮开始（剩余预算 $(( DEADLINE - $(date +%s) ))s）====="
  write_baseline "$round"

  run_pipeline "$SRC_PIPELINE" logs/aps-refactor.log "源码重构"
  if [[ "$SKIP_DEBT" != "1" ]]; then
    run_pipeline "$DEBT_PIPELINE" logs/aps-test-debt.log "测试债务"
  fi

  remaining=$(( DEADLINE - $(date +%s) ))
  (( remaining > 0 )) || break
  sleep_for=$ROUND_DELAY
  (( sleep_for > remaining )) && sleep_for=$remaining
  log "休息 ${sleep_for}s 后进入下一轮"
  sleep "$sleep_for"
done

log "5 天预算用尽，共完成 ${round} 轮；工作树未提交，恢复点见脚本头注释"
exit 0