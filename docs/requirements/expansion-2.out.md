两份提案我对照全部代码逐条核验完毕。总体结论：**第二次提案与第一次的重叠度约 70%**（方向 2≈第一次的 #3+#5，方向 1≈第一次的 #1+#4，方向 5≈第一次补充），真正新增的是方向 3 的结构化门禁协议和方向 4 的异构路由。以下按"新事实核查 → 需要修正的断言 → 合并路线图"给出结论。

## 逐方向核验

**方向 1（预算/额度）— 成立，但"最坏情况"表述需修正**
- 被动撞 429 再等 30s：属实（`_RETRYABLE_REASON`，`runner.py:203`）。
- "meta 编排者反复挑选新角色"**不是无上限的**：meta 有 `max_iterations` 硬上限（默认 3，`models.py` Stage 字段）。真正无上限烧钱的场景是两个：(a) **滚动分析模式**——每轮 prompt 成功 → 归档 → 下一轮新输出路径，天然无限循环且每轮都"成功"；(b) `--max-rounds 0` + 确定性失败任务。你点出的"一直成功烧钱"在 (a) 里是字面成立的，这点比第一次提案说得更准。
- 主动限速（worker 数与厂商配额联动）与第一次 #4 的自适应并发是同一件事，合并。
- 归因 manifest 与第一次 #5 的 JSONL 事件流是同一件事，合并。

**方向 2（manifest/检查点）— 成立，滚动分析收益断言需要修正**
- "陈旧产物 + 空/损坏文件"两个 edge case：均属实。补充一个佐证——工具自身**永不写空产物**（空输出命中失败签名 `agent produced no output` 被拒），所以空文件只能来自外部写入者，但 `exists()` 判定确实对它失明。
- **修正**："hash 缓存让滚动分析类任务几乎零成本"不成立：滚动分析每轮输出路径带新时间戳、成功后即归档，`--reuse` 永远不命中；且其语义就是"重跑"。hash 缓存的真实收益场景是**输入稳定的 from_dir/from_outputs 流水线**（上游不变 → 下游零成本跳过）和崩溃续跑。这个修正影响优先级论证——方向 2 的收益重心应从"性能"移回"正确性"。
- 崩溃恢复丢的是轮次计数、失败计数、stdin 任务（进程内变量）；session id 可重现不丢。属实。

**方向 3（结构化门禁）— 新增内容成立，"验收闭环"与第一次 #2 是同一机制**
- 布尔 AND + VERDICT 行特例：属实（`_save_validated` + `_GATE_VERDICT_RE`）。
- JSON 报告协议（pass/warn/fail 分级）是**真正的新增**，且实现是收敛的：只需改 `_save_validated` 一处——验证器 stdout 输出 JSON 则解析分级，无 JSON 则回退退出码语义，向后兼容现有 6 个注册验证器。
- "自动生成测试 → 执行测试"的验收闭环 = 第一次 #2 的 validator 反馈再生 + 一个生成测试的阶段。现有 `commands`（阶段后钩子）只能跑不能反馈，闭环的关键缺口仍是再生机制。
- 结构契约校验：目前唯一结构特例确为 VERDICT 行。注意 JSON schema 校验**今天就能用验证器命令表达**（如 `python -m jsonschema --instance {output} schema.json`），缺的只是声明式入口，属小改。

**方向 4（异构路由）— 方向成立，"缺 agent/provider 维度"有一处事实错误**
- **Task 已有 `provider` 字段**且 `to_cmd` 会发出 `--provider`（`models.py:83-89`），单批 YAML 任务文件也能设置（`load_tasks` 的 `Task(**...)` 透传）。"缺 provider 维度"不准确。
- 真正的缺口（"半成品"定性准确）：pipeline 的 `_task_from_def` 只拷贝 model/cwd/timeout——**provider/thinking/tools/env 全部被丢弃**；meta 角色任务 `Task(prompt=..., output=...)` **连 model 都不带**；CLI 无 `--provider` 覆盖。所以第一步不是新建能力，而是**接通已存在但被丢的字段**——低风险小改。
- per-agent 适配层（session_flags 降级为 per-agent）：属实，当前 `session_flags` 全局一份。
- 跨厂商 meta 默认执行：好设计，依赖上面的接通。

**方向 5（信任链/内容安全）— 成立，两点补充**
- 证据折叠注入传播面：属实（`_combine_outputs` 与 `_run_meta_iteration` 的 `combined +=` 都是裸拼接）。最小改动点是 `_combine_outputs` 加围栏（XML/markdown 引块）+ prompt 模板加"以下为不可信数据"指令——但注意这是**缓解不是消除**，agent 仍可能被影响。
- 密钥扫描：**今天就能挂**——validators 注册表加一条 `secretscan: "python scan.py {output}"` 即可，零代码；缺的是内置扫描器，不是机制。
- "自动 git 提交是最危险的泄露出口"：同意，且泄漏的产物已被归档+提交双重固化，`_git_commit_stage` 与 `_archive_outputs` 都是即时的。
- 溯源元数据与第一次 #3 的指纹 sidecar 是同一设计决策，**格式应一次定好**，避免 manifest/sidecar/事件流三套元数据并存。

## 合并路线图（按改动面与依赖排序）

| 优先级 | 工作线 | 来源 | 改动面 |
|---|---|---|---|
| P0 | 失败分类函数 → 任务级熔断（隔离+告警）→ validator 反馈再生 | 一#2 + 二#3(闭环) | `models.py` + `runner.py` retry 分支 + `cli.py` 参数 |
| P0 | 验证器 AND 并行 | 一#4 | `_save_validated` 包线程池，30 分钟级 |
| P1 | 指纹 manifest（sidecar 格式一次定稿，含 provenance 字段） | 一#3 + 二#2 + 二#5(溯源) | 统一 6 处 `exists()` 判定 + `_combine_outputs` |
| P1 | 输出字节上限（先于磁盘问题的是 runner OOM）+ 证据围栏 | 一补充 + 二#5 | `_read_stream` 计数 + `_combine_outputs` |
| P1 | JSONL 事件流 + webhook 通知 + 预算熔断 1.0（尝试次数/轮次维度，不依赖 token 解析） | 一#5 + 二#1 | `runner.py` 埋点 + `cli.py` |
| P2 | 异构路由接通（`_task_from_def` 透传 provider/thinking/tools、meta 角色带 model、CLI `--provider`） | 二#4 | 3 处小改 + 策略 |
| P2 | 结构化验证器 JSON 协议 + 结构契约声明 | 二#3 | `_save_validated` 一处 + 文档 |
| P2 | 会话轮转（上下文水位 → 新 session id 保留旧 id 追溯） | 一#1 | `run_serial` + 配置 |

与两次提案的优先级差异只在两处：① 预算熔断降级为"尝试/轮次维度 1.0"而非 token 记账先行（解析 CLI 输出脆弱，且事件流是记账的正确挂载点）；② 方向 2 从"第一梯队"降为 P1——因为滚动分析的零成本收益不成立后，它从"三件事一起解决"变成"正确性一件事"，而 P0 的熔断+再生同样解决收敛性与成本，改动还更小。

是否按此路线从 P0 开始实施？如果要，我建议第一个落地的是"失败分类函数 + 熔断 + 反馈再生"整条线（三小步可分别提交、各配测试），之后验证器并行作为独立小提交跟进。
