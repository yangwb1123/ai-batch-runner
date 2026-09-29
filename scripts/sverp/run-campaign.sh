#!/usr/bin/env bash
# 双项目 campaign 一键入口：在目标仓库内运行 pi-batch campaign。
#
# 用法:
#   run-campaign.sh sverpweb [campaign 额外参数...]   # sverpweb-stable 仓库
#   run-campaign.sh iris    [campaign 额外参数...]   # iris-ui 仓库（vue 包）
#
# 示例:
#   run-campaign.sh sverpweb --dry-run                     # 先看计划
#   run-campaign.sh sverpweb --max-directions 2 --reuse    # 真实执行
#   run-campaign.sh iris --modules packages/vue/src/admin   # 限定模块
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

TARGET="${1:-}"
shift || true

case "$TARGET" in
  sverpweb)
    REPO="$HOME/workspace/job/sverpweb-stable"
    CONFIG="$ROOT/projects/sverp/campaign-sverpweb.yaml"
    ;;
  iris)
    REPO="$HOME/workspace/job/iris-ui"
    CONFIG="$ROOT/projects/sverp/campaign-iris-vue.yaml"
    ;;
  *)
    echo "usage: run-campaign.sh sverpweb|iris [campaign args...]" >&2
    exit 2
    ;;
esac

# The validator registry is project data: it declares the gate this target
# accepts work on (repo-gate = the target repository's own command). Without it
# the tool's own registry would win.
export PBATCH_PROJECT_CONFIG="$ROOT/projects/sverp/pi-batch.yaml"

test -d "$REPO" || { echo "FAIL: $REPO 不存在" >&2; exit 1; }

# 同步 campaign 配置到目标仓库（campaign 要求配置与 pipeline 模板在仓库内）
SYNC_DIR="$REPO/.pi-batch/sverp"
mkdir -p "$SYNC_DIR"
cp "$ROOT/projects/sverp/campaign-$([ "$TARGET" = iris ] && echo iris-vue || echo sverpweb).yaml" "$SYNC_DIR/campaign.yaml"
cp "$ROOT/projects/sverp/$([ "$TARGET" = iris ] && echo iris-campaign-pipeline || echo sverpweb-campaign-pipeline).yaml" "$SYNC_DIR/campaign-pipeline.yaml"
echo "配置已同步到 $SYNC_DIR"

cd "$REPO"
echo "==> cd $REPO"
echo "==> python $ROOT/pi-batch.py campaign --config $SYNC_DIR/campaign.yaml $*"
exec python "$ROOT/pi-batch.py" campaign --config "$SYNC_DIR/campaign.yaml" "$@"
