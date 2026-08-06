#!/usr/bin/env bash
# Source-first 门禁（双项目工作流核心红线）：
#   工作树中不允许出现构建产物修改/新增：
#     - 任何 dist/ 目录下的文件（git 跟踪或未跟踪）
#     - 任何 *.tgz 打包产物
#   iris-ui 的问题必须改 packages/vue/src/** 源码；dist/ 由构建生成、
#   永不提交。sverpweb-stable 侧同样禁止任何把 iris-ui 产物塞进仓库的行为。
#
# 用法: check-no-dist-edit.sh <repo-root>
set -euo pipefail

ROOT="${1:?repo root required}"
cd "$ROOT"

# 只对 git 仓库生效；非 git 目录直接放行（无版本控制谈不上红线）
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "PASS: not a git work tree (skip)"
  exit 0
fi

# 未跟踪文件在 --porcelain 第 2 列是文件名（?? 前缀），第 1 列含状态码
violations=$(
  git status --porcelain 2>/dev/null \
    | awk '{ $1=""; sub(/^ /, ""); print }' \
    | grep -E '(^|/)dist/|\.tgz$' || true
)

if [ -n "$violations" ]; then
  echo "FAIL: 构建产物出现在工作树（禁止提交/修改 dist/ 与 *.tgz）:"
  echo "$violations"
  exit 1
fi

echo "PASS: no dist artifacts in working tree (source-first discipline held)"
