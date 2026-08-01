三处新断言已核对完毕，其中两处有重要修正，另外发现两个改变可行性的关键事实。

## 本轮新断言的事实核查

**1. "唯一的规避手段是手动删文件" — 错误。** `--force` 一直存在（`cli.py:51`），且**默认就是强制重生成**（`--reuse` 是 opt-in，`reuse_outputs=args.reuse and not args.force`）。所以"静默陈旧复用"只在用户显式传 `--reuse` 时发生。指纹缓存（方向 3）仍有价值——把 `--reuse` 从"存在即跳过"升级为"输入未变才跳过"——但风险定性应从"系统缺陷"降为"opt-in 模式的语义盲区"。

**2. "10 个并发任务各自退避 30s 后同时重试" — 机制错误，结论成立。** parallel 模式**根本没有重试**（`run_parallel` 无 retries 参数），限流失败直接 FAIL，全体任务在下一轮 `--round-delay` 边界**同步重启**——雷暴是"轮同步"而非"退避同步"，且 `--round-delay` 默认 60s 反而放大延迟。全局限流器要解决的是轮边界的同步突发。

**3. 日志轮转 — 属实，且是最便宜的一笔。** `cli.py:112` 用 `logging.FileHandler` 无限追加，7×24 数月确实爆盘。换 `RotatingFileHandler` 一行改动。顺带：rolling 分析 + `--archive-dir` 只归档产物，日志是唯一持续增长的实体。

## 两个改变可行性的关键事实

**事实 A：pi 会话文件自带精确 token/cost 计量，方向 1 的"从 CLI 输出解析"路线可以作废。** pi 会话是 JSONL 文件（`~/.pi/agent/sessions/`，按工作目录组织），每条消息带 `usage: {totalTokens, cost}` 字段（session-format.md 第 87-109、209 行）。runner 已知 session id → 每次调用后读会话文件尾部做 usage diff，即得**精确到调用级的 token 与美元消耗**，不依赖 CLI 输出格式、不需要 provider 计费 API。计量底座（你本轮说的 1+2 共享基础设施）由此从"脆弱"升为"可靠"，约半天工作量。

**事实 B：pi 内置 auto-compaction（默认开启）且支持 `--fork`，方向 4 的"摘要折叠/遗忘策略"大半是重造轮子。** rpc.md 显示 `autoCompactionEnabled: true` 默认开启、上下文近满时自动摘要折叠（保留 tail）；会话文件里有 `compaction` 条目可监控；`--fork` 已是 pi 一等 CLI 能力（`pi --help` 可见）。runner 侧真正缺的只是：`session_flags` 配置加一个 `fork` 键（现在只有 start/continue 两个键，`config.py:_session_flags`），超预算时用 `--fork` + 决策日志做会话轮转——**这是 3 行配置 + 少量轮转逻辑**，不是方向 4 描述的整套记忆管理。

**附带反驳**：方向 5 的"常驻 agent 进程复用"建议不纳入——与进程组击杀/硬超时模型（`_kill_group`，零孤儿设计的基石）直接冲突，且会话连续性已由服务端 session id 保证，CLI 启动不是瓶颈。

## 三轮合并后的最终路线图

| # | 工作线 | 三轮来源 | 规模 |
|---|---|---|---|
| C | **计量底座**：会话文件 usage diff + JSONL 事件流 + 日志轮转 + webhook 钩子 | R1#5、R2#1/2、R3#1/2 | 半天 |
| A | 失败分类函数 → 任务级熔断 → validator 反馈再生 | R1#2、R2#3 | 半天~一天 |
| D | 预算熔断（per-batch/每日，挂 C 上；先尝试/轮次维度，token 精确随 C 就绪） | R1#1、R2#1、R3#1 | 小 |
| E | 指纹缓存 `--reuse` 2.0 + sidecar（含 provenance 字段，格式一次定稿） | R1#3、R2#2、R3#3 | 中 |
| F | 会话轮转：`session_flags` 加 `fork` 键 + 超预算轮转 + compaction 监控 | R1#1、R3#4 | 极小 |
| G | 异构路由接通：`_task_from_def` 透传 provider/thinking/tools、meta 角色带 model、CLI `--provider`；tier 策略层后置 | R2#4、R3#5 | 小 |
| H | 结构化验证器 JSON 协议（向后兼容退出码） | R2#3 | 中 |
| I | 信任链：输出字节上限（防 OOM 先于防磁盘）+ 证据围栏 + 密钥扫描（后者今天就能用 validator 注册表挂上） | R1补充、R2#5 | 小 |

**优先级定稿**：C 与 A 并行打头（C 是三轮共识的唯一共享底座，A 是唯一现有机制完全失明的失败类）；D、F 是 C 上的两个小挂件；E、I 随时可插入；G、H 是设计决策多的慢件。这与三轮各自的排序（1+2 合并 → 熔断 → 缓存）基本收敛，差异只有两点：上下文管理因事实 B 从"紧随其后"降为"极小件 F"；成本计量因事实 A 从"脆弱依赖"升为"可靠底座 C"。

三轮提案已收敛为同一张表，且每一条都有明确的代码落点。是否按 C → A → D/F → E 的顺序开始实施？我会从 C 的最小版（会话文件 usage diff + JSONL 事件 + 日志轮转，配测试，过 `make ci`）动工。
