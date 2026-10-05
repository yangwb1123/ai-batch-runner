#!/usr/bin/env bash
# aero-im 后端（Rust）campaign runner: 引擎在 ai-batch-runner checkout 内，
# 目标仓是 cwd。仿 projects/aero-vault/run-campaign.sh。
#
#   run-campaign.sh --dry-run
#   run-campaign.sh --top-only --reuse --skip-passed --retry-failed
#
# AERO_IM_REPO 覆盖目标仓。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO="${AERO_IM_REPO:-$HOME/aero-im}"

# validator 注册表是项目数据：本项目的 pi-batch.yaml 声明它接受哪些门禁
# （repo-gate = 目标仓自己的 `make ci`）。没有它，工具自带注册表会赢。
export PBATCH_PROJECT_CONFIG="$HERE/pi-batch.yaml"

test -d "$REPO" || { echo "FAIL: $REPO does not exist (set AERO_IM_REPO)" >&2; exit 1; }

# campaign 模式要求 config 与 pipeline 模板在目标仓内，所以同步到 gitignore 的
# .pi-batch/aero-im-be-5d/。这个目录是两份配置的唯一 source of truth。
SYNC_DIR="$REPO/.pi-batch/aero-im-be-5d"
mkdir -p "$SYNC_DIR"
cp "$HERE/campaign.yaml" "$SYNC_DIR/campaign.yaml"
cp "$HERE/campaign-pipeline.yaml" "$SYNC_DIR/campaign-pipeline.yaml"
echo "config synced to $SYNC_DIR"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python3 "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
