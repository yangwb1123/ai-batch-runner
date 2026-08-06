#!/usr/bin/env bash
# 门禁：iris-ui 的 @iris-ui-kit/vue 包（sverpweb-stable 唯一使用的适配器）
# 必须构建、类型检查、单测全部通过。失败 = 验证不通过（不落盘）。
#
# 用法: check-iris-vue.sh <repo-root>
set -euo pipefail

ROOT="${1:?repo root required}"
cd "$ROOT"

# 防御：确认确实是 iris-ui 仓库，防止在错误仓库里跑出假阳性
test -d packages/vue/src || { echo "FAIL: packages/vue/src not found — not an iris-ui repo?"; exit 1; }
test -f packages/vue/package.json || { echo "FAIL: packages/vue/package.json missing"; exit 1; }

echo "==> pnpm --filter @iris-ui-kit/vue typecheck"
pnpm --filter @iris-ui-kit/vue typecheck

echo "==> pnpm --filter @iris-ui-kit/vue test"
pnpm --filter @iris-ui-kit/vue test

echo "==> pnpm --filter @iris-ui-kit/vue build"
pnpm --filter @iris-ui-kit/vue build

echo "PASS: iris-ui vue package gates (typecheck + test + build)"
