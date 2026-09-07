#!/bin/bash
# 10 轮推进：BC → BL（每个 pipeline 4 阶段串行）
cd /home/u1/ai-batch-runner
for slug in bc bd be bf bg bh bi bj bk bl; do
  echo "=== 启动批 ${slug^^} $(date) ==="
  python3 pi-batch.py projects/iris-ui/vxe-grid-batch-${slug}.yaml \
    --log-file logs/iris-ui-batch-${slug}.log --retries 1
  echo "=== 批 ${slug^^} 结束 $(date) ==="
done
