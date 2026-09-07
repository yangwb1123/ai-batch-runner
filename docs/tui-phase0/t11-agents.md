# T11：多 Agent 共享运行账本

状态：已交付异构 Agent 配置、共享运行索引和只读查询界面。

## 目标

让不同 Agent 的任务、会话状态和产物路径通过同一受限账本互相可见，为后续结果接力提供事实源。

## 已交付

- `pi-batch.yaml`：提供 pi、Codex、Claude、Gemini 与 OpenCode 的声明式调用配置。
- `pbatch/agent_ledger.py`：实现共享 JSONL 账本、轮转、查询、健康检查及 `pi-batch agents`。
- `pbatch/execution_trace.py`：在任务公共收尾路径记录成功、失败、拒绝或取消结果。
- `pbatch/tui_agent_runs.py` 与 `pbatch/tui.py`：实现不污染 transcript 的只读
  `/agents [agent]` overlay，`/runs` 为兼容别名；旧 `tui_agents.py` 投影仍服务
  headless/运营摘要兼容路径。
- `tests/test_agent_ledger.py`、`tests/test_tui_agents.py`：覆盖账本边界、CLI 和 TUI 投影。

## Agent 适配边界

| Agent | Prompt/会话能力 | 配置定位 |
|---|---|---|
| pi | 仓库默认适配 | 通用批处理与会话模式 |
| codex | `exec`、provider thread resume/fork | 结构化执行与会话延续 |
| claude | print 模式与显式 resume/fork 参数 | 配置驱动调用 |
| gemini | 非交互 approval/trust 参数 | 每轮新会话 |
| opencode | `run`、session/fork 参数 | 配置驱动调用 |

能力以实际二进制与配置探测结果为准；配置存在不等于 provider 可用，也不把逻辑 session id 冒充 provider 原生 id。

## 账本合同

- 路径为当前配置的 `<data_dir>/agents/ledger.jsonl`；记录本身不携带独立 tenant、cwd
  或 sensitivity 字段，因此数据隔离依赖调用方选择正确的 data_dir。
- 单条记录包含 agent、run/session、状态、attempt、任务摘要、产物路径、耗时与退出码；摘要和路径数量均有上限。
- 账本总量有界并在锁内保留最近记录；写入失败只产生 warning，不反转任务结果。
- `agents list` 展示执行器/配置健康度，`agents sessions` 展示有界运行摘要；TUI
  `/agents` 只读同一事实源，并额外展示当前进程明确知道的 coordinator operation。
- Inspector 最多展示最近 100 条，支持 agent/status 过滤与刷新；损坏、超长或缺少
  agent 的记录跳过并显示诊断。当前 operation 不是终态账本行，不会被持久化两次。
- 账本只共享索引和产物路径，不复制产物正文；读取和接力边界由调用方及 T12 负责。

## 验证入口

```bash
python -m pytest tests/test_agent_ledger.py tests/test_tui_agents.py tests/test_tui.py tests/test_pi_batch.py tests/test_entrypoints.py -q
python quality.py --strict .
python pi-batch.py eval
```

## 结果与边界

异构 Agent 已能互见运行与产物位置；真正的证据注入与目标 Agent 执行由 T12 handoff 完成。
