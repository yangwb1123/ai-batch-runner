# T6：Codex provider session 映射

## 目标

让 headless runner 的逻辑 `session_id` 与 Codex 原生 thread id 明确分离，使
`shared` / `per-stage` 会话能够在后续任务中使用受信映射继续同一 provider 会话；
agent/provider/session 由映射名隔离，项目隔离依赖正确配置 data_dir。

## 已交付

- `pbatch/config.py` 支持 session-led argv：session flag 可使用 `{prompt}`、
  `{session}` 和 `{name}`，并可在生成 continue/fork argv 时显式传入
  `provider_session`。
- `pbatch/session.py` 负责 provider thread id 的校验、原子写入、有界读取、
  路径净化及兼容映射迁移。
- `pbatch/runner_session.py` 在任务启动前解析映射，在任务结束后从 Codex JSONL
  的首个非空 `thread.started` 事件捕获原生 thread id。
- 映射位于 `config.data_dir(cwd)/sessions/`，文件名绑定逻辑 session、agent 与
  provider；不会把 provider thread id 当作 pi-batch 的逻辑 session id。
- 映射本身不记录 cwd/project；多个项目共用同一外置 data_dir 时必须使用独立
  session 标识或 data_dir，避免同名映射相撞。

## 关键契约与数据流

```text
逻辑 session id
    │
    ├─ 首轮 Codex 执行 ──► thread.started.thread_id
    │                         │ 校验 + 原子写入
    │                         ▼
    └─ 后续 continue/fork ◄── scoped provider mapping
                              │
                              └─► 渲染 Codex resume/fork argv
```

- 只有声明支持 thread 捕获且采用 session-led argv 的 agent 进入此流程。
- 首轮成功但未捕获到有效映射时，任务不会被标记为可续会话。
- 对显式 `continue` / `fork`，缺失或无效映射会在启动 agent 前 fail closed；不会
  把逻辑 id 冒充 provider thread id。
- 对新的 `start`，若已有可信映射则升级为 continue；没有映射时仍按新会话启动。
- 映射读取拒绝超限、非 UTF-8、非普通文件、符号链接和不符合 Codex thread id
  形状的内容。
- 映射只保存 provider 标识，不保存 transcript，也不替代 Memory 或 RuntimeEvent
  journal。

## 验证入口

- `tests/test_agent_adapter.py`：session-led/tail argv、per-agent 配置和
  `provider_session` 替换。
- `tests/test_pi_batch.py`：首轮捕获、次轮继续、new 模式、坏首行、有界映射读取及
  shared/per-stage 会话行为。
- `tests/test_tui_adapter_codex.py`：Codex resume/fork 的 provider argv 与事件模式。
- 仓库门禁：`python quality.py --strict .`、`python pi-batch.py eval`、`make ci`。

## 结果与当前边界

Headless Codex 会话已经具备逻辑 id 到 provider thread id 的持久映射和 fail-closed
续跑路径。T6 阶段本身不包含 TUI session selector；后续 T17 已交付当前 scope 的
`/sessions`、精确 `/resume` 与能力驱动 Codex 续跑。退出后自动选择旧界面、overlay
picker 和 provider 状态预对账仍属于后续恢复切片。
