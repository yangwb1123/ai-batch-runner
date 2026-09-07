# T7.5：Structured Backend 职责拆分

## 目标

把 Structured Exec 的 adapter 生命周期、事件消费和中断协调从通用 `TUIBackend`
中分离，保持公开行为不变，并让核心 backend 与 provider-specific 执行逻辑各自维持
清晰边界。

## 已交付

- `pbatch/tui_backend.py` 保留会话设置、workspace、Memory、结果状态和 Legacy 执行等
  通用治理职责。
- `pbatch/tui_backend_structured.py` 承担 Structured adapter 创建、session adopt/resume、
  turn 启动、typed event 消费、handle-scoped interrupt、超时和结果折叠。
- `TUIBackend.run_turn_structured()` 保留为稳定委托入口，调用方无需依赖拆分模块的
  私有实现。
- structured 模块通过注入的回调使用 backend 的加锁、任务构造、运行时配置、结果
  记录和收官逻辑，避免复制治理规则或形成循环依赖。

## 关键契约与数据流

```text
TUIBackend
  ├─ 通用会话 / workspace / Memory / 结果状态
  └─ run_turn_structured()
          │ 委托治理回调
          ▼
tui_backend_structured
  └─ adapter / handle / event consumer / interrupt / terminal result
```

- 同一 turn 锁、workspace 锁和结果收官语义在拆分前后保持一致。
- backend 只保存通用的当前 turn interrupt callback；adapter、handle 和“一次中断”
  状态由 structured 模块持有。
- provider 事件仍走既有 event sink；拆分不创建第二套 Timeline、Projection 或会话状态。
- adapter 关闭和消费线程收尾属于 structured turn 生命周期，不泄漏到下一轮。

## 验证入口

- `tests/test_tui_backend.py`：structured 委托、结果、取消、超时和 provider 会话延续。
- `tests/test_tui.py`：调用方行为及 Legacy TUI 回归。
- `tests/test_tui_timeline.py`、`tests/test_tui_adapter_codex.py`：事件展示与 adapter 契约。
- 组织门禁：`python quality.py --strict .`、`make check-filesize`、`make ci`。

## 结果与当前边界

该切片完成的是内部职责拆分，没有新增用户命令或运行时能力。后续 structured
provider 集成应扩展 adapter/runtime 层，不把 provider-specific 状态重新塞回核心
`TUIBackend`。
