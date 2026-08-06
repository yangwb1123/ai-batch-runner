#!/usr/bin/env bash
# 平台 6 仓库状态看板：一眼看清每个仓库的进度、门禁、campaign 状态与决策历史。
#
# 用法:
#   bash examples/snaplink-platform/platform-status.sh
#   bash examples/snaplink-platform/platform-status.sh --short   # 只显示摘要行
#
# 数据源（每仓库本地事实源，全部追加式）:
#   docs/auto/SUMMARY.md    campaign 重建摘要（不参与恢复判断，仅展示）
#   docs/auto/state.jsonl   campaign 状态事实源（尾部 3 条）
#   docs/DECISIONS.md       决策日志（尾部 3 条）
#   docs/advance/state.jsonl advance 推进状态（尾部 2 条）
#   未提交改动数            git status 脏文件数

set -u
SHORT="${1:-}"

PLATFORM_REPOS=(
  "$HOME/workspace/demo/snaplink"
  "$HOME/workspace/demo/snaplink-console"
  "$HOME/workspace/demo/snaplink-audit-governance"
  "$HOME/aero-id"
  "$HOME/aero-vault"
  "$HOME/aero-im"
)

printf '%-28s %-10s %-8s %-8s %-8s\n' "REPO" "LAST_COMMIT" "DIRTY" "CAMPAIGN" "DECISIONS"
printf '%s\n' "----------------------------------------------------------------------"

for repo in "${PLATFORM_REPOS[@]}"; do
  [ -d "$repo/.git" ] || { printf '%-28s %s\n' "$(basename "$repo")" "NO GIT REPO"; continue; }
  name="$(basename "$repo")"
  last="$(git -C "$repo" log --oneline -1 2>/dev/null | cut -c1-42)"
  dirty="$(git -C "$repo" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  campaign="n/a"
  if [ -f "$repo/docs/auto/state.jsonl" ]; then
    campaign="$(wc -l < "$repo/docs/auto/state.jsonl" | tr -d ' ') ev"
  elif [ -d "$repo/docs/auto" ]; then
    campaign="$(ls -1 "$repo/docs/auto" 2>/dev/null | wc -l | tr -d ' ') files"
  fi
  decisions="n/a"
  [ -f "$repo/docs/DECISIONS.md" ] && decisions="$(grep -c '^## ' "$repo/docs/DECISIONS.md" 2>/dev/null || echo 0)"
  [ -f "$repo/docs/decisions/DECISIONS.md" ] && decisions="$(grep -c '^## ' "$repo/docs/decisions/DECISIONS.md" 2>/dev/null || echo 0)"
  # aero-im: decisions/ 目录内滚动决策文件
  if [ -d "$repo/decisions" ] && [ -z "$(find "$repo/decisions" -maxdepth 1 -type f 2>/dev/null | head -1)" ] && [ "$decisions" = "n/a" ]; then
    count=$(ls -1 "$repo/decisions" 2>/dev/null | wc -l | tr -d ' ')
    [ "$count" -gt 0 ] && decisions="$count files"
  fi
  printf '%-28s %-10s %-8s %-8s %-8s\n' "$name" "${last:0:10}" "$dirty" "$campaign" "$decisions"

  if [ "$SHORT" != "--short" ]; then
    if [ -f "$repo/docs/auto/SUMMARY.md" ]; then
      echo "  └ campaign SUMMARY (tail):"
      tail -4 "$repo/docs/auto/SUMMARY.md" | sed 's/^/      /'
    fi
    if [ -f "$repo/docs/auto/state.jsonl" ]; then
      echo "  └ campaign state (last 3):"
      tail -3 "$repo/docs/auto/state.jsonl" 2>/dev/null | sed 's/^/      /' | cut -c1-160
    fi
    if [ -f "$repo/docs/DECISIONS.md" ]; then
      echo "  └ decisions (last 3):"
      grep '^## ' "$repo/docs/DECISIONS.md" | tail -3 | sed 's/^/      /'
    fi
  fi
done

printf '%s\n' "----------------------------------------------------------------------"
echo "提示: 每仓库跑 campaign --dry-run 预览；platform-247.sh 轮巡无人值守。"
