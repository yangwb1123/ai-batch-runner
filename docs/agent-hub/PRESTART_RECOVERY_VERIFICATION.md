# 派发前失联恢复验收

本增量覆盖 Hub 已写入 Fabric 任务、但 Runner 还没有启动证据时的失联窗口。

## 行为

- 只有 Hub 任务仍为 `dispatching`、且 Fabric 返回 `status=lost` 和明确的
  `started_at: null` 时，才执行恢复；缺失字段按不可信响应处理并保持终态。
- Hub 原子清除 `_fabric_id`、工作目录和不确定派发标记，恢复 `queued`，并记录
  `compute.queued` 的 `reason=device_lost_before_start`。
- 每次恢复递增内部派发尝试号，下一次请求使用
  `<hub-task-id>:attempt-<n>`，不复用已经进入 Fabric 幂等账本的旧请求标识。
- 任务原来的 `target_device_id` 不会被清空；固定目标失联时仍等待显式
  `reschedule`，不会把约束改成自动调度。
- Fabric 提供 `started_at` 后，Hub 保持 `lost`，不自动复制可能已经产生副作用的命令。

## 验证

```text
python -m pytest -q tests/test_hub_compute.py
23 passed
python quality.py pbatch/hub_compute_store.py pbatch/hub_compute.py tests/test_hub_compute.py --strict
quality: OK
git diff --check
```

传输层测试在允许 loopback TCP 监听的环境执行，计算、transport、Broker、TUI
联合回归为 46 项通过；Hub GPU/Fabric/HTTP 集成为 46 项通过。该路径没有新增
HTTP 契约，现有接口仍由原 transport、Broker、TUI 测试覆盖。
