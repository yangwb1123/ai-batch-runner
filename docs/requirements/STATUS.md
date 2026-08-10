# 扩展需求闭环状态

> 本文件是 `expansion-*.md` 与对应 `.out.md` 的当前状态索引。历史核验结果中的
> “待做”表格不代表当前实现状态。

## 已闭环路线图

| 路线 | 状态 | 当前落点 |
|---|---|---|
| C1 复用新鲜度 | 已实现 | `pbatch/reuse.py`、既有产物复验、fingerprint sidecar |
| C2 验证反馈重试 | 已实现 | `pbatch/runner.py` 将有界失败反馈注入下一次重试 |
| C3 事件、轮转与计量 | 已实现 | 结构化事件、webhook、日志轮转与 session 增量 usage |
| A 熔断、停滞与环境分诊 | 已实现 | `pbatch/triage.py` 与 CLI 分诊命令 |
| B 成本和时间预算 | 已实现 | task、stage、pipeline 与 campaign 的硬截止和预算传播 |
| D 内容寻址复用 | 已实现 | 输入、配置、agent 与仓库状态参与 fingerprint，sidecar fail closed |
| E 会话分叉 | 已实现 | new/shared/per-stage、稳定 session id 与渐进式 memory |
| F 速率与仓库并发治理 | 已实现 | provider 令牌桶、jitter、项目锁与 campaign worktree 隔离 |
| G 多 agent 适配 | 已实现 | agent 级命令、会话标志、环境变量与完成输出适配 |
| H 语义验证器 | 已实现 | JSON `pass/warn/fail` 协议、judge validator 与有界诊断 |
| I 人工批准点 | 已实现 | approval gate、超时、非交互 fail closed 与决策日志 |
| 文件级 validator AND 并行 | 已实现 | 最多 4 路并行、按声明顺序裁决；repo scope 仍在阶段末执行 |

## 被替代或刻意不采纳的提案

| 提案 | 决策 |
|---|---|
| 根据 429 动态升降 worker | 由确定性的 provider 令牌桶、burst 和 jitter 重试替代，避免运行时振荡 |
| aggregate 阶段增量重算 | 由 fingerprint 复用、有界证据注入和来源上限承担；不增加缓存一致性层 |
| 自动向每个 prompt 注入 git diff | 不默认启用；agent 可读取工作区，fingerprint 已绑定仓库状态，避免上下文重复和 argv 膨胀 |
| validator 失败即抢跑取消其余 validator | 不采纳；AND 门禁等待已启动检查，保证完整诊断且不遗留子进程 |

## 当前闭环判定

历史 expansion 路线图没有未决的必做项。新增能力应先进入明确的维护规范或
活动需求，再进入实现；历史 `.out.md` 只作为决策证据保留。
