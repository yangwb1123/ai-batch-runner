#!/usr/bin/env bash
# 限流恢复后自动重启暂停的 campaign（后台守护）
# 用法: nohup bash wait-for-rate-limit.sh &
set -u
while true; do
  r=$(timeout 20 pi -p "ok" --provider anthropic 2>&1 | tail -1)
  case "$r" in
    *ok*|*OK*)
      echo "[$(date +%H:%M:%S)] 限流恢复，重启 campaign"
      (cd ~/aero-im && nohup python3 pi-batch.py campaign --config docs/campaigns/campaign-aero-im-b5.yaml \
        --jobs 4 --max-directions 2 --reuse --retry-failed --provider anthropic --wait-lock 240 \
        --log-file logs/campaign-b5-r8.log --stream-output none > logs/campaign-b5-r8.out 2>&1 &)
      (cd ~/workspace/demo/snaplink && nohup python3 pi-batch.py campaign --config docs/campaigns/campaign-snaplink-b4.yaml \
        --jobs 4 --max-directions 2 --reuse --retry-failed --provider anthropic --wait-lock 240 \
        --log-file logs/campaign-b4-r6.log --stream-output none > logs/campaign-b4-r6.out 2>&1 &)
      echo "[$(date +%H:%M:%S)] aero-im + snaplink 已重启"
      exit 0
      ;;
  esac
  sleep 180
done
