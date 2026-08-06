#!/usr/bin/env bash
# 同步 ai-batch-runner 工具集到 6 个产品仓库（幂等，可重复执行）
#
# 用法: bash examples/snaplink-platform/sync-tool.sh [--commit]
#   --commit: 每仓库用 platform-bot 身份提交（默认只放置文件，不提交）
#
# 每仓库获得: pi-batch.py（薄壳）+ pbatch/（包）+ quality.py（门禁）
#   + pi-batch.yaml（按语言适配的验证器注册表）+ .gitignore 追加
# 注意: 不触碰仓库业务代码与未提交工作；提交由 --commit 显式控制。

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMMIT="${1:-}"
PY="${PY:-python3}"

REPOS=(
  "$HOME/workspace/demo/snaplink:go"
  "$HOME/workspace/demo/snaplink-console:flutter"
  "$HOME/workspace/demo/snaplink-audit-governance:go"
  "$HOME/aero-im:rust"
  "$HOME/aero-id:go"
  "$HOME/aero-vault:go"
)

gitignore_extra() {
  cat >> .gitignore <<'EOF'

# ai-batch-runner runtime artifacts
.pi-batch/
logs/
docs/archive/
docs/auto/
docs/requirements/runs/
EOF
}

write_yaml() {
  local lang="$1"
  cat > pi-batch.yaml <<EOF
# Declarative defaults for pi-batch.py (synced from ai-batch-runner).
agent:
  bin: pi
  default_timeout: 900
  default_workers: 4
  session_flags:
    start: ["--session-id", "{session}", "--name", "{name}"]
    continue: ["--session-id", "{session}"]

defaults:
  timeout: 900
  retries: 2

evidence:
  max_bytes: 65536
  max_sources: 64

validators:
  pyquality: "$PY {cwd}/quality.py {cwd} --exclude ai-dev"
  completion: "$PY /home/u1/ai-batch-runner/scripts/check-completion-report.py {output}"
  secretscan: 'grep -nE "(api[_-]?key|secret|password|token|private[_-]?key)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9_./+-]{8,}" {output} && exit 1 || exit 0'
EOF
  case "$lang" in
    go)
      cat >> pi-batch.yaml <<'EOF'
  gobuild:
    cmd: "go build ./... && go vet ./..."
    scope: repo
  gotest:
    cmd: "go test ./... -count=1"
    scope: repo
  gofmtcheck:
    cmd: 'test -z "$(gofmt -l .)"'
    scope: repo
EOF
      ;;
    rust)
      cat >> pi-batch.yaml <<'EOF'
  cargocheck:
    cmd: "cargo check --quiet"
    scope: repo
  cargofmt:
    cmd: "cargo fmt --check"
    scope: repo
EOF
      ;;
    flutter)
      cat >> pi-batch.yaml <<'EOF'
  dartfmt:
    cmd: "dart format --output=none --set-exit-if-changed lib"
    scope: repo
EOF
      ;;
  esac
}

for entry in "${REPOS[@]}"; do
  repo="${entry%%:*}"
  lang="${entry##*:}"
  [ -d "$repo/.git" ] || { echo "[SKIP] $repo (no git repo)"; continue; }
  cp "$ROOT/pi-batch.py" "$repo/pi-batch.py"
  rm -rf "$repo/pbatch"
  cp -r "$ROOT/pbatch" "$repo/pbatch"
  cp "$ROOT/quality.py" "$repo/quality.py"
  (cd "$repo" && write_yaml "$lang")
  (cd "$repo" && gitignore_extra)
  echo "[SYNCED] $repo ($lang)"
  if [ "$COMMIT" = "--commit" ]; then
    (cd "$repo" && git add pi-batch.py pbatch/ quality.py pi-batch.yaml .gitignore \
      && git -c user.name="platform-bot" -c user.email="platform@snaplink.local" \
         commit -q -m "chore(tooling): sync ai-batch-runner (thin shell + pbatch + quality gate + per-lang validators)")
    echo "[COMMITTED] $repo"
  fi
done
echo "done. run 'python3 pi-batch.py check' in each repo to verify."
