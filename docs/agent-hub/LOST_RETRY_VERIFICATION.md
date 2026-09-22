# 丢失任务显式重试验收

本增量覆盖 Fabric 已报告启动证据后 `lost` 的任务恢复。因为远端命令可能已经
产生副作用，重试不会由轮询器自动触发，调用者必须提交
`confirm_duplicate=true`。

## 行为

- 只接受 `lost` 终态、任务创建者和 `agent.tasks:write`；活动回合还必须仍在运行
  且任务属于当前 turn。
- 目标设备继续受任务创建时的目标集合、当前 compute grant 和项目绑定约束。
  请求省略目标时保留原显式目标，原本自动放置的任务才会重新自动选机。
- `archive_state` 不是 `disabled` 时拒绝重试，避免归档记录与新的执行结果竞争。
- Hub 原子清空旧 Fabric 句柄、设备分配、结果和 GPU 分配，重置工作区输出状态，
  记录 `retry_requested` 队列事件，并使用 `<task-id>:retry-<n>` 新请求标识。
- CLI、TUI、活动回合 capability 和 Flutter Console 都显示这是可能重复执行的
  人工动作；没有客户端隐式重试 `lost`。

## 验证

```text
python -m pytest -q tests/test_hub_compute.py tests/test_hub_compute_transport.py \
  tests/test_hub_compute_tui.py tests/test_hub_placement_broker.py
52 passed
python quality.py pbatch/hub_compute_models.py pbatch/hub_compute_store.py \
  pbatch/hub_compute_api.py pbatch/hub_compute_client.py pbatch/hub_compute_cli.py \
  pbatch/hub_compute_broker.py pbatch/hub_compute_tui.py --strict
quality: OK
```

Console 定向验证：`flutter test test/agent_hub_api_test.dart
test/agent_compute_reschedule_widget_test.dart`，9 项通过；`flutter analyze` 的
本轮 Agent 代码无错误，工作区另有 Forge 页面既有 2 个可空类型错误。

这些结果来自本地 SQLite、loopback HTTP 和测试替身；没有把真实跨主机 Fabric、
真实副作用命令或生产归档服务的验收混入结论。
