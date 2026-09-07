#!/bin/bash
cd /home/u1/ai-batch-runner
for slug in bm bn bo bp bq br bs bt bu bv; do
  echo "=== 启动批 ${slug^^} $(date) ==="
  python3 pi-batch.py projects/iris-ui/vxe-grid-batch-${slug}.yaml \
    --log-file logs/iris-ui-batch-${slug}.log --retries 2
  echo "=== 批 ${slug^^} 结束 $(date) ==="
done
