#!/usr/bin/env bash
# 门禁：sverpweb-stable 的 apps/web 必须类型检查、单测、构建全部通过。
#
# 用法: check-sverpweb.sh <repo-root>
set -euo pipefail

ROOT="${1:?repo root required}"
cd "$ROOT"

# 防御：确认确实是 sverpweb-stable 仓库
test -f apps/web/package.json || { echo "FAIL: apps/web/package.json not found — not sverpweb-stable?"; exit 1; }

echo "==> pnpm --filter @sverp/api typecheck"
pnpm --filter @sverp/api typecheck

echo "==> pnpm --filter @sverp/api test"
pnpm --filter @sverp/api test

echo "==> pnpm --filter @sverp/web typecheck"
pnpm --filter @sverp/web typecheck

echo "==> pnpm --filter @sverp/web test"
pnpm --filter @sverp/web test

echo "==> pnpm --filter @sverp/web build"
pnpm --filter @sverp/web build

echo "PASS: sverpweb-stable gates (api typecheck+test, web typecheck+test+build)"
