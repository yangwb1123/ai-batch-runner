#!/usr/bin/env bash
# 等待仓库锁释放后启动命令（协作者占用时排队，不抢锁）
#
# 用法:
#   bash wait-for-run.sh <仓库目录> <命令...>
#   bash wait-for-run.sh ~/aero-vault python3 pi-batch.py campaign --config ...
#
# 锁检查: <仓库>/.pi-batch.lock 存在即等待（间隔 60s，最多 WAIT_MAX_MIN 分钟）
set -u
REPO="${1:?repo dir required}"; shift
WAIT_MAX_MIN="${WAIT_MAX_MIN:-240}"
waited=0
while [ -f "$REPO/.pi-batch.lock" ]; do
  if [ "$waited" -ge "$((WAIT_MAX_MIN * 60))" ]; then
    echo "[WAIT-FOR-RUN] lock still held after ${WAIT_MAX_MIN}m — giving up: $REPO"
    exit 1
  fi
  echo "[WAIT-FOR-RUN] $(date +%H:%M:%S) waiting for lock in $REPO (${waited}s)..."
  sleep 60
  waited=$((waited + 60))
done
echo "[WAIT-FOR-RUN] lock free — starting: $*"
cd "$REPO" || exit 2
exec "$@"
