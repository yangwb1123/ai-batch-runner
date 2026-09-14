#!/usr/bin/env bash
# SVERP 工程门禁：先检查本次变更，再运行仓库级类型/测试/构建门禁。
#
# 用法：
#   check-engineering-gate.sh <repo-root>
#   PBATCH_GATE_BASE=origin/main check-engineering-gate.sh <repo-root>
#
# 设计原则：
# - 仓库历史债务通过基线/重构计划管理，不因存量复杂度阻塞无关变更；
# - 变更文件的确定性错误（any、console.log、危险 HTML、禁用测试、非法 UI
#   token、后端依赖方向/状态散落等）必须 fail closed；
# - 最终仍执行完整 API/Web typecheck、test、build；
# - 不接受 dist、tgz 或未经过源代码门禁的构建产物。
set -euo pipefail

ROOT="${1:?repo root required}"
# PBATCH_SCRIPT_DIR selects the project config and may therefore point at the
# target repository. Resolve shared scripts from this script's distribution
# root instead; PBATCH_TOOL_ROOT is an explicit override for installed assets.
RUNNER_ROOT="${PBATCH_TOOL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BASE="${PBATCH_GATE_BASE:-HEAD}"
FRONT_CHECK="$RUNNER_ROOT/scripts/check-frontend-quality.py"
BACK_CHECK="$RUNNER_ROOT/scripts/check-backend-quality.py"
UI_CHECK="$RUNNER_ROOT/scripts/check-ui-spec.py"
I18N_CHECK="$RUNNER_ROOT/scripts/check-i18n.py"
NO_DIST="$RUNNER_ROOT/scripts/sverp/check-no-dist-edit.sh"
FULL_GATE="$RUNNER_ROOT/scripts/sverp/check-sverpweb.sh"

cd "$ROOT"

test -f package.json || { echo "FAIL: package.json not found — not sverpweb-stable?" >&2; exit 1; }
test -f apps/api/package.json || { echo "FAIL: apps/api/package.json not found" >&2; exit 1; }
test -f apps/web/package.json || { echo "FAIL: apps/web/package.json not found" >&2; exit 1; }
test -f "$I18N_CHECK" || { echo "FAIL: i18n checker not found: $I18N_CHECK" >&2; exit 1; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "FAIL: engineering gate requires a git work tree" >&2; exit 1; }
git rev-parse --verify "$BASE^{commit}" >/dev/null 2>&1 \
  || { echo "FAIL: invalid gate base: $BASE" >&2; exit 1; }

bash "$NO_DIST" "$ROOT"

echo "==> git diff --check (base=$BASE)"
git diff --check "$BASE" --

if test -f apps/web/src/i18n.ts; then
  echo "==> i18n engineering gate (base=$BASE)"
  python "$I18N_CHECK" --root "$ROOT" --base "$BASE"
else
  echo "PASS: i18n gate not applicable (apps/web/src/i18n.ts is absent)"
fi

# Collect tracked staged/unstaged changes and untracked files. NUL separation
# prevents a filename containing whitespace from becoming another target.
mapfile -d '' CHANGED < <(
  {
    git diff --name-only -z --diff-filter=ACMRTUXB "$BASE" --
    git ls-files --others --exclude-standard -z
  } | sort -z -u
)

frontend_count=0
backend_count=0
ui_count=0

run_frontend_check() {
  local file="$1"
  echo "==> frontend engineering: $file"
  python "$FRONT_CHECK" "$file"
  frontend_count=$((frontend_count + 1))
}

run_backend_check() {
  local file="$1"
  echo "==> backend engineering: $file"
  python "$BACK_CHECK" "$file"
  backend_count=$((backend_count + 1))
}

run_ui_check() {
  local file="$1"
  echo "==> UI specification: $file"
  python "$UI_CHECK" --mode all "$file"
  ui_count=$((ui_count + 1))
}

for file in "${CHANGED[@]}"; do
  # Deleted files have no source to inspect; the diff and build gates still
  # validate the resulting tree.
  test -f "$file" || continue

  case "$file" in
    apps/web/src/*)
      case "$file" in
        *.ts|*.tsx|*.js|*.jsx|*.vue) run_frontend_check "$file" ;;
      esac
      case "$file" in
        *.ts|*.tsx|*.js|*.jsx|*.vue|*.css) run_ui_check "$file" ;;
      esac
      ;;
    apps/api/src/*)
      case "$file" in
        *.ts|*.tsx|*.js|*.jsx) run_backend_check "$file" ;;
      esac
      ;;
    packages/types/*)
      case "$file" in
        *.ts|*.tsx|*.js|*.jsx) run_frontend_check "$file"; run_backend_check "$file" ;;
      esac
      ;;
  esac
done

echo "PASS: changed-file engineering checks (frontend=$frontend_count backend=$backend_count ui=$ui_count i18n=checked)"

echo "==> repository engineering gate"
bash "$FULL_GATE" "$ROOT"

bash "$NO_DIST" "$ROOT"
echo "PASS: SVERP engineering gate (diff + changed-file checks + full repository gate)"
