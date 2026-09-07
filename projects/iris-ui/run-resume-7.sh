#!/bin/bash
cd /home/u1/ai-batch-runner
# 探测：pi 返回任何非空输出即视为恢复（宽松）
for i in $(seq 1 60); do
  OUT=$(timeout 40 /usr/local/bin/pi -p "hi" --tools ls 2>&1)
  if [ -n "$OUT" ] && ! echo "$OUT" | grep -q "usage limit"; then
    echo "=== 配额已恢复 $(date) ==="; break
  fi
  sleep 60
done
for slug in dc dd de df dg dh di dj; do
  echo "=== 重跑 ${slug^^} $(date) ==="
  python3 pi-batch.py projects/iris-ui/vxe-grid-batch-${slug}.yaml --log-file logs/iris-ui-batch-${slug}-r3.log --retries 3
  echo "=== ${slug^^} 结束 $(date) ==="
  sleep 30
done
for slug in dk dl dm dn do dp dq dr ds dt; do
  echo "=== 启动批 ${slug^^} $(date) ==="
  python3 pi-batch.py projects/iris-ui/vxe-grid-batch-${slug}.yaml --log-file logs/iris-ui-batch-${slug}.log --retries 3
  echo "=== 批 ${slug^^} 结束 $(date) ==="
  sleep 30
done
