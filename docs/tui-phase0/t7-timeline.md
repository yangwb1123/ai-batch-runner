# T7：TUI 结构化执行与 Timeline

## 目标

把 Codex Structured Exec 的可信生命周期事件接入产品 TUI，使正文、命令、文件、
计划、用量和终态来自版本化事件，而不是从 assistant 文本猜测；同时保持 Legacy
agent 和 headless 命令行为不变。

## 已交付

- Codex TUI turn 通过 `CodexExecAdapter` 和 `TUIBackend.run_turn_structured()` 执行；
  其他 agent 继续使用 Legacy 路径。
- command、command output、file、plan、usage、message 和 runtime status 进入有界
  Timeline；同一 command 或 plan item 按稳定 identity 原位更新。
- `Ctrl+T` 与 `/timeline` 控制 Timeline 展示，窄终端继续使用既有降级布局。
- Structured turn 捕获 provider thread id，当前进程内的下一轮可 resume 同一 Codex
  会话。
- cancel 绑定当前 adapter handle，只中断本轮 provider 进程，不调用全局进程清理。
- 事件最终经 EventBus、Projection 和不可变 `TUIViewSnapshot` 进入 UI 线程；兼容
  callback 不得把正文提升为 command、plan、approval 或终态事件。

## 关键契约与数据流

```text
Codex exec --json
    │
    ▼
CodexExecAdapter ── RuntimeEvent ──► EventBus ──► Projection
    │                                               │
    └─ handle-scoped interrupt                      ▼
                                            immutable snapshot
                                                    │
                                                    ▼
                                             Conversation/Timeline
```

- 事件 kind 决定展示类型；正文中的控制词没有控制权。
- command 输出、Timeline detail、plan item、路径和总条目数都有硬上限；ANSI 控制序列
  在渲染前移除。
- 运行中或失败 command 默认展开；成功 command、file、usage 和普通 message/status
  可折叠。
- 相同 `item_id` 的 command/plan 生命周期合并；缺失 identity 时使用有界兼容键，
  不与其他已知 item 混合。
- provider `file.changed` 只表示观察结果；工作区是否真的变化仍以 turn 边界的 Git
  snapshot/delta 为权威。
- turn 的完成、失败、取消和超时只由 typed terminal event 与 backend 结果决定。

## 验证入口

- `tests/test_tui_adapter_codex.py`：Codex JSONL 映射、session、终态和中断。
- `tests/test_tui_backend.py`：structured turn、provider resume、事件转发和结果折叠。
- `tests/test_tui_timeline.py`：条目上限、折叠、ANSI 清理、command/plan identity 与用量。
- `tests/test_tui.py`：Textual 无头交互、快捷键、slash 命令和窄屏行为。
- `tests/test_tui_controller.py`、`tests/test_tui_events.py`：统一事件投影与不可变快照。
- 仓库门禁：`python quality.py --strict .`、`python pi-batch.py eval`、`make ci`。

## 结果与当前边界

单 Agent 的 Structured Exec 与当前 turn Timeline 已接入 TUI。独立 Plan/Changes
Inspector、steer/queue、session picker、重启后的 provider resume 和完整 ArtifactRef
展开仍未交付；Timeline 不是完整 transcript 或 provider 执行真值。
