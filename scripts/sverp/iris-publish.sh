#!/usr/bin/env bash
# iris-ui 发布循环（双项目工作流唯一合法的依赖升级路径）。
#
#   源码修复 → 门禁 → 版本递增 → 【依赖固定】→ npm publish → 依赖还原
#   → 发布校验（npm view 无 workspace:*）→ sverpweb-stable 升级 → pnpm install
#   → sverpweb 门禁 → 变更摘要
#
# ✦ 依赖固定（硬性）：
#   发布出去的 npm 包 package.json 的 dependencies 全部必须是具体版本号，
#   不允许 workspace:*（2026-08 教训：0.2.29/0.2.30 泄漏 workspace:* 导致
#   外部 pnpm 安装失败、sverpweb 长期停在旧版）。脚本在 build+publish 前
#   自动替换 workspace:* → 各 workspace 包当前版本，发布后自动还原源码为
#   workspace:*（本地开发保持 workspace 语义）。
#
# 用法:
#   iris-publish.sh [--package=vue|core|react|solid|svelte] [--dry-run]
#                   [--version=X.Y.Z] [--commit] [--skip-gates]
#     --package      发布哪个包（默认 vue）
#     --dry-run      只 pack 不发布、不升级 sverpweb 依赖
#     --version      显式指定版本（默认 = 当前版本 patch +1）
#     --commit       发布后提交版本号变更（默认不提交，由人审阅）
#     --skip-gates   跳过目标包门禁（紧急修复时用）
#
# 环境变量: IRIS_UI_DIR（默认 ~/workspace/job/iris-ui）
#           SVERPWEB_DIR（默认 ~/workspace/job/sverpweb-stable）
set -euo pipefail

IRIS_UI_DIR="${IRIS_UI_DIR:-$HOME/workspace/job/iris-ui}"
SVERPWEB_DIR="${SVERPWEB_DIR:-$HOME/workspace/job/sverpweb-stable}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PACKAGE="vue"
DRY_RUN=0
DO_COMMIT=0
SKIP_GATES=0
VERSION=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --commit) DO_COMMIT=1 ;;
    --skip-gates) SKIP_GATES=1 ;;
    --package=*) PACKAGE="${arg#--package=}" ;;
    --package) echo "usage: --package=vue|core|react|solid|svelte" >&2; exit 2 ;;
    --version=*) VERSION="${arg#--version=}" ;;
    --version) echo "usage: --version=X.Y.Z" >&2; exit 2 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

step() { echo; echo "===== $* ====="; }
PKG_DIR="$IRIS_UI_DIR/packages/$PACKAGE"
PKG="$PKG_DIR/package.json"

# ── 0. 前置检查 ──────────────────────────────────────────────
step "0. 前置检查"
test -d "$PKG_DIR" || { echo "FAIL: $PKG_DIR 不存在（--package=vue|core|react|solid|svelte）" >&2; exit 1; }
test -f "$SVERPWEB_DIR/apps/web/package.json" || { echo "FAIL: $SVERPWEB_DIR 不是 sverpweb-stable 仓库" >&2; exit 1; }
"$HERE/check-no-dist-edit.sh" "$IRIS_UI_DIR"

# ── 1. 门禁 ──────────────────────────────────────────────────
if [ "$SKIP_GATES" = "1" ]; then
  echo "[skip-gates] 跳过 $PACKAGE 包门禁"
elif [ "$PACKAGE" = "vue" ]; then
  step "1. iris-ui vue 包门禁 (typecheck + test + build)"
  "$HERE/check-iris-vue.sh" "$IRIS_UI_DIR"
else
  step "1. @iris-ui-kit/$PACKAGE 门禁 (typecheck + test + build)"
  pnpm --filter "@iris-ui-kit/$PACKAGE" typecheck
  pnpm --filter "@iris-ui-kit/$PACKAGE" test
  pnpm --filter "@iris-ui-kit/$PACKAGE" build
fi

# ── 2. 版本号 ────────────────────────────────────────────────
step "2. 版本号"
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

# ── 3. 依赖固定（workspace:* → 具体版本）─────────────────────
# 收集所有 workspace 包的当前版本作为映射表
step "3. 依赖固定：workspace:* → 具体版本号"
FIXMAP="{}"
for pkgfile in "$IRIS_UI_DIR"/packages/*/package.json; do
  name="$(node -p "require('$pkgfile').name")"
  ver="$(node -p "require('$pkgfile').version")"
  FIXMAP="$(node -e "
    const m = JSON.parse(process.argv[1]);
    m[process.argv[2]] = process.argv[3];
    console.log(JSON.stringify(m));
  " "$FIXMAP" "$name" "$ver")"
done
node -e "
  const fs = require('fs');
  const p = process.argv[1];
  const fixmap = JSON.parse(process.argv[2]);
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  let replaced = 0;
  for (const section of ['dependencies', 'devDependencies', 'peerDependencies']) {
    const deps = j[section];
    if (!deps) continue;
    for (const [name, spec] of Object.entries(deps)) {
      if (spec === 'workspace:*' || spec === 'workspace:^' || spec === 'workspace:~') {
        if (!fixmap[name]) {
          console.error('FAIL: workspace 包 ' + name + ' 不在映射表（先 build/发布该包）');
          process.exit(3);
        }
        deps[name] = fixmap[name];
        replaced += 1;
        console.log('  fixed: ' + name + ': workspace:*' + ' -> ' + fixmap[name]);
      }
    }
  }
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
  if (replaced === 0) console.log('  （无 workspace:* 依赖，无需固定）');
" "$PKG" "$FIXMAP"

# 固定后 guard：绝对不允许 workspace:* 出现在发布清单里
LEAKED_SPECS="$(node -e "
  const j = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
  const leaks = [];
  for (const section of ['dependencies','devDependencies','peerDependencies']) {
    for (const [name, spec] of Object.entries(j[section] ?? {})) {
      if (String(spec).startsWith('workspace:')) leaks.push(name + '@' + spec);
    }
  }
  console.log(leaks.join(' '));
" "$PKG")"
if [ -n "$LEAKED_SPECS" ]; then
  echo "FAIL: 发布清单仍含 workspace:*（guard 拒绝发布）: $LEAKED_SPECS" >&2
  exit 3
fi

# ── 4. 发布前重建（确保 dist 与固定依赖一致）──────────────────
step "4. 发布前重建 @iris-ui-kit/$PACKAGE"
pnpm --filter "@iris-ui-kit/$PACKAGE" build

# ── 5. 发布（dry-run 只 pack）────────────────────────────────
step "5. npm 发布 @iris-ui-kit/$PACKAGE@$VERSION"
cd "$PKG_DIR"
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] npm pack --dry-run"
  npm pack --dry-run
else
  npm publish
  echo "published: @iris-ui-kit/$PACKAGE@$VERSION"
fi

# ── 5.1 发布后校验（npm view 无 workspace:*）──────────────────
if [ "$DRY_RUN" != "1" ]; then
  step "5.1 发布校验 npm view @iris-ui-kit/$PACKAGE@$VERSION"
  sleep 15
  LEAKED="$(npm view "@iris-ui-kit/$PACKAGE@$VERSION" dependencies --json 2>/dev/null | grep -c "workspace:" || true)"
  LEAKED="${LEAKED:-0}"
  if [ "$LEAKED" != "0" ]; then
    echo "FAIL: 已发布包 dependencies 含 workspace:*（$LEAKED 处）——发布流程缺陷！" >&2
    # 不自动回滚版本（npm 不可覆盖）；提示下一版修正
    echo "注意：npm 不允许覆盖已发布版本，请修复后发下一版本。" >&2
    exit 4
  fi
  npm view "@iris-ui-kit/$PACKAGE@$VERSION" dependencies --json | head -10
fi

# ── 5.2 还原源码依赖（保持 workspace:* 本地语义）──────────────
step "5.2 还原源码 package.json（workspace:*）"
git -C "$IRIS_UI_DIR" checkout -- "packages/$PACKAGE/package.json" 2>/dev/null \
  || node -e "
    const fs = require('fs');
    const p = process.argv[1];
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    for (const section of ['dependencies','devDependencies','peerDependencies']) {
      const deps = j[section]; if (!deps) continue;
      for (const [name, spec] of Object.entries(deps)) {
        if (name.startsWith('@iris-ui-kit/')) deps[name] = 'workspace:*';
      }
    }
    fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
  " "$PKG"
echo "已还原为 workspace:*（本地开发语义）"

if [ "$DO_COMMIT" = "1" ] && [ "$DRY_RUN" != "1" ]; then
  node -e "
    const fs = require('fs');
    const p = process.argv[1];
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    j.version = process.argv[2];
    fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
  " "$PKG" "$VERSION"
  git -C "$IRIS_UI_DIR" add "packages/$PACKAGE/package.json"
  git -C "$IRIS_UI_DIR" commit -m "chore($PACKAGE): release $VERSION" || true
fi

# ── 6. sverpweb-stable 升级依赖 ──────────────────────────────
if [ "$PACKAGE" = "vue" ] || [ "$PACKAGE" = "core" ]; then
  step "6. sverpweb-stable 升级 @iris-ui-kit/$PACKAGE -> $VERSION"
  node -e "
    const fs = require('fs');
    const p = process.argv[1];
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    j.dependencies['@iris-ui-kit/$PACKAGE'] = process.argv[2];
    fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
  " "$SVERPWEB_DIR/apps/web/package.json" "$VERSION"
  grep -n '"@iris-ui-kit/'"$PACKAGE"'"' "$SVERPWEB_DIR/apps/web/package.json"
else
  echo "[skip] $PACKAGE 非 sverpweb 直接依赖，跳过升级"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "DRY-RUN COMPLETE: 以上为将执行的变更（依赖固定预览可通过 npm pack --dry-run 的输出核对）。"
  exit 0
fi

step "7. pnpm install（sverpweb-stable）"
cd "$SVERPWEB_DIR"
pnpm install

# ── 8. sverpweb 门禁 ─────────────────────────────────────────
step "8. sverpweb-stable 全门禁 (api typecheck+test, web typecheck+test+build)"
"$HERE/check-sverpweb.sh" "$SVERPWEB_DIR"

step "9. 变更摘要"
echo "iris-ui:      packages/$PACKAGE -> $VERSION（依赖已固定为具体版本）"
if [ "$PACKAGE" = "vue" ] || [ "$PACKAGE" = "core" ]; then
  echo "sverpweb:     apps/web -> @iris-ui-kit/$PACKAGE@$VERSION"
fi
echo "发布完成。建议在 sverpweb-stable 运行 dev 冒烟验证受影响页面。"