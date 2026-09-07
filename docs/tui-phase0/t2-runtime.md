# T2 — Runtime、EventBus 与 Projection

## 目标

提供统一的 Runtime 操作边界和有界事件分发/投影机制，使不同 provider 的执行结果
可以被 TUI 以相同方式消费、验证和重建。

## 已交付

- `pbatch/tui_runtime.py` 定义初始化、会话、Turn、steer、中断和审批请求类型，以及
  `RuntimeInfo`、`RuntimeSession`、`TurnHandle`、`AgentRuntime` Protocol 和
  `UnsupportedOperation`；
- `pbatch/tui_events.py` 提供线程安全的 `EventBus`、独立订阅、可选 journal 接口和
  可删除、可重建的 `Projection`；
- `pbatch/tui_adapter_legacy.py` 将现有 Backend 的 callback、结果与 workspace 快照
  映射为统一 `RuntimeEvent`；
- `tests/test_tui_runtime.py` 与 `tests/test_tui_events.py` 固定 Runtime、总线和投影契约。

## 关键契约

- Runtime 必须按真实能力实现操作；不支持的 resume、steer、approval 等动作返回
  `UnsupportedOperation`，不能以空操作冒充成功；
- `TurnHandle` 绑定 session/run/turn/actor，中断请求只作用于匹配的活动 Turn，重复
  中断和 `close()` 保持幂等；
- Legacy callback 的来源决定事件类型：正文只形成 message，stderr/log 只形成
  runtime 诊断；正文内容不能生成 command、approval、gate 或终态事件；
- EventBus 按 `event_id` 去重、按 run 校验或分配单调 `seq`，并向主队列和所有强引用
  订阅者原子投递；任一队列满时显式拒绝，不静默形成部分 fan-out；
- EventBus 投递独立的 canonical 副本；配置 journal 时先完成持久化，再让内存消费者
  可见；
- Projection 校验 workspace/session/run/turn/actor 绑定和单向 Turn 状态，使用有界
  recent、实体与统计索引；只有已终结实体可以作为安全淘汰对象；
- 终态后的 turn-scoped late event 不回退状态，`workspace.snapshot` 保留为权威快照
  更新例外；`Projection.rebuild()` 仅依赖事件流重建展示状态。

## 验证入口

- `tests/test_tui_runtime.py`：能力、Legacy 映射、中断、队列与关闭语义；
- `tests/test_tui_events.py`：去重、顺序、背压、绑定、容量、终态和 rebuild；
- `tests/test_tui_journal.py`：journal 注入后的 durable-before-visible 与重建；
- 仓库级验证入口：`make ci`。

## 结果/当前边界

Runtime 和 EventBus 已形成 provider-neutral 控制面，Projection 是可丢弃的展示缓存。
Provider IO 与协议翻译仍由各 Adapter 负责；持久 journal、损坏恢复和精确 session
重建由 T17 合同定义。
