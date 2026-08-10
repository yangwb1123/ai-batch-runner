# AADM-G：成熟 Agent 系统治理模型（Governance）

> **定位**：AADM 理论模型与运行时内核（`docs/AADM_RUNTIME.md`）之上、
> 面向"二十年工程经验"的治理层。前两份文档解决"Agent 如何聪明地选择和
> 执行"；本文件解决更根本的问题——**当 Agent 选择错误、信息过期、工具
> 失败、多个 Agent 冲突、规则失效、环境变化时，整个系统如何仍然可控**，
> 以及**如何防止这套规则系统自己变成一个失控的复杂系统**。
>
> **四核心**：小而可信的内核、明确的世界状态、独立可靠的证据、可持续
> 演化的治理。
>
> **状态图例**：✅ 已落地（仓库有对应实现）｜🟡 部分落地｜🔴 待实现

## 0. 总览：六平面、三闭环、四保证

```
                    ┌────────────────────┐
                    │    治理平面         │
                    │ 规则、权限、版本、审计 │
                    └─────────┬──────────┘
                              ↓
Prompt → 语义平面 → 世界状态平面 → 规划平面 → 执行平面
                              ↑              ↓
                              └──── 证据平面 ←┘
```

时间、Token、成本、算力作为**横跨所有平面的资源账本**。

### 六平面

| 平面 | 回答的问题 | 仓库对应 |
|---|---|---|
| 语义平面 | 用户真正要什么、谁是角色、操作对象是什么、什么是成功、什么不能发生、哪些是事实/假设/未知 | assessor / product / classifier ✅ |
| 世界状态平面 | 系统现在到底是什么状态、代码和数据什么版本、哪些任务在跑、哪些资源被锁、外部服务是否可用、哪些证据已过期 | campaign_state.jsonl、memory 索引、fingerprint 🟡 |
| 规划平面 | 生成链/DAG/AND-OR 图、候选方案、拆分节点、计算依赖、升级降级、形成波次 | pipeline / meta / advance ✅ |
| 执行平面 | 调用工具、修改代码、跑测试、执行脚本、创建分支、产生真实副作用 | runner（进程组隔离、硬超时）✅ |
| 证据平面 | 收集测试结果、日志、性能数据、契约检查、安全扫描、人工确认、独立 Reviewer 结论；决定"是否真的完成" | evidence.py、验证器、completion 门禁 ✅ |
| 治理平面 | 规则版本、权限边界、模型/工具版本、规则升级与废弃、异常处置、人工门禁、审计与责任追踪 | registry / learn / 决策日志 🟡 |

**任务图表达"准备做什么"，世界状态表达"现在真实是什么"——两者必须分开。**

### 三闭环（不同时间尺度）

| 闭环 | 内容 | 尺度 |
|---|---|---|
| 快速闭环 | 行动 → 验证 → 修复 | 秒～分钟级 |
| 中速闭环 | 更新状态 → 重构任务图 → 重新调度 | 一个任务生命周期 |
| 慢速闭环 | 历史评估 → 调整规则 → 影子验证 → 发布新策略 | 跨任务、跨版本的长期学习 |

### 四保证

| 保证 | 含义 |
|---|---|
| Safety 安全性 | 不进入禁止状态 |
| Liveness 活性 | 最终能够前进，而不是永远等待或循环 |
| Traceability 可追溯性 | 每个决策、行动和结果都能解释来源 |
| Evolvability 可演化性 | 模型、规则、工具和系统升级后仍能兼容 |

## 1. 最重要的架构原则：策略和机制分离 ✅（仓库：pbatch 确定性内核 + 声明式配置）

机制回答"怎么执行"（如何创建任务节点、锁定文件、运行测试、启动 Agent、
回滚、记录事件）；策略回答"该不该执行"（是否需要三个 Agent、是否值得开
第二条方案、是否应该先问用户、是否允许改数据库、是否需要人工审批）。

**不能让每个 Agent 自己同时定义策略、授权自己、执行行动、再评价自己。**

推荐使用一个最小可信内核：

```
Trusted Agent Kernel
├── Policy Engine          规则与权限判断
├── State Store            世界状态与版本
├── Graph Scheduler        图调度
├── Capability Broker      工具与权限分发
├── Evidence Ledger        证据账本
├── Verification Engine    验证
├── Rollback Controller    回滚与补偿
└── Kill Switch            紧急停止
```

产品思维、架构模式、前端规范、DDD、算法选择、多 Agent 策略，都作为
**可插拔策略**存在。灵活性存在于策略插件中，**不侵入可信内核**。

## 2. 任务图之外，必须维护"世界状态" 🔴

任务图写着"修改接口 → 修改前端 → 测试"，但真实世界可能已经发生：另一个
Agent 修改了接口、数据库 migration 执行了一半、用户刚改变需求、外部依赖
升级、测试环境数据被修改。因此需要带版本的世界快照：

```typescript
interface WorldSnapshot {
  id: string;
  asOf: string;
  repositoryVersions: Record<string, string>;
  schemaVersions: Record<string, string>;
  serviceVersions: Record<string, string>;
  activeTasks: string[];
  activeLeases: ExecutionLease[];
  externalConditions: {
    service: string;
    status: "healthy" | "degraded" | "unavailable";
    observedAt: string;
  }[];
  facts: FactRecord[];
  unresolvedIncidents: string[];
  version: number;
}
```

所有计划绑定一个世界状态版本：

```
plan:    based_on_world_version: 124
execution: current_world_version: 130
```

两者差异超过阈值时，**不能继续机械执行，必须重新验证前提**。

## 3. 每个节点必须使用"假设—保证契约" 🔴（仓库：pipeline_schema fail-closed 为雏形）

节点之间不能只写"A 依赖 B"，应明确：

```typescript
interface NodeContract {
  assumptions: Predicate[];     // 执行前假设什么成立
  preconditions: Predicate[];   // 执行前必须满足
  guarantees: Predicate[];      // 执行后保证什么成立
  postconditions: Predicate[];
  invariants: Predicate[];      // 不能破坏的不变量
  forbiddenStates: Predicate[];
  failureSemantics:
    | "atomic" | "retryable" | "compensatable"
    | "best_effort" | "irreversible";
}
```

组合时检查：**上游节点的 guarantees 是否满足下游节点的 assumptions**。

例如：身份节点保证 `userIdentity 已验证 / permissionSet 已获取`；审批节点
假设 `userIdentity 已验证 / permissionSet 包含 file.approve`。

这种方式比"几个 Agent 商量一下"可靠得多。

## 4. 复杂度主要来自关系和状态，不是代码行数 🟡

三个微服务不一定复杂；一个同时涉及用户状态、文件状态、审批状态、通知
状态、审计状态、重试状态的功能可能产生大量组合。

```
Complexity = Σ NodeCost + λ·Σ CrossBoundaryEdgeCost + μ·log(StateSpace) + ν·SharedMutableState
```

跨项目、跨团队、跨数据库的边权应**明显高于**项目内部边权。设计原则：

- 减少共享可变状态
- 减少跨边界调用
- 减少隐式依赖
- 缩小接口表面积
- 限制状态组合

不是简单地减少文件数量或服务数量。

## 5. 防止"目标漂移"和代理指标劫持 🟡（仓库：assessor 需求完整性为雏形）

Agent 很容易完成表面目标却没完成真实目标。例如"优化页面性能"——Agent
可能通过减少数据展示、取消动画、延迟加载、隐藏复杂模块让指标变好，但
用户体验变差。任务模型必须分开：

```typescript
interface GoalModel {
  primaryGoal: string;
  userOutcome: string;
  successMetrics: Metric[];
  guardrailMetrics: Metric[];       // 护栏指标：优化时不得破坏
  forbiddenOptimizations: string[];
  negativeRequirements: string[];
}
```

例如：

```yaml
primary_goal: 提高订单页面首屏速度
guardrails:
  - 不减少用户可见数据
  - 不降低搜索准确性
  - 不取消必要的状态反馈
  - 不牺牲可访问性
```

**决策不能只依赖一个总分**，采用分层、字典序决策：

1. 安全、法律、权限和数据完整性
2. 业务正确性和验收标准
3. 用户目标和体验
4. 可维护性、性能、可扩展性
5. 时间、Token、金钱成本

低成本不能补偿错误结果，高性能也不能补偿越权。

## 6. Agent 必须具备因果思维，而不只是模式匹配 🔴

"接口慢 → 加缓存、数据库压力高 → 分库分表、页面卡顿 → Web Worker、
代码复杂 → 拆微服务"都可能是错误因果判断。应先形成因果假设：

```
现象 → 可能原因 → 可观测证据 → 最小干预实验 → 结果 → 更新因果模型
```

```typescript
interface CausalHypothesis {
  symptom: string;
  proposedCause: string;
  supportingEvidence: string[];
  contradictingEvidence: string[];
  intervention: string;
  expectedObservation: string;
  confidence: number;
  status: "unverified" | "supported" | "rejected" | "inconclusive";
}
```

页面卡顿应先测量：主线程执行时间、DOM 数量、网络延迟、序列化耗时、
重复渲染、内存回收——之后才决定用虚拟滚动、缓存、Web Worker、分页还是
请求合并。

## 7. 不确定性必须分类型处理 🔴

不能把所有"不确定"用一个数字表示：

| 类型 | 含义 | 应对方式 |
|---|---|---|
| 认知不确定性 | 信息不足，不知道事实 | 搜索、探测、实验 |
| 随机不确定性 | 现实本身具有波动 | 容错、统计、冗余 |
| 对抗不确定性 | 有攻击者或恶意输入 | 威胁建模、最小权限、隔离 |

示例：不知道 API 是否存在 → 认知不确定，读代码即可；网络延迟波动 →
随机不确定，需要超时/重试/熔断；用户输入可能含 Prompt Injection →
对抗不确定，需要安全边界。

**Agent 的置信度必须经过校准**——不是语言越肯定置信度越高。应统计：
Agent 声称 90% 置信度的判断，实际有多少真正正确。否则置信度只是修辞。

## 8. 时间不只是耗时，还包括语义时间 🔴

四种时间：事件发生时间 Event Time、系统处理时间 Processing Time、业务
有效时间 Valid Time、记录入库时间 Transaction Time。

示例：用户在 10:00 有权限，10:05 权限被撤销，10:06 审批请求到达——按
哪个时间判断权限必须由业务规则定义。

```typescript
interface TemporalValidity {
  observedAt: string;
  validFrom?: string;
  validUntil?: string;
  freshnessClass: "static" | "slow_changing" | "volatile";
}
```

对于容易变化的信息，置信度需要**随时间衰减**。世界状态、规则、接口文档
和外部服务状态都不能被认为永久有效。

## 9. 必须根据副作用类型决定执行权限 🔴

| 副作用类型 | 示例 | 是否允许并行试跑 |
|---|---|---|
| Pure 纯计算 | 分析、生成方案 | 可以 |
| Read-only 只读 | 查询代码、读取数据库 | 通常可以 |
| Sandboxed 沙箱 | 临时分支、临时数据库 | 可以 |
| Reversible 可回滚 | 修改配置、有完整回滚 | 谨慎可以 |
| Compensatable 可补偿 | 发消息、异步任务 | 需要补偿机制 |
| Irreversible 不可逆 | 删除生产数据、外部付款 | 不允许投机执行 |

```
type EffectClass = "pure" | "read_only" | "sandboxed"
                 | "reversible" | "compensatable" | "irreversible";
```

**多路径并行只应该自由发生在纯计算、只读、沙箱阶段。多个 Agent 不能同时
对生产系统执行不同技术方案。**

## 10. 并行执行必须解决所有权和共享状态问题 🔴（仓库：campaign worktree 为雏形）

多 Agent 系统最容易出的问题不是推理能力，而是：两个 Agent 同时修改同一
文件、基于不同接口版本、一个覆盖另一个的迁移、重复发送同一消息。

**单写者原则**：同一时间，一个可变对象只能有一个明确写入者。

```typescript
interface ExecutionLease {
  resourceId: string;
  ownerAgentId: string;
  worldVersion: number;
  acquiredAt: string;
  expiresAt: string;
  mode: "read" | "write" | "exclusive";
}
```

对于代码多路径：不同 Git worktree、不同分支、不同容器、不同临时数据库；
最终通过明确的 Merge 节点合并，**而不是多个 Agent 直接写同一个工作区**。

## 11. 分布式失败必须作为一等公民 ✅（失败签名 + 重试 + 对账 + 演练）

不能只设计成功路径，至少要考虑：执行成功但结果没上报、结果上报成功但
状态没保存、Agent 崩溃后任务被重复执行、消息已发送但事务回滚、测试完成
但证据记录失败、父任务取消但子任务仍在运行。

```typescript
interface FailurePolicy {
  retryPolicy?: { maxAttempts: number; backoff: string };
  idempotencyKey?: string;
  compensationAction?: string;
  reconciliationAction?: string;
  orphanDetection?: boolean;
  timeout?: number;
}
```

**长期运行系统必须有 Reconciliation（对账）**：不要只相信事件流，定期
比较期望状态与实际状态，发现差异后修复。例如审批已成功但通知未发送，
应由对账任务发现，而不是永远依赖第一次消息必然成功。

## 12. 多 Agent 的"独立性"比数量更重要 🔴（仓库：meta 并发角色为雏形）

三个相同模型、相同 Prompt、相同上下文的 Agent 很可能产生相同错误——那
不是一个错误被验证三次，而是**一个错误被复制三次**。

```
Diversity = ModelDiversity + MethodDiversity + DataDiversity + OracleDiversity
          − SharedFailureCorrelation
```

真正独立性可来自：不同模型、不同推理方法、不同技术路径、不同测试数据、
不同工具、不同验证器、不同上下文切片、**盲审机制**。

高风险任务模式：

- Agent A：实现
- Agent B：不看 A 的解释，先检查代码和测试
- Agent C：使用独立契约测试验证

而不是"Agent A 写完后告诉 Agent B 为什么自己是对的，B 顺着理由复述"——
后者产生锚定和确认偏差。

## 13. 不能用多数投票代替验证 ✅（仓库：gate 裁决 + 确定性门禁已对齐）

两票对一票不代表正确。仲裁顺序：

```
形式化约束 > 确定性工具输出 > 运行测试 > 独立实验 > 独立 Reviewer
> 多数意见 > 执行 Agent 的自我评价
```

路径冲突时应问：哪条满足更多硬约束、哪条证据更强、哪条经过更真实环境
验证、哪条残余风险更小——**不能只统计 Agent 数量**。

## 14. 验证器本身也可能不可靠（测试判定器问题）🟡（仓库：tests/ + eval 为雏形）

测试通过不一定代表正确，因为测试可能写错。按任务类型组合验证方法：

| 任务类型 | 适合的验证方法 |
|---|---|
| 算法 | 示例测试、属性测试、模糊测试、复杂度测试 |
| 数据迁移 | 行数核对、校验和、双读比较、回滚测试 |
| 分布式流程 | 契约测试、重复执行、乱序、超时、故障注入 |
| 前端页面 | 视觉回归、可访问性、响应式、任务路径测试 |
| 权限系统 | 权限矩阵、负面测试、越权测试、审计检查 |
| AI/RAG | 固定评测集、引用检查、对抗输入、人工抽样 |
| 性能 | 真实负载、尾延迟、资源使用、退化测试 |

高风险系统不应只问"测试是否通过"，还应问：测试覆盖了什么、没覆盖什么、
测试环境和真实环境差多远、**测试判定器是否可信**。

## 15. 证据必须带环境、范围、独立性和新鲜度 🟡（仓库：evidence.py + completion 门禁）

```typescript
interface EvidenceRecord {
  id: string;
  claimId: string;
  source: "formal_check" | "compiler" | "test" | "runtime"
        | "benchmark" | "review" | "model_inference";
  environment: "mock" | "sandbox" | "staging" | "canary" | "production";
  result: "supports" | "refutes" | "inconclusive";
  independent: boolean;
  reliability: number;
  scope: string[];
  observedAt: string;
  validUntil?: string;
  artifactVersion: string;
}
```

Mock 测试通过只能证明 Mock 环境下的行为，**不能直接升级成"生产环境已经
安全"**。证据强度必须与适用范围一致。

## 16. 解决死锁、活锁和无限修复循环 🟡（仓库：gate_fix 轮次上限为雏形）

| 问题 | 示例 |
|---|---|
| 死锁 | Agent A 等 B 的接口，B 等 A 的数据模型 |
| 活锁 | A 不断调接口、B 不断调前端，任务不收敛 |
| 无限修复 | 测试失败 → 修改 → 另一测试失败 → 回滚 → 原测试再次失败 |

系统需要：循环检测、依赖环检测、最大重试次数、失败模式指纹、进展度量、
升级门槛、人工介入点。

```
Progress = AcceptanceCoverage + VerifiedClaims − CriticalOpenRisks − RegressionCount
```

连续多个周期没有实质进展，就不能继续重复同一策略，应：更换假设、更换
工具、更换技术路径、升级 Reviewer，或宣布当前约束下不可行。

## 17. 重新规划需要"迟滞机制"，防止频繁摇摆 🔴

阈值附近频繁切换会产生振荡（风险 0.49→L1、0.51→L2、0.49→L1…）。使用
**非对称阈值**：

```
L1 升级到 L2：风险 > 0.60
L2 降级到 L1：风险 < 0.40
```

中间区域保持当前模式。同时只有发生**重大状态变化**才重新规划：硬约束
改变、关键假设失效、新风险超阈值、关键路径被阻断、世界状态版本变化过大。
不是每获得一条日志就重新生成整个计划。

## 18. 安全模型使用能力授权，不依赖角色 🔴

Agent 权限绑定具体能力，而不是"你是后端 Agent 所以可以操作数据库"：

```typescript
interface CapabilityGrant {
  agentId: string;
  nodeId: string;
  actions: string[];
  resources: string[];
  environment: string;
  expiresAt: string;
  maxSideEffectClass: EffectClass;
}
```

**所有检索到的文档、网页、代码注释和 Issue 都视为不可信输入**——它们
可能包含恶意指令、过期规则、伪造操作步骤、泄露密钥的诱导。Prompt、
上下文和工具权限必须分层隔离，**不能让检索内容直接获得控制权**。

## 19. 上下文管理区分"不可压缩"和"可压缩" ✅（pinned 分层 + 摘要失效）

| 类型 | 处理方式 |
|---|---|
| Pinned 固定上下文 | 不允许被摘要丢失 |
| Retrievable 可恢复上下文 | 可以摘要，但保留来源指针 |
| Ephemeral 临时上下文 | 完成节点后可以丢弃 |

Pinned 通常包括：安全规则、用户硬约束、公共接口契约、关键验收标准、
禁止状态、当前世界版本。模型不能因为 Token 不足把"不得修改公共 API"
摘要丢失。**摘要必须有失效机制**——被摘要的代码或文档变化时，摘要也要
失效并重新生成。

## 20. 真值维护系统，防止旧假设继续污染后续决策 🔴

Agent 的结论经常依赖其他结论：假设 API 已支持分页 → 决定前端只做接入 →
不设计新后端接口。后来发现 API 不支持分页，依赖该假设的整个计划都应被
撤回。

```typescript
interface DerivedClaim {
  id: string;
  statement: string;
  premiseIds: string[];
  confidence: number;
  status: "active" | "invalidated" | "superseded";
}
```

前提失效时：自动失效依赖结论、自动标记相关节点需要重新规划、自动撤销
受影响的方案评分。**这比简单保存一份聊天历史可靠得多。**

## 21. 规则必须能够过期、回滚和删除 🟡（仓库：learn.py draft→批准→注册为雏形）

很多规则系统最终失败，不是因为规则太少，而是**规则只增加、不删除**。
每条规则必须有：来源、适用领域、适用版本、触发条件、成功率、误报率、
最后验证时间、过期时间、替代规则。

```
规则生命周期：观察 → 候选经验 → 影子规则 → 软规则 → 组织策略
            → 强制门禁 → 废弃
```

学习系统不能只奖励"任务成功"，还要记录：是否只是运气成功、过程中是否
发生近失事件（near-miss）、是否绕过了必要验证、是否产生长期技术债、
是否造成后续维护成本。**一次成功不能证明策略正确；一次没有事故也不能
证明过程安全。**

## 22. 模型、Prompt、规则和工具都要版本化 🔴（仓库：fingerprint + DecisionCapsule 雏形）

能够重放一次 Agent 决策，至少需要记录：

```
原始 Prompt / 任务原子 / 上下文快照 / 代码版本 / 数据结构版本
模型名称和版本 / System Prompt 版本 / 规则集版本 / 工具版本
候选方案 / 实际执行路径 / 测试和运行证据 / 资源消耗 / 最终结果
```

```typescript
interface DecisionCapsule {
  taskId: string;
  promptHash: string;
  contextSnapshotId: string;
  worldVersion: number;
  modelVersions: Record<string, string>;
  ruleSetVersion: string;
  toolVersions: Record<string, string>;
  selectedStrategy: string;
  rejectedStrategies: string[];
  evidenceIds: string[];
  resourceUsage: ResourceLedger;
  finalStatus: string;
}
```

用于：事故分析、结果复现、规则回归、模型升级评估、责任追踪、策略对比。

## 23. 调度算法：关键路径、解锁价值和信息收益 🟡（仓库：campaign 方向评分 + meta 排序）

节点优先级不应只看风险或耗时：

```
Priority(n) = w1·CriticalPath + w2·UnblockValue + w3·InformationGain
            + w4·RiskReduction + w5·DeadlineUrgency + w6·ContextReuse
            − w7·WriteConflict − w8·MergeCost − w9·ContextLoad
```

| 指标 | 含义 |
|---|---|
| CriticalPath | 是否位于总工期关键路径 |
| UnblockValue | 完成后能解锁多少其他节点 |
| InformationGain | 能减少多少关键不确定性 |
| RiskReduction | 能否提前暴露或降低重大风险 |
| ContextReuse | 当前 Agent 是否已具备相关上下文 |
| WriteConflict | 是否会和其他任务产生写冲突 |
| MergeCost | 多路径结果合并难度 |

资源开关关闭时，时间/Token/金钱不能降低方案质量，但**仍可通过调度优化**
（减少空闲、利用安全并行、复用上下文、优先关键路径）——这改变执行方式，
不改变技术正确性。

## 24. Agent 分配优化"上下文连续性"，而不是平均分工 🔴

平均分配通常不是最佳方案：一个 Agent 连续完成"分析领域模型 → 设计接口 →
实现后端 → 修复测试"，往往比四个 Agent 各做一步更高效（后者需要大量
上下文同步）。

```
Fit(a, n) = SkillFit + ContextReuse + ToolAvailability + IndependenceBenefit
          − CurrentLoad − ContextDuplication − MergeRisk
```

分区目标：最大化领域内聚、上下文复用、真正可并行部分；最小化跨 Agent
边、共享写入、最终合并成本。**不是让每个 Agent 拥有相同数量的节点。**

## 25. 多路径实现必须先证明"值得分叉" 🔴

开启多个方案需同时满足：存在重要不确定性、方案确有技术差异、可在沙箱
安全比较、共享统一验收标准、失败路径可提前淘汰、路径间足够独立、比较
成本低于错误决策成本。

**不值得开三条路径**：已有成熟项目模式、需求明确、技术路线没有真实分歧、
多路径最终都修改相同核心代码、验证成本比实现成本更高。

> 多路径不是展示创造力，而是解决重大不确定性。

## 26. 人类参与避免"审批疲劳" 🟡（仓库：gate 裁决 + 决策日志为雏形）

不是风险高就什么都问人——每步都要批准，人会机械点击"同意"。按风险和
不可逆性分级：

| 情况 | 人类参与方式 |
|---|---|
| 低风险可回滚 | 自动执行，事后报告 |
| 中风险 | 执行前展示计划与差异 |
| 高风险可模拟 | 先 Dry-run，再批准真实执行 |
| 不可逆或重大影响 | 双人复核或独立审批 |
| 异常偏离 | 自动暂停并升级 |

向用户提问也要计算价值：

```
QuestionValue = ExpectedDecisionChange × Impact − InterruptionCost
```

只有一个问题可能显著改变高影响决策时，才值得打断用户。

## 27. 必须定义"完成"，否则 Agent 永远可以继续优化 🔴

完成 ≠ "Agent 没有更多想法"，而是：

- 所有硬约束满足
- 所有验收标准有证据支持
- 关键不变量未被破坏
- 没有未处理的严重风险
- 必要回滚方案可用
- 残余风险在允许范围内

```typescript
interface StopPolicy {
  acceptanceCoverageRequired: number;
  maxCriticalOpenRisks: number;
  maxResidualRisk: number;
  requireRollbackReady: boolean;
  requireIndependentEvidence: boolean;
  maxNoProgressCycles: number;
  resourceCapMode: "ignore" | "soft" | "hard";
}
```

系统也要承认：已完成、部分完成、已验证但存在残余风险、当前约束下不可行、
预算不足以达到质量底线、需要外部决策。**不能把所有结果都包装成"完成"。**

## 28. 形式化方法用于小内核，而不是整个世界 ✅（仓库：确定性门禁 + LLM 分层已对齐）

不可能仅靠通用推理对任意复杂程序和环境提供完整正确性证明。工程策略是
**缩小证明范围**：

- 对可信内核：类型系统、契约、状态机、模型检查、不变量、形式化协议
- 对外围复杂系统：测试、模拟、故障注入、运行时监控、灰度、回滚

哥德尔不完备、停机问题等理论的真正工程启示：不要把 Agent 的内部推理当
作万能证明器；必须定义有限边界；必须依靠外部证据；必须允许运行时纠错。

## 29. 未知的未知不能靠更多规划解决 🔴

Dry-run 主要解决已知未知项，但系统会遇到从未预想过的问题。需要：异常
检测、影子运行、灰度发布、Canary、故障注入、红队测试、运行时不变量、
自动回滚、Kill Switch。

一个方案所有测试都通过，也应先小流量运行、观察实际分布、检查异常指标、
再扩大范围——**因为测试只能覆盖已想到的情况。**

## 30. 非功能需求必须变成一等节点 🟡（仓库：production-readiness 强制关卡为雏形）

Agent 很容易只实现功能路径，忽略性能、安全、可访问性、可观测性、可运维
性、兼容性、容灾、数据保留、隐私。NFR 不应只放在文档最后，而应进入
任务图：

```
功能实现节点 + 性能验证节点 + 安全验证节点 + 可观察性节点 + 回滚演练节点
```

否则它们经常在最后被预算和时间挤掉。

## 31. 最终衡量指标不能只是成功率 🟡（仓库：eval + 回归测试为雏形）

| 类别 | 指标 |
|---|---|
| 结果质量 | 验收覆盖率、缺陷逃逸率、回归率 |
| 决策质量 | 置信度校准、假设失败率、方案后悔值 |
| 调度质量 | 关键路径效率、并行效率、Agent 空闲率 |
| 协作质量 | 合并冲突率、重复上下文率、返工率 |
| 资源质量 | 每个已验证结果的时间、Token 和成本 |

重点记录：第一次正确率、修复轮次、人工覆盖率、紧急停止次数、近失事件、
规则误触发率、规则漏触发率、估计与实际资源偏差。

> 真正重要的不是"一次回答花了多少 Token"，而是"**得到一个经过验证、
> 可运行、无重大风险的结果需要多少资源**"。

## 32. 必须避免的典型反模式

| 反模式 | 实际后果 |
|---|---|
| 所有任务都生成复杂图 | 规划成本超过任务本身 |
| 每个节点一个 Agent | 上下文复制和合并成本爆炸 |
| 多 Agent 多数投票 | 相关错误被误认为独立证据 |
| Agent 自己实现、自己测试、自己批准 | 确认偏差严重 |
| 规则只增加不废弃 | 规则冲突和决策僵化 |
| 所有指标加权成一个总分 | 严重风险被其他分数抵消 |
| Mock 通过等于生产可用 | 证据范围被夸大 |
| 无限重试同一方案 | 浪费资源且不增加信息 |
| 摘要压缩所有上下文 | 硬约束被悄然丢失 |
| 成功结果证明过程正确 | 忽略侥幸成功和近失风险 |
| 多路径直接完整开发 | 浪费时间、Token 和合并成本 |
| 计划生成后不再更新 | 世界状态变化后继续执行旧计划 |

## 33. 收敛规范：十五条

1. 默认使用最小可信内核，不把业务策略写死在调度器中。
2. 任务图和世界状态必须分离，并且都必须版本化。
3. 每个节点必须声明假设、保证、不变量、失败语义和副作用等级。
4. 复杂度评估重点放在共享状态、跨边界关系和状态组合上。
5. 决策必须先满足硬约束，再比较业务价值和工程质量，最后才考虑资源。
6. Agent 必须区分事实、推断、假设、未知和证据。
7. 重大技术决策必须先形成因果假设，再使用最小实验验证。
8. 多 Agent 只有在存在专业化、并行或独立验证收益时才启用。
9. 多 Agent 冗余必须具有独立性，不能把相同模型的重复意见当成多份证据。
10. 多路径只在重要不确定性存在时启用，并采用分阶段竞跑。
11. 只有纯计算、只读和沙箱节点可以自由投机并行执行。
12. 所有真实副作用必须具有所有权、幂等、回滚、补偿或对账机制。
13. 任务必须具备安全性、活性、终止条件和无进展检测。
14. 规则、模型、工具、证据和决策都必须可版本化、可重放、可废弃。
15. 系统学习只能调整软策略；硬规则升级必须经过评估、影子运行和治理。

## 34. 最终模型

```
MatureAgentSystem = SmallTrustedKernel + VersionedWorldState + DynamicTaskGraph
                  + BoundedAutonomy + IndependentEvidence
                  + FailureContainment + GovernedLearning
```

> **错误发生前有边界，错误发生时可观察，错误发生后可回滚，错误重复前
> 能学习，规则失效后能废弃，系统变化后能重新验证。**
>
> 不要追求一个无所不知、一次性规划正确的超级 Agent；要构建一个能够
> 有限理解、谨慎行动、独立验证、快速纠错、持续演化的计算系统。

## 35. 二十年工程视角的补充要点（本文档新增）

前文已覆盖大部分领域；以下是从分布式系统、形式化方法、系统可靠性
角度**还需要显式考虑**的要点：

### 35.1 分阶段竞跑 = Successive Halving，需要"早期信号可预测性" 🔴

§12（AADM-R）的"逐轮减半"正是多臂老虎机领域的 **Successive Halving /
Hyperband** 算法：每轮按当前信号淘汰一半，预算集中到幸存者。它成立
的前提是**早期信号与最终质量正相关**——如果第 0 轮架构草图不能预测
第 3 轮实现质量，逐轮减半会淘汰正确方案。落地要求：

- 淘汰必须基于**置信区间**而不是单点估计（信号噪声大时多给一轮）
- 每轮记录"被淘汰路径如果继续投入会怎样"的后悔值，校准淘汰判据
- 关键不确定性未消除前，允许"保留两条差异最大路径"而不是强制减半

### 35.2 租约必须配 Fencing Token，否则陈旧写入者造成 split-brain 🔴

ExecutionLease 过期后，持有旧租约的 Agent 可能仍在写。仅靠租约不足以防
止两个写入者并存（Chubby/ZooKeeper 的经典教训）。**写入端必须携带单调
递增的 fencing token，资源侧校验 token 最新才接受写入**。对仓库代码：
worktree/分支隔离 + 提交时校验世界版本（乐观并发 CAS），而不是信任
"我持有租约"。

### 35.3 世界状态必须是持久化事务存储（WAL + 快照），不是内存对象 🔴

State Store 崩溃时，租约、证据、世界版本都会丢失。要求：追加式 WAL +
定期快照，定义 RPO/RTO；崩溃恢复走**对账（reconciliation）**而不是
重放事件（事件可能丢失或乱序）。仓库的 `campaign_state.jsonl` /
memory 索引已是追加式，但缺少崩溃恢复语义。

### 35.4 回滚 vs 前向修复：先比较成本再决定 🟡

回滚不总是最优。代码变更通常**前向修复 + 回归测试**比回滚便宜且安全；
数据库 migration 需要可逆配对（up/down）但回滚窗口有限。决策规则：
比较 RollbackCost 与 ForwardFixCost + ResidualRisk，按副作用等级选择。
`gate_fix.py` 的自动修复循环本质上就是前向修复——应显式声明这条决策链。

### 35.5 错误预算（Error Budget）：给"治理强度"设预算 🟡

SRE 的错误预算思想可移植到治理层：为系统定义**治理预算**（如"每 N 个
任务允许 1 次未经人工门禁的高风险操作"或"允许的未覆盖验收比例"）。
预算耗尽 → 强制降级模式 / 强制人工门禁；预算富余 → 允许更激进的 L0/L1
自动执行。这同时防止两个极端：**永远 L3**（治理成本吞噬价值）和
**永远 L0**（风险敞口失控）。

### 35.6 波次 = BSP（Bulk Synchronous Parallel），必须处理 Straggler 🟡

波次调度本质是 BSP 同步屏障——最慢的 Agent 拖住整波。要求：每波设
超时；慢节点降级（拆小、并行化、或跳过并用后续对账补偿）；不要让整波
等待一个卡住的 Agent。仓库 runner 已有进程组硬超时，但缺"波内降级"。

### 35.7 LLM 非确定性：关键节点 N-version 或受约束解码 🟡

同一 Prompt + 同一版本模型，输出仍可能不同。对关键节点（契约设计、
门禁裁决文本解析）：要么运行多次取证据一致者（N-version），要么用
受约束解码（JSON schema、枚举裁决）把非确定性关进笼子。仓库的
`gate_fix.py` 结构化 JSON 解析已是后者；`--reuse` 指纹是前者雏形。

### 35.8 验证器的验证（meta-verification）✅

每个新门禁/验证器必须自带回归测试——仓库 `tests/` + eval 套件正是这个
机制（`make ci` 内自动执行）。新增验证器时必须同步扩展，否则"验证器
失效"会成为新的静默故障点。

### 35.9 时钟纪律 🔴

租约、validUntil、TTL、证据新鲜度全部依赖时钟。分布式下：使用单调时钟
（monotonic clock）测量间隔；互斥与过期判断不得用墙钟；对时钟偏差做
有界假设（如 Chubby 的 ε 偏差模型）。多 Agent 同机运行时可退化为进程
内锁，跨机时必须显式声明时钟模型。

### 35.10 公平性与饥饿 🔴

大 L3 流水线（多轮、多路径、多 Agent）不能饿死小任务。要求：优先级
队列 + 老化（aging）；波次调度器保证小任务在 L3 任务的两波之间有机会
执行；资源账本按任务记账，防止单个任务耗尽共享预算。仓库 `--round-delay`
/ `--min-interval` 是节流雏形，但无公平调度。

### 35.11 跨会话状态泄漏与隐私 🔴

世界状态、上下文索引、memory 跨任务共享时，必须按租户/项目/敏感级别
隔离（仓库 memory 已做脱敏与按需读取，但无显式租户边界）。检索到的
内容一律不可信（§18 能力授权 + 分层隔离）。

### 35.12 资源账本的计量精度 🟡

成本核算要防舍入漂移与计量丢失：按 inode/offset 增量折叠（仓库
memory 已采用）、幂等记账、账本与证据账本同源。预算硬上限应在 spawn
之前拒绝（仓库 ratelimit 已对齐），而不是事后追责。

## 36. 与仓库机制的映射

| AADM-G 概念 | 仓库现有实现 | 差距 |
|---|---|---|
| 六平面 | 语义（assessor/product）、世界状态（`pbatch/world.py`：快照+绑定+漂移→replan_required）、规划（pipeline/meta）、执行（runner）、证据（evidence.py + 验证器）、治理（registry/learn/决策日志） | ✅ |
| 可信内核 | pbatch 确定性模块 + Kill Switch（`devices halt` 一键冻结）+ State Store（world/truth/causality/error_budget JSONL 追加式）+ Lease/Fencing/CapabilityGrant（控制平面） | ✅ |
| 策略与机制分离 | 声明式配置（pi-batch.yaml）+ 确定性验证器 + 可插拔角色模板 | ✅ |
| 假设—保证契约 | `pbatch/contracts.py`：NodeContract（assumptions/guarantees/invariants/forbidden/failure_semantics）+ 组合检查（上游 guarantees ⊆ 下游 assumptions）+ 全图违规检测 + 语义自洽校验；TaskNode 已带契约字段 | ✅ |
| 失败签名 / 分布式失败 | 失败签名拒绝 + 重试退避 + runner 断线重连 + /reconcile 对账 + 失联任务重排队 + 失联演练（--drill-skip-heartbeats） | ✅ |
| 独立证据 | completion 报告门禁、被拒产物有界摘录、gate 独立裁决 | ✅ |
| 仲裁顺序 | VERDICT 裁决 + 确定性门禁优先于模型评价 | ✅ |
| 验证器可靠性 | tests/（387 回归）+ eval（25 用例）meta-验证 | ✅ |
| 规则生命周期 | learn draft → 例外（90 天复查）→ 影子模式（命中率裁决）→ 人工批准 → 注册表 | ✅ |
| 版本化重放 | `pbatch/capsule.py` + `pi-batch capsule`：prompt 哈希/世界版本/规则集摘要/工具版本/capsule_diff；fingerprint + 决策日志并存 | ✅ |
| 迟滞机制 | profile 路由：升级阈值 0.60 / 降级阈值 0.40（中间区保持当前模式） | ✅ |
| 能力授权 | 无（验证器按输出路径而非能力授予） | 🔴 |
| 真值维护 | `pbatch/truth.py` + `pi-batch truth claim/invalidate/status`：前提失效级联（invalidated/superseded）、追加式日志回放、幂等 | ✅ |
| 人工门禁分级 | `approval_level`：auto_report < preview < dry_run_approve < double_review + `question_value` 提问价值 | ✅ |
| 完成定义 | completion 报告 + `governance_policy.should_stop`（验收覆盖/残余风险/回滚/独立证据/无进展→更换策略）+ StopPolicy 随 profile 输出 | ✅ |
| NFR 一等节点 | backend-specs/production-readiness 强制关卡（编码前） | ✅ |
| 非确定性控制 | gate_fix 结构化 JSON 解析（受约束解码）+ `pbatch/nversion.py`（N 次执行一致性判定，发散显式报告） | ✅ |
