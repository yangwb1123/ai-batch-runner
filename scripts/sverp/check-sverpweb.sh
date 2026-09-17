#!/usr/bin/env bash
# 门禁：sverpweb-stable 必须通过 API/Web 类型检查、单测、构建以及全局配置矩阵。
#
# 用法: check-sverpweb.sh <repo-root>
set -euo pipefail

ROOT="${1:?repo root required}"
cd "$ROOT"

# 防御：确认确实是 sverpweb-stable 仓库
test -f apps/web/package.json || { echo "FAIL: apps/web/package.json not found — not sverpweb-stable?"; exit 1; }
test -f scripts/sverp/check-settings-matrix.py || {
  echo "FAIL: settings matrix checker not found: scripts/sverp/check-settings-matrix.py" >&2
  exit 1
}

run_settings_matrix() {
  local port="${PBATCH_SETTINGS_MATRIX_PORT:-}"
  local log_file
  local server_pid
  local ready=0
  local status=1

  if test -z "$port"; then
    port="$(python - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
  fi

  log_file="$(mktemp "${TMPDIR:-/tmp}/sverp-settings-matrix.XXXXXX.log")"
  echo "==> global settings matrix (port=$port)"
  pnpm --filter @sverp/web dev --host 127.0.0.1 --port "$port" >"$log_file" 2>&1 &
  server_pid=$!

  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/" >/dev/null 2>&1; then
      ready=1
      break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  if test "$ready" -eq 1; then
    if python scripts/sverp/check-settings-matrix.py \
      --base-url "http://127.0.0.1:$port"; then
      status=0
    else
      status=$?
    fi
  else
    echo "FAIL: settings matrix server did not become ready" >&2
    cat "$log_file" >&2
  fi

  kill "$server_pid" 2>/dev/null || true
  pkill -P "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  if test "$status" -eq 0; then
    rm -f "$log_file"
  else
    echo "settings matrix server log: $log_file" >&2
  fi
  return "$status"
}

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

# Every engineering update must verify that the global size/theme/font/radius
# settings still reach the rendered Iris shell. The checker starts a temporary
# Vite server so this gate is self-contained rather than dependent on a stale
# developer process.
run_settings_matrix

echo "PASS: sverpweb-stable gates (api typecheck+test, web typecheck+test+build, global settings matrix)"
