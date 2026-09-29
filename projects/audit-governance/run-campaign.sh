#!/usr/bin/env bash
# audit-governance campaign 一键入口：在目标仓内运行本工具集的 pi-batch。
#
# 用法:
#   run-campaign.sh [campaign 额外参数...]      # 默认 dry-run 安全预演由调用方决定
#   run-campaign.sh --dry-run                   # 只打印计划，不产生文件/状态
#   run-campaign.sh --jobs 4 --max-directions 2
#
# 目标仓可用 AUDIT_GOVERNANCE_REPO 覆盖（默认 $HOME/snaplink-audit-governance）。
# 本脚本不修改目标仓的受版本控制文件：只写入 gitignored 的 .pi-batch/。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

REPO="${AUDIT_GOVERNANCE_REPO:-$HOME/snaplink-audit-governance}"

test -d "$REPO" || { echo "FAIL: $REPO 不存在（用 AUDIT_GOVERNANCE_REPO 指定）" >&2; exit 1; }

# campaign 要求配置与 pipeline 模板位于仓内（仓外路径会以
# "campaign path escapes repository" 失败），故同步到 gitignored 的
# .pi-batch/audit-governance/。本目录的文件才是 source of truth。
SYNC_DIR="$REPO/.pi-batch/audit-governance"
mkdir -p "$SYNC_DIR"
cp "$HERE/campaign.yaml" "$SYNC_DIR/campaign.yaml"
cp "$HERE/campaign-pipeline.yaml" "$SYNC_DIR/campaign-pipeline.yaml"
echo "配置已同步到 $SYNC_DIR"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
