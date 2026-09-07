#!/bin/bash
cd /home/u1/ai-batch-runner
for slug in cq cr cs ct cu cv cw cx cy cz; do
  echo "=== 启动批 ${slug^^} $(date) ==="
  python3 pi-batch.py projects/iris-ui/vxe-grid-batch-${slug}.yaml \
    --log-file logs/iris-ui-batch-${slug}.log --retries 3
  echo "=== 批 ${slug^^} 结束 $(date) ==="
  sleep 20
done
