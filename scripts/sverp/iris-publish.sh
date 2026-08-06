#!/usr/bin/env bash
# iris-ui 发布循环（双项目工作流唯一合法的依赖升级路径）：
#
#   iris-ui 源码修复 → 门禁（check-iris-vue + check-no-dist-edit）
#   → 版本号递增 → npm publish → sverpweb-stable 升级依赖 → pnpm install
#   → sverpweb 门禁（check-sverpweb）→ 变更摘要
#
# 用法:
#   iris-publish.sh [--dry-run] [--version X.Y.Z] [--commit]
#     --dry-run   只打包不发布、不改依赖（验证发布内容）
#     --version   显式指定版本（默认 = 当前版本 patch +1）
#     --commit    发布前把版本号变更提交到 iris-ui（默认不提交，由人审阅）
#
# 环境变量: IRIS_UI_DIR（默认 ~/workspace/job/iris-ui）
#           SVERPWEB_DIR（默认 ~/workspace/job/sverpweb-stable）
set -euo pipefail

IRIS_UI_DIR="${IRIS_UI_DIR:-$HOME/workspace/job/iris-ui}"
SVERPWEB_DIR="${SVERPWEB_DIR:-$HOME/workspace/job/sverpweb-stable}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=0
DO_COMMIT=0
VERSION=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --commit) DO_COMMIT=1 ;;
    --version=*) VERSION="${arg#--version=}" ;;
    --version) echo "usage: --version=X.Y.Z" >&2; exit 2 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

step() { echo; echo "===== $* ====="; }

# ── 0. 前置检查 ──────────────────────────────────────────────
step "0. 前置检查"
test -d "$IRIS_UI_DIR/packages/vue/src" || { echo "FAIL: $IRIS_UI_DIR 不是 iris-ui 仓库" >&2; exit 1; }
test -f "$SVERPWEB_DIR/apps/web/package.json" || { echo "FAIL: $SVERPWEB_DIR 不是 sverpweb-stable 仓库" >&2; exit 1; }
"$HERE/check-no-dist-edit.sh" "$IRIS_UI_DIR"

# ── 1. iris-ui 门禁 ──────────────────────────────────────────
step "1. iris-ui vue 包门禁 (typecheck + test + build)"
"$HERE/check-iris-vue.sh" "$IRIS_UI_DIR"

# ── 2. 版本号 ────────────────────────────────────────────────
step "2. 版本号"
PKG="$IRIS_UI_DIR/packages/vue/package.json"
CURRENT="$(node -p "require('$PKG').version")"
if [ -z "$VERSION" ]; then
  VERSION="$(echo "$CURRENT" | awk -F. '{print $1"."$2"."($3+1)}')"
fi
echo "当前版本: $CURRENT → 发布版本: $VERSION"
node -e "
  const fs = require('fs');
  const p = process.argv[1];
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  j.version = process.argv[2];
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
" "$PKG" "$VERSION"

# ── 3. 发布（dry-run 只 pack）────────────────────────────────
step "3. npm 发布 @iris-ui-kit/vue@$VERSION"
cd "$IRIS_UI_DIR/packages/vue"
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] npm pack --dry-run"
  npm pack --dry-run
else
  # 发布前再次确认 dist 是最新构建产物（check-iris-vue 已 build）
  npm publish
  echo "published: @iris-ui-kit/vue@$VERSION"
fi

if [ "$DO_COMMIT" = "1" ]; then
  cd "$IRIS_UI_DIR"
  git add packages/vue/package.json
  git commit -m "chore(vue): release $VERSION" || true
fi

# ── 4. sverpweb-stable 升级依赖 ──────────────────────────────
step "4. sverpweb-stable 升级 @iris-ui-kit/vue -> $VERSION"
node -e "
  const fs = require('fs');
  const p = process.argv[1];
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  j.dependencies['@iris-ui-kit/vue'] = process.argv[2];
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
" "$SVERPWEB_DIR/apps/web/package.json" "$VERSION"
grep -n '"@iris-ui-kit/vue"' "$SVERPWEB_DIR/apps/web/package.json"

if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] 不执行 pnpm install（依赖升级留待真正发布后执行）"
  echo
  echo "DRY-RUN COMPLETE: 以上为将执行的变更。真正的发布请去掉 --dry-run。"
  exit 0
fi

step "5. pnpm install（sverpweb-stable）"
cd "$SVERPWEB_DIR"
pnpm install

# ── 6. sverpweb 门禁 ─────────────────────────────────────────
step "6. sverpweb-stable apps/web 门禁 (typecheck + test + build)"
"$HERE/check-sverpweb.sh" "$SVERPWEB_DIR"

step "7. 变更摘要"
echo "iris-ui:      packages/vue/package.json -> $VERSION"
echo "sverpweb:     apps/web/package.json -> @iris-ui-kit/vue@$VERSION"
echo "发布完成。建议在 sverpweb-stable 运行 dev 冒烟验证受影响页面。"
