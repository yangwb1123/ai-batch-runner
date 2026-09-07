# T3 — TUI 事件接线与 Turn 级取消

## 目标

让 TUI 通过统一事件和当前 `TurnHandle` 驱动展示与取消，同时保持现有 Backend、
命令入口和 workspace 审阅能力可用。

## 已交付

- `pbatch/tui.py` 为 Codex Structured Exec 接入 raw-event 路径，并为文本 Backend
  接入 `LegacyAdapter`；
- `pbatch/tui_projection_ui.py` 与 `pbatch/tui_controller.py` 将事件折叠成不可变界面
  快照，Conversation、Status、Timeline 和 workspace 提示共用该投影；
- `pbatch/tui_backend.py` 在 Turn 边界记录进程快照，并支持结构化 Turn 的专属中断；
- `pbatch/runner.py` 提供 `proc_snapshot()` 与 `kill_proc_group()`，允许按进程组精确
  回收当前 Turn 创建的 agent 进程；
- 无法接管 callback 的 Backend 继续使用受限文本兼容路径，不改变无头 CLI 行为。

## 关键契约

- Structured Runtime 的控制语义只来自版本化 `RuntimeEvent`；Legacy 正文只作为
  message 展示，不能伪造 command、approval、gate 或成功终态；
- raw event 经同一个 Controller 校验后再更新界面；workspace 的权威变化采用
  Backend 在 Turn 边界生成且标记 authoritative 的 Git 快照；
- worker 线程只生产事件和结果，Textual 组件更新统一跨越 UI-thread bridge；
- structured executor 保存当前 `TurnHandle`，Backend 保存对应 interrupt callback；
  取消只向匹配的活动 Turn 发出，并仅清理 Turn 启动后新增的进程组；
- 提交前取消保留取消意图；已启动 Turn 的最终状态以 Adapter/Backend 的真实中断结果
  为准，不能把拒绝或异常伪报为 INTERRUPTED；
- Adapter 或事件流不可用时，兼容路径仍保持正文、stderr 和结果展示，但不宣称富
  Runtime 能力。

## 验证入口

- `tests/test_tui.py`：界面接线、正文信任边界、结构化 Timeline 与取消；
- `tests/test_tui_backend.py`：Turn 进程快照、专属中断和 workspace 证据；
- `tests/test_tui_wiring_regressions.py`：Runtime 重绑、取消竞态和事件接线路径；
- `tests/test_tui_controller.py`：事件到不可变界面快照的投影；
- 仓库级验证入口：`make ci`。

## 结果/当前边界

TUI 的可信状态由事件投影驱动，取消已绑定到具体 Turn 和进程组。Legacy 兼容路径只
提供文本流、结果和本地中断；typed approval、steer、review 等动作仍由 Runtime
capability 决定是否开放。
