# AADM-R：渐进式 Agent 执行内核（Runtime）

> **定位**：AADM 理论模型的**运行时降级版**。理论模型（`docs/AADM.md`）
> 定义"完整形态"；本文件定义"默认形态"——**从一条链、一个 Agent、
> 一个方案开始，只有证据表明需要时才升级成图、多 Agent、多路径和高强度
> 验证**。它是轻量、渐进、可升级的执行内核。
>
> **状态图例**：✅ 已落地（仓库有对应实现）｜🟡 部分落地｜🔴 待实现
>
> **核心原则**：默认不能一收到 Prompt 就启动多 Agent、超图、三套架构和
> 完整治理流程。同时允许降级——Dry-run 发现任务比预期简单时回到更低模式。

## 0. 执行流程

```
Prompt
  ↓
Probe：低成本探测 / Dry-run
  ↓
Assess：判断任务规模、风险、不确定性
  ↓
Compose：构造链、图、候选路径
  ↓
Schedule：决定串行、并行和 Agent 数量
  ↓
Race：必要时让多个方案分阶段竞跑
  ↓
Verify：统一验证、比较、选择、提交
  ↓
Replan：证据变化后重新规划
```

这不是固定流水线，而是一套**由简单到复杂的升级机制**。

## 1. 最小运行模型（六对象）🟡

默认只需六个对象，不必一开始就把 Prompt 拆成认知原子和超图：

| 对象 | 用途 |
|---|---|
| TaskNode | 任务节点（最小调度单元） |
| PlanGraph | 执行图 |
| StrategyPath | 候选路径 |
| AgentAssignment | Agent 分配 |
| EvidenceRecord | 验证证据 |
| RuntimePolicy | 运行策略 |

只有复杂业务、跨系统、高风险、长期任务时，才把简单节点细化为认知原子、
决策事务和类型化超图（见 `docs/AADM.md`）：

| 任务 | 结构 |
|---|---|
| 简单任务 | Prompt → 任务链 → 执行 → 验证 |
| 一般任务 | Prompt → DAG → 并行执行 → 集成验证 |
| 探索任务 | Prompt → AND/OR 图 → 多路径实验 → 比较选择 |
| 关键任务 | Prompt → 状态图/超图 → 门禁 → 审查 → 回滚治理 |

## 2. 四级渐进模型 🟡（仓库：assessor 工作流 L0-L3 为雏形）

| 等级 | 典型任务 | 表达结构 | Agent | 候选方案 |
|---|---|---|---|---|
| L0 局部模式 | 文案、样式、小 Bug | 单链表 | 1 | 1 |
| L1 标准模式 | 单模块功能、普通前后端功能 | DAG | 1～2 | 1～2 |
| L2 探索模式 | 跨模块、高不确定、新技术 | AND/OR DAG | 2～4 | 2～3 |
| L3 治理模式 | 跨项目、权限、资金、审计、生产数据 | 动态状态图 | 多专业 Agent + Reviewer | 多路径分阶段验证 |

**默认从 L0 开始评估，而不是从 L3 开始。**

## 3. 升级 / 降级触发器 🟡

升级只由明确触发器决定：

- 影响范围扩大
- 公共契约发生变化
- 数据不可逆
- 存在安全或权限风险
- 跨服务、跨仓库
- 未知项较多
- 没有可复用的已有模式
- 需要独立验证
- 多个技术方向难以仅靠分析判断

**同时允许降级**：Dry-run 原本估计会涉及后端，但检查后发现已有 API、
只需增加一个前端入口，任务应从 L1 降回 L0。

## 4. Dry-run 三深度 🟡

Dry-run **不是写一份长计划，而是低成本获取信息**。

### D0：语义探测（只分析 Prompt，不读取大量上下文）

```yaml
intent: feature
likely_scope:
  - frontend
possible_risks:
  - permission
unknowns:
  - 是否已有导出 API
likely_shape: chain_or_small_dag
confidence: 0.42
```

作用只是判断大概方向。

### D1：只读上下文探测

读取最小必要上下文：相关目录、相同功能实现、接口定义、数据模型、权限
配置、测试文件、项目约束。**不修改代码、不提交、不执行不可逆操作**。

```yaml
found:
  existing_export_api: true
  existing_permission: order.export
  reusable_component: ExportButton
likely_change:
  files: 3
  projects: 1
risk: low
uncertainty: low
recommended_mode: L0
recommended_agents: 1
recommended_paths: 1
```

### D2：安全模拟（不能仅靠阅读确认的任务）

临时分支、接口草案、最小 Spike、静态分析、已有测试、模拟数据迁移、性能
微测试、构造失败场景——**不进入真实生产状态**。例如数据库变更：

```
生成 migration → 在临时数据库执行 → 检查历史数据兼容 → 执行 rollback → 记录结果
```

### 信息收益判据

```
ProbeValue = ImpactOfUnknown × ExpectedUncertaintyReduction − ProbeRisk − ProbeEffort
```

只有探测价值为正，才继续扩大 Dry-run。**Agent 不能为了"了解得更全面"
无差别读取整个仓库。**

## 5. Dry-run 标准输出 ✅（仓库：`pi-batch probe`，确定性 ProbeReport，区间+置信度）

```typescript
interface ProbeReport {
  intent: string;
  likelyScope: {
    files?: number; modules?: number; projects?: number;
    actors?: string[]; systems?: string[];
  };
  profile: {
    clarity: number; scope: number; coupling: number; risk: number;
    uncertainty: number; reversibility: number; novelty: number;
  };
  evidenceFound: string[];
  unknowns: string[];
  conflicts: string[];
  candidateDirections: string[];
  recommendedMode: "L0" | "L1" | "L2" | "L3";
  recommendedGraph: "chain" | "dag" | "and-or-dag" | "dynamic-graph";
  estimatedAgents: number;
  estimatedPaths: number;
  estimates: {
    elapsedTime?: EstimateRange;
    tokens?: EstimateRange;
    monetaryCost?: EstimateRange;
  };
  confidence: number;
}

interface EstimateRange {
  p50: number;
  p90: number;
  confidence: number;
}
```

估计必须带区间和置信度——Prompt 刚开始时，估计本身也有很大不确定性：

```yaml
elapsed_time:
  p50: 10
  p90: 25
  confidence: 0.55
```

不能只给出"预计花费 10 分钟"这种伪精确值。

## 6. 结构升级：链 → DAG → AND/OR 图 → 状态图 🟡

### 局部任务用链表（默认结构）

```
调整订单页按钮间距：
读取相关组件 → 检查设计系统间距变量 → 修改样式
→ 检查响应式布局 → 运行 typecheck → 视觉确认
```

一个 Agent、一条路径足够。

### 存在独立工作时升级成 DAG

```
增加订单导出功能：
读取现有实现 ─┬─ 权限检查设计 ─┐
              ├─ 导出 API 检查 ─┼─ 实现功能 ── 集成验证
              └─ 前端交互设计 ─┘
```

三个调查节点可并行，实现节点等待它们完成。

### 存在替代方案时升级成 AND/OR 图

```
需求与验收标准 ─┬─ 方案 A：同步导出 ─┐
                ├─ 统一测试 ── 选择
                └─ 方案 B：异步导出 ─┘
```

- AND 节点：所有子节点都要完成
- OR 节点：候选中选择一个
- VERIFY 节点：所有路径使用相同验收标准
- MERGE 节点：允许组合兼容的局部成果

### 存在循环、长任务和动态变化时使用状态图

```
计划 → 执行 → 验证失败 ──→ 修复 ──→ 再验证
              ↓
            验证通过 → 提交
```

**链表是图的退化形式，复杂任务才需要完整图模型。**

## 7. 任务节点（最小调度单元）🟡

节点不应大到"完成整个系统"，也不应小到"修改一行代码"。合适节点：

1. 只有一个主要目标
2. 有明确输入
3. 有明确输出
4. 有一个主要责任人
5. 可以独立验证
6. 失败后可以独立重试或替换

```typescript
interface TaskNode {
  id: string;
  kind: "probe" | "design" | "implement" | "test" | "review"
       | "compare" | "merge" | "gate" | "rollback";
  goal: string;
  inputs: string[];
  outputs: string[];
  dependsOn: string[];
  readSet: string[];
  writeSet: string[];
  requiredEvidence: string[];
  risk: number;
  uncertainty: number;
  flexibility: "fixed" | "select" | "generative";
  parallelSafe: boolean;
  refineable: boolean;
  requiredCapability?: string;
  estimate: { time?: EstimateRange; tokens?: EstimateRange; cost?: EstimateRange };
  status: "pending" | "ready" | "running" | "verified" | "failed" | "pruned";
}
```

`flexibility` 保留主观能动性：

| 取值 | 含义 | 示例 |
|---|---|---|
| fixed | 必须按明确方式执行 | 运行数据库回滚测试、检查指定安全规则、验证 API 向后兼容 |
| select | 从已知方案中选择 | 分页还是虚拟滚动、同步接口还是异步任务 |
| generative | 在约束内创造新方案 | 重新设计页面操作路径、提出更合理的领域模型 |

**图不是把 Agent 每一步写死，而是固定必要边界、开放可创造节点。**

## 8. 图本身必须是动态的 ✅（仓库：`graph ops` 七操作映射——expand/collapse/replace/prune/insert/reroute/merge 均有对应实现：gate_fix/--reuse/meta 重规划/racing 淘汰/aggregate）

| 操作 | 含义 |
|---|---|
| expand | 把一个抽象节点细化为子图 |
| collapse | 把已验证的子图折叠成能力 |
| replace | 用更合适的实现替换节点 |
| prune | 删除失败或无价值路径 |
| insert | 插入调查、验证或修复节点 |
| reroute | 根据新证据改变路径 |
| merge | 合并兼容结果 |

示例——原计划"实现导出按钮 → 测试"，调查后发现没有导出接口：

```
实现导出按钮 → 检查数据量 → 设计导出接口 → 实现后端任务
→ 实现前端状态反馈 → 测试
```

这就是滚动规划：**不是一开始就强行预测全部细节。**

## 9. 串行还是并行，由数据依赖和状态冲突决定 🟡

两个节点可以并行，至少需要：

```
Parallel(i, j) = NoDependency ∧ NoWriteConflict ∧ IndependentVerification ∧ Mergeable
```

具体地：

- i 不依赖 j 的输出，j 不依赖 i 的输出
- 双方不修改同一不可合并状态
- 执行结果可以独立验证
- 存在明确的合并契约

用读写集判断：

```
W_i ∩ (R_j ∪ W_j) = ∅    且    W_j ∩ R_i = ∅
```

（R：节点读取的对象；W：节点修改的对象）

可并行示例：分析 aero-id 身份接口 ∥ 分析 aero-im 通知接口 ∥ 分析审计
事件规范。应串行示例：确定审批状态机 → 设计数据库结构 → 实现状态迁移
（后续节点依赖前面的设计结果）。

## 10. Agent 分配：节点是调度单位，不一定是 Agent 分配单位 🟡

为减少上下文重复和沟通成本，把相关节点**聚类成子图**（身份与权限子图、
文件状态子图、审计与通知子图、前端交互子图、集成验证子图）。每个 Agent
持有一个语义连续的子图，而不是每个 Agent 只修改一个函数。

```
AgentBenefit = SpecializationGain + IndependentEvidenceGain + CriticalPathReduction
             − CoordinationCost − ContextDuplication − MergeRisk
```

只有 `AgentBenefit > Threshold` 才增加 Agent。**默认策略是 1 个 Agent**：

| 触发情况 | 增加的 Agent |
|---|---|
| 存在两个以上独立工作流 | 专业执行 Agent |
| 涉及不同专业领域 | 领域专家 Agent |
| 高风险或关键状态变更 | 独立 Reviewer |
| 存在多个候选方案 | 并行方案 Agent |
| 上下文过大且可按边界分割 | 子系统 Agent |

**不能因为涉及六个项目就机械派六个 Agent**——若六个项目修改高度相关，
派六个 Agent 反而产生重复读取、契约冲突、状态不同步、代码合并困难、
责任模糊。

## 11. Agent 调度使用"波次"执行 🟡（仓库：runner 轮循环 + meta 并发为雏形）

拓扑调度：

1. 找出所有依赖已经满足的节点
2. 从中筛选可以并行的节点
3. 按领域和上下文相似度聚类
4. 为每个节点簇分配 Agent
5. 执行当前波次
6. 收集证据
7. 更新任务图
8. 进入下一波次

```python
while not graph.completed():
    ready_nodes = graph.getReadyNodes()
    groups = partitionByDependencyConflictAndContext(ready_nodes)
    assignments = assignAgents(groups, runtime_policy)
    results = executeWave(assignments)
    graph.attachEvidence(results)
    graph.updateStatuses(results)
    if results.hasUnexpectedOutcome():
        graph = replan(graph, results)
    if results.hasFailedPath():
        graph.pruneOrRepair(results.failedNodes)
```

典型波次：

```
Wave 1: Agent A 检查身份与权限 ∥ Agent B 检查文件状态模型 ∥ Agent C 检查审计与通知契约
Wave 2: Agent A 设计审批状态机和 API ∥ Agent B 设计 UI 流程
Wave 3: Agent A 后端实现 ∥ Agent B 前端实现
Wave 4: Reviewer 跨项目集成验证
```

## 12. 多方案采用分阶段竞跑（不直接全部实现）✅（仓库：racing.py Successive Halving / 逐轮减半）

允许多技术路径并行，但采用**分阶段竞跑**：

| 轮次 | 内容 | 成本 |
|---|---|---|
| 第 0 轮 | 方案级 Dry-run：架构草图、关键数据结构、状态流转、风险、兼容性、验证计划 | 很低 |
| 第 1 轮 | 最小 Spike：每条路径只实现最关键、最不确定的部分（状态一致性、失败恢复、重复请求、审计可靠性、集成难度） | 低 |
| 第 2 轮 | 代表性切片：对剩余方案实现一条完整业务链（提交审批 → 审批通过 → 写入审计 → 通知申请人），使用同一套数据、接口契约和测试 | 中 |
| 第 3 轮 | 完整实现：只完整实现胜出的方案；必要时组合兼容部分（必须经过 Merge 节点验证契约兼容性） | 高 |

```
多路径生成 → 低成本筛选 → 逐步增加投入 → 提前淘汰弱方案 → 最终收敛
```

可以采用"逐轮减半"：3 个方案 → 保留 2 个 → 保留 1 个 → 完整实现。

> 专业注：这种"逐轮减半"正是多臂老虎机领域的 **Successive Halving /
> Hyperband** 算法（见 `docs/AADM_GOVERNANCE.md` §37 补充要点）。

## 13. 多路径必须先固定共同验收标准 🟡（仓库：gate + acceptance 门）

不同 Agent 不能各自定义"什么叫成功"。分支执行前先冻结：外部接口、
输入输出、权限要求、业务不变量、测试数据、性能负载、失败场景、验收标准。

```
共同契约与测试 ─┬─ Path A ─┐
                ├─ Path B ─┼─ 统一验证器 ── 比较 ── 提交
                └─ Path C ─┘
```

每条路径输出：

```typescript
interface StrategyResult {
  strategyId: string;
  implementation: string[];
  evidence: EvidenceRecord[];
  acceptanceCoverage: number;
  correctnessConfidence: number;
  securityFindings: string[];
  maintainabilityFindings: string[];
  performanceMetrics: Record<string, number>;
  assumptions: string[];
  unresolvedRisks: string[];
  compatibleWithOtherPaths: string[];
}
```

比较者不能只看实现 Agent 的自我评价。优先使用：编译结果、测试结果、
性能测试、故障注入、契约测试、安全扫描、运行日志；其次才是 Reviewer
的解释性判断。

## 14. 质量和资源必须分开保存 🟡（仓库：metering.py 为雏形）

不要把质量、时间、Token、金钱一开始就揉成一个总分：

```
方案质量向量   Q(p) = [Correctness, GoalFit, Security, Maintainability, Testability, Evidence]
资源消耗向量   B(p) = [ElapsedTime, TokenUsage, MonetaryCost, CoordinationOverhead]
```

默认：

```
p* = argmax Q(p)          # 且只记录 B(p)
```

即：**默认由正确性、业务目标和证据决定方案，资源指标只用于观测和报告。**

## 15. 两种时间必须区分 🟡

| 类型 | 示例 | 性质 |
|---|---|---|
| 业务时间约束 | Token 15 分钟后过期、发布窗口、订单 30 分钟不能取消、消息 5 秒内到达 | **业务正确性，始终影响决策** |
| Agent 运行时间 | 预计规划 3 分钟、实现 15 分钟、多 Agent 可缩短墙钟时间 | 资源指标，是否影响方案选择由开关决定 |

二者不能混为一谈。

## 16. 时间、Token、成本的计算 🟡

```
T_serial = Σ T_i                     # 串行节点
T_elapsed ≈ CriticalPathLength(Graph)  # 并行时接近关键路径，不是简单相加

Tokens = SharedContext + Σ AgentPrivateContext + Σ Reasoning
       + Σ Outputs + MergeContext + ReviewContext

Cost = ModelTokenCost + ToolComputeCost + ExternalServiceCost + CoordinationCost
```

多 Agent 最容易产生的隐形 Token 消耗：重复读取同一仓库、重复解释同一
背景、重复总结、重复合并。Token 优化重点不是"让回答短一点"，而是：
共享基础上下文、按领域分配子图、避免 Agent 重复读取、使用结构化中间
结果、让同一 Agent 连续处理相关节点。

成本估计也应保留**区间和置信度**，而不是伪精确值。

## 17. 资源开关分两个层次 🟡

最重要的设计是区分：**是否影响调度** 与 **是否影响技术方案**。

```yaml
resource_policy:
  observe: true
  affect_schedule: true
  affect_solution: false
```

含义：始终记录时间/Token/成本；可利用安全并行减少耗时；可以减少重复
上下文；**但不能因为某方案更便宜就选择质量明显更差的方案**。

| 模式 | 资源指标的作用 |
|---|---|
| observe_only | 只记录，不改变执行 |
| schedule_only | 影响并发、排序、上下文分配，不改变技术方案 |
| balanced | 可影响 Agent 数量、候选路径和模型选择 |
| hard_cap | 时间、Token、成本成为硬上限 |

## 18. 资源开关开启后能改变什么 🟡

| 优化 | 可调整 |
|---|---|
| 时间 | 提高并行度、优先关键路径节点、并行安全 Dry-run、候选方案投机执行、缩短每轮滚动规划范围 |
| Token | 减少重复 Agent、合并上下文相近节点、限制候选路径数量、摘要替代完整历史、同一 Agent 连续处理同一子图、减少讨论轮次 |
| 成本 | 低风险节点用低成本模型、高风险节点用强模型、减少重复 Reviewer、优先确定性工具、限制候选方案完整实现数量、降低非关键节点推理深度 |

**但不能取消**：硬约束检查、安全验证、必要的权限检查、关键数据一致性
验证、用户明确的验收标准、高风险操作的回滚能力。

## 19. 资源影响方案时，必须设置质量底线 ✅（仓库：pareto.select_with_quality_floor + `pareto --quality-floor`）

不要直接使用 `Quality − Cost`（会用低成本掩盖低质量）。分层决策：

1. 第一层：满足全部硬约束
2. 第二层：达到最低质量标准
3. 第三层：达到全部验收标准
4. 第四层：在近似质量方案中优化资源

```
max Utility(p)
满足  HardConstraints(p) = true
      Quality(p) ≥ Q_min

Utility(p) = Q(p) − λ_t·T(p) − λ_k·Tokens(p) − λ_c·Cost(p)
```

推荐默认 `affect_solution: false`。只有明确要求"优先最快完成 / 控制在
固定 Token 内 / 只能使用某预算 / 优先最低推理成本"时才开启。

## 20. 资源不足时不能伪装成完成 ✅（仓库：`probe --budget-tokens` → infeasible_under_budget + cannot_remove）

使用 hard_cap 时，若预算无法满足最低质量要求，返回：

```yaml
status: infeasible_under_budget
minimum_required:
  time: ...
  tokens: ...
  cost: ...
current_limit:
  time: ...
  tokens: ...
  cost: ...
cannot_remove:
  - security_verification
  - migration_rollback_test
  - contract_test
```

**不能偷偷删除测试后声称已完成。**

## 21. 运行策略配置 ✅（仓库：pi-batch.yaml + config.py：timeout/retries/validators/evidence/session 全声明式）

```yaml
runtime_policy:
  start_mode: L0
  auto_escalate: true
  auto_deescalate: true

  dry_run:
    enabled: true
    initial_depth: semantic
    allow_read_only_scan: true
    allow_sandbox_spike: true
    allow_irreversible_action: false

  planning:
    default_representation: chain
    promote_to_dag_when_parallel: true
    promote_to_and_or_when_alternatives: true
    rolling_replan: true

  agents:
    default_count: 1
    add_specialist_on_domain_split: true
    add_reviewer_on_high_risk: true
    avoid_one_agent_per_node: true
    max_parallelism: 4

  strategies:
    default_paths: 1
    max_paths: 3
    use_staged_racing: true
    require_shared_acceptance_suite: true
    prune_failed_paths_early: true

  resources:
    observe: true
    affect_schedule: true
    affect_solution: false
    optimization_mode: quality_first
    weights: { time: 0.0, tokens: 0.0, monetary_cost: 0.0 }
    hard_caps: { time: null, tokens: null, monetary_cost: null }

  quality:
    never_skip_required_proofs: true
    never_weaken_security_constraints: true
    never_claim_unexecuted_tests_passed: true
```

开启预算影响时：

```yaml
resources:
  observe: true
  affect_schedule: true
  affect_solution: true
  optimization_mode: balanced
  weights: { time: 0.4, tokens: 0.2, monetary_cost: 0.4 }
  quality_floor:
    acceptance_coverage: 1.0
    required_tests_passed: true
    unresolved_critical_risks: 0
```

## 22. 完整运行算法 🟡（各步骤组件已落地：1-3 probe → 4-7 profile/impact/atomize → 8 racing → 10-11 runner+gate_fix → 12 pareto → 13 gate 验收门；完整编排= pipeline 组装）

```python
def executePrompt(prompt, policy) -> ExecutionResult:
    # 1. 最低成本 Dry-run
    probe = runDryProbe(prompt, depth="semantic", irreversibleActions=False)

    # 2. 必要时只读扩大上下文
    if probe.confidence < 0.7 or probe.hasHighImpactUnknowns():
        probe = extendProbeWithReadOnlyContext(probe)

    # 3. 选择最低充分运行模式
    mode = chooseLowestSufficientMode(probe)

    # 4. 默认先生成链
    graph = buildInitialChain(prompt, probe)

    # 5. 只有存在真实需要时才升级
    if mode.requiresParallelGraph():
        graph = promoteChainToDag(graph)
    if mode.requiresAlternativeStrategies():
        graph = promoteDagToAndOrGraph(graph)

    # 6. 生成候选方案
    strategies = generateStrategies(graph, count=mode.defaultPathCount,
                                    requireDiversity=True)

    # 7. 硬约束过滤
    strategies = [s for s in strategies if satisfiesHardConstraints(s)]
    if not strategies:
        return buildConstraintConflictResult()

    # 8. 分阶段，而不是直接完整实现
    strategies = runPlanningRace(strategies)
    strategies = pruneInvalidStrategies(strategies)
    if len(strategies) > 1:
        strategies = runSpikeRace(strategies)
        strategies = pruneWeakStrategies(strategies)

    # 9. 构造执行图
    graph = attachStrategiesToGraph(graph, strategies)
    graph = attachSharedVerificationNodes(graph)

    # 10. 滚动执行
    while not graph.acceptanceSatisfied():
        ready_nodes = graph.getReadyNodes()
        groups = partitionIntoCompatibleSubgraphs(ready_nodes)
        assignments = assignAgents(groups, policy, benefitCheck=True)
        results = executeWave(assignments)
        graph.attachResults(results)
        graph.attachEvidence(verifyWave(results))
        updateResourceLedger(results)

        # 11. 根据证据动态变更图
        if graph.hasUnexpectedEvidence():
            graph = replanGraph(graph)
        if graph.needsEscalation():
            mode = escalateMode(mode)
            graph = promoteGraph(graph, mode)
        if graph.canDeescalate():
            mode = deescalateMode(mode)
        graph.pruneFailedOrDominatedPaths()

    # 12. 比较最终候选方案
    feasible = graph.getVerifiedStrategies()
    if not policy.resources.affectSolution:
        selected = selectByQualityAndEvidence(feasible)
    else:
        selected = selectByQualityFloorThenResources(feasible, policy.resources)

    # 13. 最终提交前再验证
    final_evidence = runFinalAcceptanceSuite(selected)
    if not final_evidence.passed:
        return rollbackOrRepair(selected, final_evidence)

    return commitVerifiedResult(selected, final_evidence, getResourceLedger())
```

## 23. 简化量化模型（L0-L3 路由）🟡

Dry-run 不需要一开始计算二十多个指标，轻量模式只用六个核心信号：

| 信号 | 含义 |
|---|---|
| S | Scope 影响范围 |
| R | Risk 失败风险 |
| U | Uncertainty 不确定性 |
| C | Coupling 依赖耦合 |
| N | Novelty 创新程度 |
| V | Reversibility 可回滚性 |

```
Load = max(R, 0.25·S + 0.20·U + 0.20·C + 0.20·N + 0.15·(1 − V))
```

初始阈值：

| Load | 模式 |
|---|---|
| 0.00～0.25 | L0 |
| 0.25～0.50 | L1 |
| 0.50～0.75 | L2 |
| 0.75～1.00 | L3 |

**硬触发器直接升级**（不受 Load 限制）：

| 触发 | 最低模式 |
|---|---|
| 修改认证授权 | ≥ L2 |
| 不可逆数据库操作 | ≥ L2 |
| 跨项目公共契约变更 | ≥ L2 |
| 资金、安全、审计 | L3 |
| 生产数据删除 | L3 |

> **数字只是路由信号，不是真理。** 需要根据历史任务结果不断校准。

## 24. 示例：简单页面修改（L0）🟡

Prompt：把订单页面顶部三个按钮对齐，并统一间距。

```yaml
scope: 0.08   risk: 0.03   uncertainty: 0.10
coupling: 0.02  novelty: 0.01  reversibility: 0.98
mode: L0    graph: chain    agents: 1    paths: 1
```

执行链：读取页面组件 → 查找设计 Token → 调整布局 → 检查不同分辨率 →
typecheck → 视觉确认。

**这里不能启动**设计 Agent、前端 Agent、审查 Agent、三套布局方案、完整
页面重构——这就是"大炮打蚊子"。

## 25. 示例：跨项目审批功能（L3）🟡

Prompt：在 snaplink-console 增加文件审批，审批后记录审计并通知申请人。

Dry-run 后发现涉及：snaplink-console、snaplink、aero-id、aero-vault、
snaplink-audit-governance、aero-im。

```
确定业务流程与共同契约
        ↓
身份权限分析 ∥ 文件状态分析 ∥ 审计通知分析
        ↓
审批状态机与 API 设计
        ↓
后端实现 ∥ 前端实现
        ↓
跨项目集成验证
```

Agent 分配：Coordinator（领域与契约）+ 身份/文件/后端集成 + 前端交互 +
Reviewer（高风险契约与审计检查）。**不必为六个仓库分别启动六个 Agent。**

候选路径：

- Path A：同步服务编排
- Path B：Outbox + 事件驱动

先做 Spike（审批状态提交、审计服务不可用、通知服务超时、重复请求、事件
重复消费），再用同一套故障测试决定路径，而不是只靠架构偏好。

## 26. 规范表达：MUST / SHOULD / MAY

### MUST

- 必须从最低充分模式开始。
- 每个 Prompt 必须先进行低成本 Dry-run。
- Dry-run 默认不得产生不可逆副作用。
- 简单任务必须优先使用链，而不是复杂图。
- 每个节点必须有目标、输出和验证方式。
- 串行和并行必须由依赖与状态冲突决定。
- 增加 Agent 前必须证明专业化、并行或独立审查收益。
- 多方案必须共享同一套外部契约和验收标准。
- 多路径必须分阶段投入，不能默认全部完整实现。
- 时间、Token、成本必须和质量指标分开记录。
- 资源指标默认不得降低硬约束和验收标准。
- 未实际执行的测试不得声称通过。

### SHOULD

- 应该使用动态可修改的任务图。
- 应该用区间和置信度表达资源估计。
- 应该把语义连续节点分配给同一 Agent。
- 应该尽早淘汰违反约束或证据较弱的路径。
- 应该根据运行证据升级或降级任务模式。
- 应该优先确定性工具验证，而不是让另一个 Agent 只做语言评价。
- 应该记录预估资源、实际资源和估计误差。

### MAY

- 可以在安全沙箱中投机执行多个技术路径。
- 可以允许 Agent 将抽象节点细化为新的子图。
- 可以允许 Agent 发现和提出原 Prompt 之外的候选方案。
- 可以在资源开关开启时调整 Agent 数量、路径数量和模型等级。
- 可以在保持质量底线的前提下优化时间、Token 和成本。
- 可以合并不同方案中经过契约验证的兼容部分。

## 27. 最终表达

```
Execution = ProgressiveProbe + DynamicGraph + EvidenceDrivenScheduling
          + StagedMultiPathRace + OptionalResourceOptimization
```

- 先探测，不立即重构。
- 先用链，必要时升图。
- 先一个 Agent，有明确收益才增加。
- 先一个方案，不确定性高才开分支。
- 多路径先做 Spike，不直接全部实现。
- 先统一验收，再比较技术路线。
- 质量和资源分开计算。
- 资源默认只影响调度，不影响方案正确性。
- 需要时通过开关，让时间、Token、成本参与优化。
- 所有计划都可以根据新证据重新构图。

**它既不会一上来"大炮打蚊子"，也不会因为最初把任务看简单了，就失去
升级为复杂工程流程的能力。**

## 28. 与仓库机制的映射

| AADM-R 概念 | 仓库现有实现 | 差距 |
|---|---|---|
| L0-L3 路由 | `classify`（前后端判定）、`assessor`（scale S/M/L + workflow L0-L3）、`rules`（demo/standard/production 三档） | 🟡 档位已存在，无 Load 公式与硬触发器表 |
| Dry-run D0/D1 | `assess --prompt`（编译指令块）、`rules --summary`（压缩需求）、`campaign --dry-run`（不创建目录/日志/状态） | 🟡 无 D2 沙箱 Spike；无 ProbeValue 判据 |
| 链/DAG | pipeline 阶段 DAG（from_outputs/aggregate，完整 DAG 证据合并） | ✅ |
| AND/OR 算子 | **OR**：stage `or_tasks: true`（第一个通过验证器者胜出，全败 fail closed）；**AND**：aggregate 合并；**RACE**：`pbatch/racing.py` 分阶段竞跑 | ✅ |
| 波次调度 | `pbatch/waves.py`：拓扑波次 + 写冲突分解 + 串行化防活锁 + 上下文聚类（`assign_agents`）；runner 轮询认领即设备侧波次 | ✅ |
| Agent 分配 | meta 相关性评分、去重、运行时数量上限 | 🟡 无上下文连续性优化、无 AgentBenefit 公式 |
| 分阶段竞跑 | `pbatch/racing.py` + `pi-batch race`：逐轮减半 + 置信度保守淘汰 + 历史追踪 | ✅ |
| 统一验收 | gate VERDICT（fail closed）+ acceptance 门 + QA 核对 | ✅ |
| 质量/资源分离 | metering 成本计量 + `runtime_policy.resources`（observe/affect_solution 开关；默认不影响方案正确性）+ 预算 `infeasibility_report` | ✅ |
| 资源硬上限 | `profile --budget-time/tokens/cost` → `infeasibility_report`（显式返回 infeasible_under_budget + cannot_remove，不伪装完成） | ✅ |
| 滚动规划 | `execution_horizon`（Horizon 公式：Confidence×Reversibility/(Risk+Uncertainty+ε)）+ L0-L3 路由迟滞阈值（升>0.6 降<0.4） | ✅ |
| 动态图操作 | gate_fix 自动修复循环（insert/repair）、campaign 方向选择（prune）、任务 migrate/cancel（设备织网） | ✅ |
