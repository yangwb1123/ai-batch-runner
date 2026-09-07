# T12：跨 Agent 产物接力

状态：已交付 `pi-batch handoff`，与 T11 共享账本形成可审计的结果接力闭环。

## 目标

从一个已成功 run 选择已记录产物，把路径清单和有界摘录注入目标 Agent 的新任务，同时保留原任务权限、锁、验证和账本语义。

## 已交付

- `pbatch/handoff.py`：实现源选择、证据组合、目标 Agent 执行、验证器接入和退出码映射。
- `pbatch/cli.py`：注册 `handoff` 子命令。
- `tests/test_handoff.py`：覆盖源选择、唯一前缀、预算、缺失产物、执行结果和参数边界。

## 正向流程

```text
查询共享账本
  → 选择 succeeded run
  → 校验并筛选可读产物
  → 生成路径 manifest + 公平有界摘录
  → 注入目标任务 prompt
  → 串行执行目标 Agent
  → 结果继续写入共享账本
```

## 使用方式

```bash
pi-batch handoff --from-agent codex --agent pi "根据上游结果完成实现"
pi-batch handoff --from-run RUN_ID_PREFIX --agent codex --output docs/result.md "复核并总结"
pi-batch handoff --from-session SESSION_NAME --validate quick "继续处理"
```

`--from-agent`、`--from-run`、`--from-session` 必须且只能选择一个。run id 前缀必须唯一；按 agent/session 选择时只采用最近的成功记录及其产物。

## 证据纪律

- 复用 `evidence.combine_outputs` 的总字节预算和来源数量上限；缺失文件会被省略，全部不可读则在启动 Agent 前拒绝。
- prompt 同时携带来源身份、路径 manifest 和有界摘录，并要求目标 Agent 只引用实际使用的产物。
- coordinator 只读取源产物并生成摘录；目标 Agent 仍遵守其 workspace lock、timeout、
  validator 和 prompt 大小门禁。源路径可来自账本中的绝对路径，工具不把它限制在 cwd，
  也不保证目标 Agent 不会修改其原本可写的源文件。
- 退出码：`0` 成功，`1` 目标任务或验证失败，`2` 参数、源选择、证据或内部准备失败。

## 验证入口

```bash
python -m pytest tests/test_handoff.py tests/test_agent_ledger.py tests/test_pi_batch.py tests/test_entrypoints.py -q
python quality.py --strict .
python pi-batch.py eval
```

## 结果与边界

Agent 之间共享的是显式产物证据，不是隐式聊天上下文；handoff 串行执行且不自动
commit。使用外部绝对路径前，调用方必须确认数据边界和目标 Agent 的写权限。
