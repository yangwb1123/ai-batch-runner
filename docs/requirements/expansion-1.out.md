第一轮提案已对照全部代码（`pbatch/*.py` 全量 + `pi-batch.yaml`）逐条核验。总体结论：**五个方向全部建立在真实缺口上，无虚构**；但有 4 处机制描述需要修正（parallel 无重试、validator 输出并非完全丢弃、reuse 无提交噪音、会话 id 不丢），方向 1 的计量路线可大幅简化（pi 会话文件自带精确 usage），方向 3 的真实危害比原提案更强（reuse 会**绕过验证门禁**）。以下按"逐方向核验 → 修正断言 → 合并路线图"给出结论。

## 逐方向核验

**方向 1（成本/上下文预算）— 成立，计量路线需修正**
- `--max-rounds 0` 无限轮、停止条件仅"全部通过"：属实（`cli.py:_run_rounds`，`max_rounds==0` 循环至 `not round_failed`）。
- "永远过不了 gate 的任务 + `--max-rounds 0` = 无限烧钱"：**属实且是字面成立的最坏情况**——gate FAIL 会 `break` 当前 pipeline（`pipeline.py:run_pipeline` 的 `_handle_gate` 分支），但 `_run_pipeline_round` 返回 failed → 下一轮同一 gate 阶段重跑，永续循环。meta 编排有 `max_iterations` 上限（默认 3），所以工具内部真正的无上限烧钱场景就是"确定性失败任务 + 无限轮"这一种。
- 上下文无限增长：属实。shared 模式跨阶段、跨轮延续同一 session（`stage_session_id` 即 `session_name`，`_derive_session_name` 确定性派生），且 `run_serial` 的**重试也留在同一会话**（continue flags）——一个反复失败的任务每次重试都在烧上下文，与方向 2 的熔断缺失互相放大。
- 盲区定性准确，一处补充：**硬溢出其实在失败签名里**（`context_length_exceeded`，`runner.py:_AGENT_ERROR_PATTERNS`）会被拒绝；失明的只是**软退化**（截断/遗忘早期约束、exit 0）。这改变护栏重点：要做的是"水位监控 + 轮转"，而非"溢出兜底"。
- **方法修正**：token 记账不应"解析 CLI 输出"（脆弱、依赖格式）。pi 会话是 JSONL 文件（`~/.pi/agent/sessions/`），每条消息带 `usage.totalTokens/cost`——runner 已知 session id，调用后读会话文件尾部 diff 即得**精确到调用级的 token 与美元消耗**。这让方向 1 的预算熔断与方向 5 的事件流共享一个可靠底座（路线图 C3）。

**方向 2（失败分类/熔断/反馈再生）— 方向成立，两处机制修正**
- "统一走退避重试"不准确：现有 `_retry_wait` 已有粗粒度二分——`_RETRYABLE_REASON`（rate/429/quota/network 等）≥30s 保底，其余走 10s 基数退避。真正缺的不是"分类"而是**策略**：auth/permanent 类照样被重试（无 stop+notify）、无任务级熔断、无批次级停滞检测。
- **重要机制修正**：重试只存在于单批 serial（`run_serial` 的 retries 参数）；**pipeline 阶段内任务 retries=0**（`pipeline.py:_execute_stage_tasks` 调 `run_serial(tasks, retries=0, ...)`），**parallel 模式根本没有重试**（`run_parallel` 无 retries 参数）。所以"盲目重试同一 prompt"更精确的表述是：单批 serial 内盲重试 + pipeline 按轮盲重跑（`--round-delay` 边界同步重启）。
- "validator 输出被丢弃"：半对——`_save_validated` 会把失败验证器的 stderr/stdout 各末 10 行写进日志（`log.warning("  | ...")`），但确实**未回喂给 agent**，且验证器不变时同一 prompt 重试是**确定性失败**（浪费）。修正后的缺口表述：捕获验证器摘录 → 存入 TaskResult → 下一轮拼进 prompt（日志已有摘录逻辑可复用，改动小）。
- 熔断/隔离：属实缺失。

**方向 3（指纹缓存）— 成立，动机需重排**
- `--reuse` = 存在即跳过：属实（`cli.py:_filter_reused`、`pipeline.py:_stage_from_dir_tasks`、`_aggregate_tasks` 三处均为 `exists()` 判定）。
- 陈旧产物污染下游：属实，且**实际危害比原提案更强**——reuse 命中时任务被直接丢弃，**验证门禁被整体绕过**：`_filter_reused` 直接过滤（单批），`execute_stage` 的"全部复用"短路 `return [], True` 不经过 `_save_validated`（pipeline）。改了验证器配置或升级了 `cli.py check` 后，续跑场景门禁静默失效。指纹缓存（输入 hash + agent bin + model + 验证器集）恰好同时修复此洞——这是方向 3 的第一收益，正确性而非性能。
- **修正**："指纹未变时跳过 git commit 消除噪音"不成立——当前代码只提交本轮产出（`_git_commit_single_batch`、`_git_commit_stage` 都只收本轮的 success outputs），复用的文件根本不会进提交。此动机删除。
- aggregate 全量重读：属实（`_combine_outputs` 每次拼接全部上游产物），指纹驱动的增量合并顺带解决（收益场景：输入稳定的 `from_dir`/`from_outputs` 流水线）。

**方向 4（自适应并发/流水线化/验证器并行）— 方向成立，一处机制错误**
- **机制错误**："任务集体触发限流时，所有任务一起进入 30s 退避"不成立——30s 保底只存在于 serial 重试路径；parallel 模式无重试，限流任务直接 FAIL，全体在下一轮 `--round-delay` 边界**同步重启**。雷暴是"轮同步"而非"退避同步"，且 `--round-delay` 默认 60s 反而放大延迟。结论（缺自适应并发）仍成立，但问题形态是轮边界的同步突发 + 固定 worker 无观测。
- 阶段严格串行、无预渲染：属实，但收益需降级——任务装配是文件读取 + 字符串替换，成本极低；预渲染省不了什么。真正值得做的是 aggregate 增量合并（并入 D）与验证器并行。
- 验证器 AND 串行：属实（`_save_validated` 的 `for raw in commands` 顺序执行）。并行化是低风险小改：临时文件在全部验证器前已写好、验证器只读 `{output}`，包线程池即可（保持 AND 语义 + 退出码归集），30 分钟级。

**方向 5（遥测/journal/通知）— 成立，一处过度断言**
- "会话 id 丢失"不准确：session id 由 `--session-name`/source stem 确定性派生，崩溃后从命令行参数即可重现；真正丢失的是轮次计数、重试计数、退避进度、stdin 任务（进程内变量）。
- `--log-file` 无限追加（`logging.FileHandler`）：属实，7×24 数月必爆盘——`RotatingFileHandler` 一行修复，归入 C3。
- 无 JSONL 事件流、无通知钩子：属实。gate REJECTED / 熔断 / 任务全败无人唤醒——"无人值守"缺的最后一环，定性准确。
- journal 续跑：属实缺失，且与方向 3 的 sidecar 共享状态存储——**元数据格式应一次定稿**，避免 manifest/sidecar/事件流三套并存。

**补充（信任边界）— 成立，两点量化**
- `from_dir`/`@file.md` 直接内联不可信内容：属实（`_stage_from_dir_tasks` 原样读文件当 prompt；`resolve_prompt` 裸替换 `@file`），注入指令可直达 agent。最小缓解：`_combine_outputs`/`resolve_prompt` 加围栏 + prompt 模板标注"以下为不可信数据"——缓解而非消除。
- 产物无上限：属实且**风险排序应为 runner OOM 先于磁盘**——`_read_stream` 把全部 stdout 收进内存列表，GB 级产物先爆的是进程内存。修复形态：流式计数上限 + 超限拒绝。
- 密钥扫描：机制今天就有——validators 注册表加一条 `secretscan: "python scan.py {output}"` 即挂上（零代码），缺的是内置扫描器。

## 合并路线图（与其余轮次对齐的定稿表）

| # | 工作线 | 本轮来源 | 规模 |
|---|---|---|---|
| C3 | 计量底座：会话文件 usage diff + JSONL 事件流 + 日志轮转 + webhook 钩子 | 方向5 + 方向1（修正后） | 半天 |
| C2 | 验证失败回喂：验证器 stderr/exit 摘录入 TaskResult，retry 时拼进 prompt | 方向2 | 小 |
| C1 | reuse 新鲜度校验：reuse 时对已有产物重跑验证器，失败则重生成（本轮核验新确认的洞） | 方向3 | 极小（~50 行） |
| A | 失败治理：三态分类（transient/permanent/environment）→ 任务级熔断隔离 → 批次级停滞检测 → 告警 | 方向2 | 半天~一天 |
| B | 预算熔断：`--budget-max` 触顶停轮（先尝试/轮次维度，token 精确随 C3 就绪） | 方向1 | 小 |
| D | 指纹缓存 `--reuse` 2.0 + sidecar（provenance 格式一次定稿）+ aggregate 增量合并 | 方向3 | 中 |
| E | 会话轮转：水位监控 + 新 session id（保留旧 id 追溯），`session_flags` 加 fork 键 | 方向1 | 极小 |
| F | 调度治理：429 频率观测 → 动态 worker + 轮边界抖动去同步 | 方向4 | 小~中 |
| G | 验证器 AND 并行（独立小提交，AND 语义不变） | 方向4 | 30 分钟级 |
| I | 信任链：输出字节上限（防 OOM 先于防磁盘）+ 证据围栏 + 密钥扫描器 | 补充 | 小 |

**优先级定稿**：C1 是最便宜的实质正确性修复，可最先落；C3 与 A 并行打头（C3 是预算/遥测/通知的共享底座，A 是唯一现有机制完全失明的失败类，且与 C2 同一条线）；B、E 是 C3 上的小挂件；G 随时可插；D、F、I 为中量项；阶段预渲染并入 D（收益低，不单列）。与原文优先级（2→1→3→4/5）的差异有三处：① 预算熔断降级为"轮次维度 1.0"，不先行做 CLI 输出解析——会话文件 usage diff 更可靠且事件流是正确挂载点；② 方向 3 保持 P1 而非"紧随其后"——其触发条件是需要显式 `--reuse` 的 opt-in 场景，且 C1 已先行堵住门禁绕过；③ 方向 4 拆成 G（立即可做）与 F（依赖 C3 的事件流观测）。

是否按 C1 → C3 → A（含 C2）→ B/E → G → D 的顺序开始实施？第一笔建议是 C1（reuse 门禁校验，~50 行，配测试过 `make ai-dev-test`），随后 A 整条线分三个小提交落地（分类函数 → 熔断 → 反馈再生）。
