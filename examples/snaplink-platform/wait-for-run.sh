#!/usr/bin/env bash
# 等待仓库锁释放后启动命令（协作者占用时排队，不抢锁）
#
# 用法:
#   bash wait-for-run.sh <仓库目录> <命令...>
#   bash wait-for-run.sh ~/aero-vault python3 pi-batch.py campaign --config ...
#
# 锁检查: <仓库>/.pi-batch.lock 存在即等待（间隔 60s，最多 WAIT_MAX_MIN 分钟）。
# 与 pbatch/lock.py 的规则一致：活 PID 的锁永远等待（7x24 运行可远超 24h）；
# 死 PID 的 stale 锁立即破除（SIGKILL 残留）；不可解析的锁按 fail-closed 等待
# （partial write 不视为"可抢占"）。
#
# 提示: campaign 子命令内建 `--wait-lock MINUTES`（30s 轮询 + stale 破除 +
# 预算到期 exit 5），无需外部脚本；本脚本保留给非 pi-batch 命令排队用。
set -u
REPO="${1:?repo dir required}"; shift
WAIT_MAX_MIN="${WAIT_MAX_MIN:-240}"
lock="$REPO/.pi-batch.lock"
waited=0

lock_pid() {
  python3 - "$lock" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.load(f).get("pid", ""))
except Exception:
    print("")
PY
}

while [ -f "$lock" ]; do
  if [ "$waited" -ge "$((WAIT_MAX_MIN * 60))" ]; then
    echo "[WAIT-FOR-RUN] lock still held after ${WAIT_MAX_MIN}m — giving up: $REPO"
    exit 1
  fi
  pid=$(lock_pid)
  if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
    # 死 PID → stale 锁：确认锁文件仍是同一个持有者后破除（避免竞态中误删
    # 后继者刚发布的新锁；极小窗口内由工具自身的 ownership 检查兜底）。
    now=$(lock_pid)
    if [ -n "$now" ] && [ "$now" = "$pid" ]; then
      echo "[WAIT-FOR-RUN] $(date +%H:%M:%S) holder pid=$pid dead — breaking stale lock"
      rm -f "$lock"
      continue
    fi
  fi
  echo "[WAIT-FOR-RUN] $(date +%H:%M:%S) waiting for lock in $REPO (${waited}s, holder pid=${pid:-?})..."
  sleep 60
  waited=$((waited + 60))
done
echo "[WAIT-FOR-RUN] lock free — starting: $*"
cd "$REPO" || exit 2
exec "$@"
