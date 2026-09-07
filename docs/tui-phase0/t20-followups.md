# T20 · 活动 turn follow-up queue 与 steer

## 用户闭环

agent turn 运行时，composer 继续接受输入。普通文本进入当前 TUI 进程的
follow-up queue；`/queue` 打开独立 overlay，支持查看、删除、清空和空闲时运行，
`/queue edit N TEXT` 可在发送前修订。当前 turn 成功后自动启动队首；失败或取消后
保留并暂停，避免在 provider 异常时连续消费后续任务。

`/steer <text>` 是独立控制意图，不等同于 queue。它只在活动 adapter 的已初始化
capability 声明 `turn_steer=true` 且存在匹配、未终结的 turn handle 时后台发送。
Codex Structured Exec 与 Legacy 当前不具备该能力；App Server Adapter 的协议实现
在主 TUI 接线并握手成功前不会开放。

## 不变量

- FIFO 最多 16 条、总计 64 KiB、单条最多 16 KiB；空文本、NUL 和超限输入被拒绝。
- 队列是 process-local 协调状态，不写 journal，不声明 provider `message_queue`，
  也不承诺崩溃恢复。
- queued prompt 只有真正启动为 turn 后才进入 transcript；overlay 操作不污染对话。
- `_start_turn` 接受新 turn 后才删除队首；启动失败保持原项。
- session/runtime 切换清空旧队列并告警，禁止跨上下文误发。
- steer 能力从活动 runtime 读取，不通过 initialize 探测；并发 steer 请求串行拒绝。
- steer 成功的 Timeline 证据只来自合法 `turn.steered` RuntimeEvent，正文同名文本无效。

## 实现与验证

- `pbatch/tui_followups.py`：有界队列、overlay、slash 控制、自动调度与 steer worker。
- `pbatch/tui.py`：busy composer、turn 终态调度和状态侧栏接线。
- `pbatch/tui_controller.py`：可信 `turn.steered` Timeline 投影。
- `tests/test_tui_followups.py`：容量/FIFO、成功续发、失败暂停、无 transcript 噪声、
  active capability 检查和 Timeline 事件。
