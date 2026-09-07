# T5 — Codex 会话延续

## 目标

在 pi-batch 逻辑会话与 Codex provider thread 之间建立稳定映射，支持 headless 与
TUI 场景中的 continue、resume 和 fork，同时保持 argv 构造无 shell、可配置且有界。

## 已交付

- `pbatch/config.py` 支持按 agent 配置 session flags，并通过 `{session}`、`{name}` 与
  `{prompt}` 构造 tail-style 或 session-led argv；
- `pbatch/session.py` 捕获合法 `thread.started` provider id，并在当前 `data_dir` 下按
  agent/provider/name/session 保存会话映射；
- `pbatch/runner_session.py` 为 continue/fork 解析 provider id，并把会话建立结果折回
  TaskResult；
- `CodexExecAdapter` 支持 `resume_session()` 与 `fork_session()`，并在事件中标明本次
  session mode；
- `pbatch/tui_adapter_codex.py` 保留 session/Turn 状态、事件序列、JSONL 分派与有界
  队列；`pbatch/tui_codex_support.py` 集中 RuntimeInfo、argv 定位、进程组生命周期、
  字段/identity 限界及通用 ID/时间/workspace 辅助；
- fork 捕获子 thread id 后，后续 Turn 自动延续子会话。

## 关键契约

- 逻辑 session id 用于协调器和 TUI 绑定，provider thread id 用于 Codex 原生延续；
  两者不能互相冒充；
- agent-specific session flags 优先于全局配置；含 `{prompt}` 的模板在声明位置注入
  prompt，不含该占位符的模板保持 prompt-tail 布局；
- invocation extra args 去重追加，避免同一选项在 session template 和公共参数中重复；
- provider mapping 的路径组件经清理和摘要隔离，写入使用原子替换；读取有字节上限、
  拒绝 symlink/非普通文件、非 UTF-8 和非法 thread id；
- mapping metadata 不单独绑定 cwd/project；多个项目显式共用同一外置 data_dir 时，
  调用方必须使用不同 session name/id 或独立 data_dir；
- resume/fork 必须有 provider id，且活动 Turn 未结束时不能替换 session；新 Adapter
  恢复时采用协调器提供的逻辑 session id；
- resume 使用当前 provider thread；fork 首次使用父 thread，收到子 id 后将后续 Turn
  切换为 resume 子 thread；
- 状态机只存在于 Adapter 主模块；支持模块只提供无状态私有函数，主模块单向依赖
  支持模块，不形成第二套 Runtime 状态或公共 ABI；
- interrupt/close 共用支持模块的进程组回收路径；字段限界、identity 摘要、usage、
  单调 `seq`、唯一 event id 和终态规则不因模块边界改变；
- capability 中 `session_resume`、`session_fork` 与实际 argv/事件 mode 保持一致。

## 验证入口

- `tests/test_agent_adapter.py`：session-led/tail argv、配置优先级和 provider id 覆盖；
- `tests/test_pi_batch.py`：headless thread 捕获、scoped mapping 和连续调用；
- `tests/test_tui_adapter_codex.py`：resume、fork、逻辑 session 绑定和子 thread 延续；
- `tests/test_tui_integration_codex.py`：安装 Codex 时的真实双 Turn 延续；
- 仓库级验证入口：`make ci`。

## 结果/当前边界

Codex 会话延续已同时覆盖配置驱动的 headless 调用和结构化 TUI Adapter。该映射只保存
provider thread identity，不是聊天 transcript、session picker 或 provider 状态对账；
跨进程界面恢复与执行恢复仍由独立的持久化和产品能力负责。Adapter 当前采用“有状态
主模块 + 无状态支持模块”的内部结构，不改变 Runtime capability 或用户可见语义。
