# AADM：原子化自适应决策模型（Atomic Adaptive Decision Model）

> **定位**：本文件是把"产品思维、架构思维、领域建模、UI 几何、数据结构、
> 算法、安全、运维、多 Agent、验证、自我学习"统一收敛为**一个** Agent
> 决策模型的**设计契约**（理论层）。它与本仓库现有机制（`pbatch/`、
> `ui-specs/`、`backend-specs/`、`product-specs/`）是**规范与实现**的
> 关系：本文定义结构、公式与边界；实现状态在每节标注。
>
> **文档索引（四层模型）**：
>
> | 文档 | 层 | 内容 |
> |---|---|---|
> | 本文件 | 理论模型 | 认知原子、决策事务、超图、裁量包络、规则五层、多目标优化 |
> | `docs/AADM_RUNTIME.md` | 运行时内核 | 渐进式执行：L0-L3 分级、Dry-run、链→图、波次调度、分阶段竞跑、资源开关 |
> | `docs/AADM_GOVERNANCE.md` | 治理模型 | 六平面三闭环四保证、可信内核、世界状态、契约、对账、真值维护、版本化重放 |
> | `docs/AADM_DEVICE.md` | 设备织网 | 设备感知执行：ExecutionTarget 抽象、SSH/身份/能力探测、Placement 调度、Lease + Fencing、迁移与对账 |
>
> 四者关系：理论模型定义"完整形态"；运行时内核定义"默认形态"（从一条
> 链、一个 Agent、一个方案开始，按证据升级）；治理模型保证系统在错误、
> 冲突、规则失效时仍然可控；设备织网把执行平面从单机扩展到分布式
> （默认关闭，`device_fabric.enabled: false` 时行为与单机完全一致）。
>
> **状态图例**：✅ 已落地（仓库有对应实现）｜🟡 部分落地｜🔴 待实现
>
> **证据原则**：本文是提案级设计文档。按仓库证据标准（Verified/Partial/
> Missing/Proposed）核对后，落地项才提升为正式机制；未实现的公式与结构
> 不冒充已由工程门禁验证。

## 0. 与现有机制的关系（映射总表）

所有理念统一抽象为：

> **Agent = 约束求解器 + 多目标优化器 + 滚动反馈控制器 + 经验学习器**

它不是根据 Prompt 查一张"规则清单"然后机械执行，而是在一个受约束的
状态空间中持续完成：

```
理解状态 → 构造目标 → 激活规则 → 生成候选方案 → 排除非法方案
→ 选择当前最优行动 → 获取真实反馈 → 更新认知 → 重新规划
```

核心不是把所有东西做成固定流程，而是区分：

| 层级 | 含义 | 仓库现有对应 |
|---|---|---|
| 什么不可违反 | invariant / 宪法规则 | `backend-specs/architecture-constitution.md`、gate fail closed、失败签名拒绝落盘 ✅ |
| 什么默认应该遵循 | contract / policy | `rules.yaml` 三档处方、必选规则不可被 LLM 跳过 ✅ |
| 什么可以根据场景调整 | heuristic | 规则双向校验、assessor 克制处方（min 档）✅ |
| 什么可以由 Agent 主动创造 | suggestion / 裁量包络内自由 | meta ad-hoc 角色、低风险可回滚实验 🟡 |

### 概念 ↔ 实现状态总表

| AADM 概念 | 仓库现有实现 | 差距 / 落地项 |
|---|---|---|
| 认知原子 Atom（Type/Source/Confidence/Hardness/Scope/Time） | `pbatch/atomize.py`（确定性提取/来源/硬度/时间元数据/超图） | ✅ 原子类型、来源类型、置信度/硬度、四类时间字段与图导出有回归证据 |
| 决策事务 DT（Trigger/ReadSet/Goal/Guards/Options/Action/WriteSet/ExpectedEvidence/Verifier/Compensation） | `pbatch/transaction.py`（完整契约、guard/预算门、验证/回滚状态机、JSON 重放） | 🟡 事务内核与重放已落地；任意 Agent 调用自动包装仍由具体执行入口选择 |
| 组合算子（AND/OR/SEQUENCE/PARALLEL/GUARD/LOOP/VERIFY/COMPENSATE/ESCALATE/REFINE） | pipeline 阶段 DAG、`or_tasks`、gate_fix/max-rounds、assessor 路由 | ✅ 仓库支持的算子均有显式入口；未声明的算子仍 fail closed，不把普通 DAG 冒充全语义执行 |
| 类型化超图 | `pbatch/hypergraph.py` + `pi-batch graph` | ✅ `conflicts_with/guards/realizes`、环检测、级联失效与 JSON 校验均有实现 |
| 多维任务向量（12 维） | `pbatch/profile.py` + `assessor.py` | ✅ 12 维画像、归一化、L0-L3 路由与硬触发器输出结构化 JSON |
| 自主性向量 A（信息/规划/执行/学习） | `pbatch/profile.py` autonomy/探索度/迟滞路由 | ✅ 四维自主性与任务等级输出；具体策略仍受 gate/预算硬门约束 |
| 裁量包络 Discretion Envelope | `profile.py` `_discretion_envelope`、assess --json | ✅ allowed/forbidden/maxRisk/change budget/proofs/rollback/human gate 等字段结构化输出 |
| 五层规则（invariant/contract/policy/heuristic/suggestion） | `rules.yaml` level + `rule_matcher.py` | ✅ level、activation、required、suppress_on 与必选规则 veto 均显式校验 |
| 动态激活/抑制 | `rule_matcher` 三轴筛选、证据与有界 manifest | ✅ tier/page/risk/profile/system-type 路由及抑制理由可审计；上下文仍有预算上限 |
| 约束求解 + Pareto | `pbatch/pareto.py`、MUC/utility、proposal/diversity | ✅ 硬约束过滤、Pareto 前沿、Minimal Unsatisfied Core 与证据仲裁已有回归 |
| 假设记录 | `profile.py` `assumption_records`（product 推演链） | ✅ AssumptionRisk=(1-C)×I×(1-R)、处理决策表，并随 task profile 输出 |
| 滚动规划 | `execution_horizon` + `--max-rounds` 轮循环、advance 逐轮续跑 | ✅ Horizon 公式（风险/不确定性 → 步长收缩） |
| 规则晋升机制 | `learn.py` draft/exception/shadow/promote | ✅ RCA 前置、影子命中率、人工批准、归档与注册表合并均有门禁 |
| 软规则例外 | `learn exception` + `RuleException` schema | ✅ expectedBenefit/addedRisk/compensatingControls/scope/expiresAt 持久化，invariant/required 不可豁免 |
| 多 Agent 提案 | `meta.py` + `proposal.py` + diversity/decision | ✅ 标准化 proposal、冲突/影响面、独立性与字典序仲裁均有实现 |
| 混合架构（LLM↔确定性分工） | check-ui-spec / check-backend-quality / quality.py 确定性门禁、gate VERDICT fail closed、失败签名拒绝落盘 | ✅ 已完全对齐 |
| MUST/SHOULD/MAY 规范表达 | completion 报告门禁（禁止伪造通过）、失败零产物、gate fail closed | ✅ 已对齐 |

---

## 1. 统一模型

```
Prompt
  ↓
认知原子 Cognitive Atom
  ↓
交互事件 Interaction Event
  ↓
决策事务 Decision Transaction
  ↓
能力单元 Capability
  ↓
任务与决策图 Task Graph
  ↓
规则场 Rule Field
  ↓
裁量包络 Discretion Envelope
  ↓
计划、行动、验证
  ↓
证据反馈与规则学习
```

---

## 2. 最小语义单元：认知原子 ✅

认知原子是不可再拆分的单一命题。示例：

- "审批人可以拒绝文件申请" → 权限事实原子
- "审批完成后必须记录审计" → 硬约束原子
- "文件审批可能需要支持多级审批" → 假设原子

```
Atom = ⟨Type, Proposition, Source, Confidence, Hardness, Scope, Time⟩
```

| 字段 | 含义 |
|---|---|
| Type | 目标、事实、约束、偏好、未知、假设、风险、验收、证据 |
| Proposition | 原子的具体内容 |
| Source | 用户、代码、文档、运行日志、规则库、模型推断 |
| Confidence | 当前可信度，0～1 |
| Hardness | hard / required / preferred / advisory |
| Scope | 当前文件、模块、项目、系统、组织 |
| Time | 生效时间、过期时间、版本 |

至少定义以下原子类型：`GoalAtom` 目标、`ActorAtom` 角色、`ObjectAtom`
操作对象、`OperationAtom` 行为、`FactAtom` 已知事实、`ConstraintAtom`
约束、`PreferenceAtom` 偏好、`UnknownAtom` 未知项、`AssumptionAtom`
假设、`RiskAtom` 风险、`AcceptanceAtom` 验收条件、`EvidenceAtom` 证据、
`ObservationAtom` 执行观察、`DecisionAtom` 决策。

> **每个原子必须带来源**。否则 Agent 很容易把自己的推断当成用户给出的
> 事实。（仓库对应：`pbatch/atomize.py` 保留兼容的 `source=text`，并提供
> `source_type=user|code|document|runtime_log|rule|model`、稳定原子 id 与
> `time={event_time,processing_time,valid_from,valid_until,transaction_time,version}`。）

---

## 3. 最小行为单元：决策事务 🔴

单个原子不能产生行为。Agent 真正的最小交互单元是**可以完整闭环的决策
事务**：

```
触发 → 读取状态 → 理解目标 → 检查约束 → 产生候选行动 → 选择行动
→ 改变状态 → 获取证据 → 提交或回滚
```

```
DT = ⟨Trigger, ReadSet, Goal, Guards, Options, Action, WriteSet,
      ExpectedEvidence, Verifier, Compensation⟩
```

```typescript
interface DecisionTransaction {
  id: string;
  taskId: string;
  triggerAtomIds: string[];        // 为什么启动这个事务
  readSet: string[];               // 决策所读取的事实、状态和证据
  goalAtomId: string;              // 本次事务只允许有一个主要目标
  guardAtomIds: string[];          // 执行前必须成立的条件
  options: CandidateAction[];      // 候选行动
  selectedActionId?: string;       // 最终选中的行动
  writeSet: StateMutation[];       // 预期改变的状态
  expectedEvidence: ProofObligation[];  // 完成后必须获得什么证据
  verifier: VerifierSpec;          // 如何验证
  compensation?: CompensationSpec; // 失败如何回退
  budget: ExecutionBudget;         // 成本、时间、风险与权限预算
  status:
    | "draft" | "proposed" | "authorized" | "executing"
    | "observed" | "verified" | "failed" | "rolled_back";
}
```

**最小事务判定标准**（六条同时满足）：

1. 只有一个主要目标
2. 只有一个主要状态变化
3. 只有一个责任主体
4. 只有一个明确的完成条件
5. 可以独立验证
6. 可以独立重试或补偿

如果一个单元同时修改权限、创建文件、发送通知、记录审计、更新审批状态，
它**不是**一个最小事务，应拆成多个事务通过依赖关系组合。

---

## 4. 组合层级

```
认知原子
  ↓
交互事件
  ↓
决策事务
  ↓
能力单元
  ↓
工作流
  ↓
系统任务
  ↓
多 Agent 组织
```

### 4.1 交互事件 ✅（仓库：决策日志 / 事件系统部分）

所有用户、Agent、工具和系统之间的交互统一表示为事件，**不可变、追加式
记录**：

```typescript
interface InteractionEvent {
  id: string;
  taskId: string;
  transactionId?: string;
  actor: string;
  target: string;
  verb:
    | "request" | "propose" | "approve" | "execute"
    | "observe" | "verify" | "reject" | "rollback";
  objectType: string;
  objectId?: string;
  payload: unknown;
  correlationId: string;
  causationId?: string;
  source: "user" | "agent" | "tool" | "system";
  confidence: number;
  createdAt: string;
}
```

可以回答：是谁做出的决策、基于什么信息、使用了哪些规则、当时有哪些候选
方案、为什么选择当前方案、获得了什么验证结果。

### 4.2 能力单元 🟡（仓库：角色模板 / pipeline 阶段）

多个稳定的决策事务组合成一个能力：权限判断、文件上传、审批状态机、
审计记录、通知发送、数据导出。

```typescript
interface Capability {
  id: string;
  name: string;
  inputSchema: object;
  outputSchema: object;
  preconditions: string[];
  postconditions: string[];
  transactionTemplates: string[];
  permissions: string[];
  sideEffects: string[];
  failureModes: string[];
  observability: string[];
}
```

能力是 Agent 可复用的"积木"，而不是某一次任务中的临时代码。

---

## 5. 类型化超图 🔴

Agent 任务不是单纯树形结构——一个决策可能同时依赖用户目标、角色权限、
当前代码结构、数据库限制、安全规则、验收标准、多个系统契约。因此使用
**类型化属性超图 Typed Property Hypergraph**。

节点类型：原子、决策事务、能力、代码文件、数据库表、接口、服务、Agent、
规则、证据、风险。

关系类型：`contains` 包含、`depends_on` 依赖、`conflicts_with` 冲突、
`supports` 支持、`refines` 细化、`abstracts` 抽象、`guards` 约束、
`realizes` 实现、`verifies` 验证、`causes` 导致、`compensates` 补偿、
`alternative_to` 替代方案、`owned_by` 责任归属。

```typescript
interface GraphNode {
  id: string;
  type: "atom" | "transaction" | "capability" | "artifact" | "rule" | "agent" | "evidence";
  payload: unknown;
  version: number;
}

interface GraphRelation {
  id: string;
  from: string[];
  to: string[];
  type: "contains" | "depends_on" | "conflicts_with" | "supports"
      | "refines" | "guards" | "realizes" | "verifies" | "compensates"
      | "alternative_to" | "owned_by";
  condition?: string;
  weight?: number;
  confidence: number;
}
```

**为什么是超图而不是普通图**：一个决策通常需要多个输入共同成立：

```
用户具有审批权限
+ 文件处于待审批状态
+ 审批单没有被其他人处理
+ 审批意见合法
→ 允许执行审批事务
```

这不是单条边，而是多个条件共同约束一个状态变化。

---

## 6. 标准组合算子 🟡

| 组合 | 含义 | 示例 |
|---|---|---|
| AND | 所有子任务都要完成 | 写代码并通过测试 |
| OR | 从候选方案中选择一个 | 同步导出或异步导出 |
| SEQUENCE | 顺序执行 | 创建审批单后发送通知 |
| PARALLEL | 并行执行 | 同时检查权限和文件状态 |
| GUARD | 满足条件才执行 | 有权限才能审批 |
| LOOP | 根据反馈重复 | 测试失败后修复 |
| VERIFY | 行动后必须验证 | 更新数据后查询确认 |
| COMPENSATE | 失败时补偿 | 通知失败进入重试队列 |
| ESCALATE | 超过权限时升级 | 高风险变更请求人工审批 |
| REFINE | 抽象任务细化 | "增加审批"拆成状态机、接口、页面 |

简化表达：

```
A ⊗ B      A 和 B 都必须完成        （AND）
A ⊕ B      A 和 B 二选一            （OR）
A → B      A 完成后执行 B           （SEQUENCE）
A ∥ B      A、B 可以并行            （PARALLEL）
C ▷ A      条件 C 成立才执行 A      （GUARD）
A ↺ V      执行 A 后进入验证循环 V   （LOOP/VERIFY）
A ↯ B      A 失败时执行补偿 B       （COMPENSATE）
```

示例——文件审批工作流：

```
权限验证 → 读取文件和审批单 → 执行状态迁移 → (记录审计 ∥ 发送通知) → 端到端验证
```

若审计不可丢失、通知允许稍后重试，则进一步分解：

```
审批状态变更 ⊗ 审计事件持久化 → 事务提交 → 异步发送通知
```

---

## 7. 完整任务数据模型 🔴

一次 Prompt 处理过程保存为统一任务模型：

```typescript
interface TaskModel {
  id: string;
  rawPrompt: string;
  atoms: CognitiveAtom[];
  relations: GraphRelation[];
  transactions: DecisionTransaction[];
  capabilities: string[];
  profile: TaskProfile;
  activeRules: ActivatedRule[];
  suppressedRules: SuppressedRule[];
  candidates: CandidatePlan[];
  selectedPlan?: CandidatePlan;
  discretionEnvelope: DiscretionEnvelope;
  evidenceLedger: EvidenceRecord[];
  assumptions: AssumptionRecord[];
  eventLog: InteractionEvent[];
  status: "compiling" | "planning" | "executing" | "verifying"
        | "completed" | "blocked" | "failed";
}
```

---

## 8. 多维任务画像（而不是单一复杂度分数）🟡

单一分数会隐藏大量信息：两个任务都可能是 70 分复杂度——一个是 UI 很复杂
但容易回滚；另一个只有一行数据库操作但可能造成生产数据丢失。因此使用
多维任务向量：

```
M = [Clarity, Scope, Coupling, Risk, Uncertainty, Reversibility,
     Novelty, Testability, Evidence, Observability, Value, TimePressure]
```

| 指标 | 含义 |
|---|---|
| Clarity | Prompt 是否明确 |
| Scope | 涉及多少层、模块、项目、角色 |
| Coupling | 跨边界依赖程度 |
| Risk | 错误造成的后果 |
| Uncertainty | 未知和冲突程度 |
| Reversibility | 是否容易回滚 |
| Novelty | 是否缺少已有模式 |
| Testability | 是否容易验证 |
| Evidence | 当前证据覆盖率 |
| Observability | 行动结果是否容易观察 |
| Value | 对用户目标的贡献 |
| TimePressure | 时间约束 |

所有值标准化到 0～1。

---

## 9. 指标计算 ✅（`pbatch/formulas.py`，纯确定性程序）

### 9.1 不确定性（按 Criticality 加权，低影响文案不确定 ≠ 数据库迁移不确定）

```
U = Σᵢ [ Criticalityᵢ × (1 − Confidenceᵢ) ] / Σᵢ Criticalityᵢ
```

### 9.2 风险（每项）

```
Riskᵢ = Severityᵢ × Probabilityᵢ × BlastRadiusᵢ × Irreversibilityᵢ × (1 − Detectabilityᵢ)
```

整体风险**不能简单取平均**（一个严重风险会被大量低风险稀释）：

```
Risk = 0.7 × max(Riskᵢ) + 0.3 × WeightedMean(Riskᵢ)
```

### 9.3 复杂度

```
Complexity = 0.20·Scope + 0.20·Coupling + 0.15·StateComplexity
           + 0.15·Uncertainty + 0.15·Novelty + 0.15·ChangeSurface
```

ChangeSurface：此次修改会触及多少现有对象。

### 9.4 当前置信度

```
Confidence = 0.35·EvidenceCoverage + 0.20·PromptClarity
           + 0.15·Consistency + 0.15·Testability + 0.15·KnownPrecedent
```

已存在的同类实现、测试结果和运行证据可以提高置信度；模型"觉得应该如此"
不能显著提高置信度。

---

## 10. 量化"主观能动性"：自主性向量 ✅

不应只有 `autonomy = true / false`，而是四种自主性：

| 维度 | 含义 |
|---|---|
| 信息自主性 A_information | 能否主动搜索、阅读、调查 |
| 规划自主性 A_planning | 能否提出替代方案和调整计划 |
| 执行自主性 A_execution | 能否直接修改和执行 |
| 学习自主性 A_learning | 能否修改长期规则和经验 |

```
A = [A_information, A_planning, A_execution, A_learning]
```

示例——高风险生产数据库任务：

```yaml
autonomy:
  information: 0.95   # 可以充分调查
  planning: 0.85      # 可以提出多个方案
  execution: 0.20     # 不能自由执行不可逆操作
  learning: 0.10      # 不能因一次任务就自动修改全局硬规则
```

示例——低风险 UI 调整：

```yaml
autonomy:
  information: 0.70
  planning: 0.65
  execution: 0.90
  learning: 0.30
```

---

## 11. 裁量包络（代替固定脚本）🟡

规则引擎不应告诉 Agent"必须按照第 1、2、3、4 步执行"，而应输出一个
**裁量包络 Discretion Envelope**——规定 Agent 能自由活动的范围：

```typescript
interface DiscretionEnvelope {
  allowedActionTypes: string[];
  forbiddenActionTypes: string[];
  allowedScopes: string[];
  maxRisk: number;
  maxCost: number;
  maxChangeSurface: number;
  executionAutonomy: number;
  planningAutonomy: number;
  requiredProofs: ProofObligation[];
  rollbackRequired: boolean;
  humanGateRequired: boolean;
  maxParallelism: number;
  maxPlanningHorizon: number;
  stopConditions: string[];
}
```

示例：

```yaml
allowed:
  - 修改当前前端模块
  - 增加测试
  - 调整组件结构
forbidden:
  - 修改公共 API
  - 修改数据库结构
  - 引入新的状态管理框架
max_change_surface: 0.15
rollback_required: true
required_proofs:
  - typecheck_pass
  - unit_test_pass
  - visual_regression_checked
```

包络内部 Agent 可自由决定：修改哪个组件、使用哪种局部抽象、如何组织
函数、选择哪种测试方式、是否顺手修复直接相关的小问题。这就是
**受约束的主观能动性**——不是无限自由，也不是机械脚本。

---

## 12. 规则分五层 🟡

| 层 | 性质 | 示例 | 仓库对应 |
|---|---|---|---|
| L1 宪法规则 invariant | **不可违反，不参与权重竞争，违反即淘汰方案** | 不得伪造验证结果、不得越权访问、不得破坏数据完整性、不得绕过安全边界、不得把推断冒充事实 | architecture-constitution、gate fail closed ✅ |
| L2 用户与业务契约 contract | 当前任务的硬边界 | 必须兼容 SQL Server 2012、不能增加新的基础设施、必须保留现有 API | 任务级约束 ✅ |
| L3 组织策略 policy | 默认强制，可通过正式例外流程处理 | 数据库修改必须有 migration、公共接口修改必须有版本策略、权限变更必须有安全测试 | rules.yaml 必选规则 ✅ |
| L4 工程启发式 heuristic | 非绝对，可被更高价值方案覆盖 | 优先最小修改、优先已有组件、避免上帝文件、高内聚低耦合 | 门禁启发式（uiquality/backendquality）✅ |
| L5 创造性建议 suggestion | 保留 Agent 创造性 | 重新组织页面信息层级、提出更符合用户操作路径的交互 | meta ad-hoc 角色 ✅ |

---

## 13. 规则数据结构 🟡（与 learn.py Rule Schema 对齐）

```typescript
interface Rule {
  id: string;
  name: string;
  level: "invariant" | "contract" | "policy" | "heuristic" | "suggestion";
  scope: string[];
  trigger: RulePredicate;
  guard?: RulePredicate;
  effect: {
    require?: string[];
    prefer?: string[];
    prohibit?: string[];
    activateLenses?: string[];
    suppressLenses?: string[];
    addProofObligations?: string[];
    changeAutonomy?: Partial<AutonomyVector>;
  };
  priority: number;
  weight: number;
  confidence: number;
  specificity: number;
  exceptions?: ExceptionPolicy;
  evidenceRefs: string[];
  version: number;
  validFrom?: string;
  validUntil?: string;
}
```

规则必须具有：触发条件、适用范围、规强度、来源、版本、例外条件、验证
要求。不能只是"尽量使用最佳实践"——这种规则无法执行，也无法验证。

---

## 14. 动态激活规则，而不是全部加载 🟡

每条规则计算激活度：

```
Activationᵣ = SemanticMatchᵣ × TriggerTruthᵣ × ScopeMatchᵣ
             × SourceTrustᵣ × Specificityᵣ × Freshnessᵣ
```

- **硬规则只要触发就必须加载**。
- 软规则按注意力价值选择最有价值的一部分进入当前上下文：

```
AttentionValueᵣ = Activationᵣ × ExpectedImpactᵣ / (ReasoningCostᵣ + ε)
```

避免一个简单按钮调整同时激活 DDD、分布式事务、事件溯源、布隆过滤器、
微服务拆分、多租户计费。

**优秀规则系统必须同时具备激活机制与抑制机制**：

```yaml
rule: local_ui_change
activate:
  - geometry_alignment
  - design_system
  - accessibility
  - responsive_layout
suppress:
  - database_migration
  - distributed_transaction
  - service_split
  - event_sourcing
```

---

## 15. 约束求解和多目标优化选择方案 🔴

不应直接选择第一个想到的方案。对非简单任务，先生成有限数量的差异化方案
（如：A 最小变更 / B 遵循现有工程模式 / C 解决根因的结构性方案），然后
分三步选择。

### 第一步：硬约束过滤

```
Feasible = { p | p 满足全部硬约束 }
```

违反以下条件的方案直接淘汰：权限边界、数据完整性、明确业务要求、公共
契约、法律和安全限制、不可接受的破坏性影响。

**如果没有可行方案，系统不应编造结果**，而应返回最小冲突集合
Minimal Unsatisfied Core。例如：要求 A"不得修改公共 API"与要求 B"必须
改变公共 API 返回结构"无法同时满足——系统应显式指出。

### 第二步：构造 Pareto 前沿

对剩余方案评价：目标匹配度、用户价值、实施风险、复杂度、可维护性、
可测试性、可回滚性、迁移成本、运行成本。先保留没有被其他方案全面压制
的 Pareto 方案，**不依赖单一总分**。

### 第三步：根据当前任务动态选择

```
Utility(p) = w_g·GoalFit + w_v·Value + w_e·EvidenceGain + w_t·Testability
           + w_r·Reversibility − w_k·Risk − w_c·Complexity − w_m·MigrationCost
```

权重不固定：高风险任务提高风险与可回滚权重；探索任务提高信息增益权重；
紧急修复提高交付速度权重；长期平台能力提高可维护性与兼容性权重。

> **最终原则：在满足目标的方案中，优先选择最低充分复杂度，而不是理论上
> 最复杂、最先进的方案。**

---

## 16. 规划探索和行动探索必须分开 🔴

### 规划探索度（思考可以大胆）

```
T_plan = Clamp(0.45·Uncertainty + 0.35·Novelty + 0.20·OptionDiversity)
```

### 行动探索度（执行必须保守）

```
T_action = T_plan × (1 − Risk) × Reversibility × Observability
```

| 场景 | 策略 |
|---|---|
| 高不确定 + 低风险 + 容易回滚 | 可以边试边改 |
| 高不确定 + 高风险 + 难以回滚 | 可以大胆思考，但必须保守执行 |
| 低不确定 + 高风险 | 按已验证流程执行 |
| 低不确定 + 低风险 | 快速直接执行 |

这比简单地设置一个模型 temperature 更合理。

---

## 17. 未知项不应该都变成"向用户提问" 🟡

每个未知项建立假设记录：

```typescript
interface AssumptionRecord {
  id: string;
  statement: string;
  confidence: number;
  impactIfWrong: number;
  reversibility: number;
  verificationCost: number;
  chosenDefault?: string;
  verificationPlan?: string;
  status: "unverified" | "accepted" | "verified" | "rejected";
}
```

```
AssumptionRisk = (1 − Confidence) × ImpactIfWrong × (1 − Reversibility)
```

| 假设风险 | 处理方式 |
|---|---|
| 很低 | 使用合理默认值，并记录 |
| 较低 | 使用可回滚方案，同时验证 |
| 中等 | 先查看代码、文档、日志或运行测试 |
| 较高 | 先模拟、实验或请求明确确认 |
| 极高 | 阻止不可逆执行 |

这样 Agent 不会因为一个低价值未知项频繁打断用户，也不会擅自猜测高风险
事项。

---

## 18. 滚动规划（而不是一次性写死全部步骤）🟡

传统方式（生成完整计划 → 从头执行到尾）的问题：中间状态与预期不一致时
后续计划全部失效。更合理的是**滚动时域控制**：

```
制定当前可见范围内的计划 → 执行一小批可验证动作 → 获取状态
→ 更新任务图 → 重新计算指标 → 调整下一批行动
```

```
x_t = [GoalProgress, Risk, Uncertainty, EvidenceCoverage, Resource, Time]   // 当前状态
u_t = [Research, Plan, Execute, Verify, Rollback, Escalate]                 // 可选行动
x_{t+1} = F(x_t, u_t, Observation_{t+1})                                    // 状态变化

min Σ_t ( GoalGap_t + λ_r·Risk_t + λ_c·Cost_t + λ_u·Uncertainty_t )        // 优化目标
```

执行批次大小动态计算：

```
Horizon = Clamp( H_max × Confidence × Reversibility / (Risk + Uncertainty + ε) )
```

| 情形 | 结果 |
|---|---|
| 风险高、不确定性高 | 每次只执行很小一步 |
| 证据充分、容易回滚 | 可以批量执行 |
| 实际结果偏离预期 | 立即缩短规划范围 |

---

## 19. 完整算法：processPrompt 🟡

```python
def processPrompt(prompt, context) -> TaskResult:
    # 1. Prompt 编译
    atoms = compilePromptToAtoms(prompt, context)

    # 2. 构造任务超图
    graph = buildTaskHypergraph(atoms, context)

    # 3. 识别未知、冲突、依赖和影响范围
    resolveReferences(graph)
    detectConflicts(graph)
    createUnknownAtoms(graph)
    calculateImpactSurface(graph)

    # 4. 多维量化
    profile = measureTaskProfile(graph)

    # 5. 动态激活规则
    rules = activateRules(graph, profile)
    hardRules = rules.filter(isHardRule)
    softRules = rules.filter(isSoftRule)

    # 6. 生成裁量包络
    envelope = deriveDiscretionEnvelope(graph, profile, hardRules, softRules)

    # 7. 产生差异化候选方案（最少 3 个）
    candidates = generateCandidatePlans(graph, profile, envelope, minimumDiversity=3)

    # 8. 硬约束求解
    feasible = [c for c in candidates if satisfiesAll(c, hardRules)]
    if not feasible:
        return {status: "blocked",
                unsatisfiedCore: findMinimalUnsatisfiedCore(candidates, hardRules)}

    # 9. 多目标 Pareto 筛选
    frontier = calculateParetoFrontier(feasible,
        ["goalFit", "value", "risk", "complexity",
         "testability", "reversibility", "migrationCost"])

    # 10. 选择最低充分复杂度方案
    plan = selectPlan(frontier, profile, softRules,
                      planningTemperature=calculatePlanningTemperature(profile))

    # 11. 给每个行动挂上验证义务和回滚方案
    plan = attachProofObligations(plan)
    plan = attachCompensationActions(plan)

    # 12. 滚动执行
    while not acceptanceSatisfied(graph):
        horizon = calculateExecutionHorizon(profile)
        transactions = selectNextTransactions(plan, horizon, envelope)
        for tx in transactions:
            authorizeTransaction(tx, envelope)
            result = executeTransaction(tx)
            evidence = verifyTransaction(tx, result)
            appendEvidence(graph, evidence)
            updateTaskState(graph, result, evidence)
            if not evidence.passed:
                compensateOrRollback(tx, result)
        newProfile = measureTaskProfile(graph)
        if significantDeviation(profile, newProfile):
            plan = replan(graph, newProfile, rules)
            envelope = deriveDiscretionEnvelope(graph, newProfile, hardRules, softRules)
        profile = newProfile

    # 13. 经验学习
    updateExperienceBase(graph)
    updateSoftRuleWeights(graph)
    return buildVerifiedResult(graph)
```

---

## 20. 混合架构：大模型 vs 确定性程序 ✅

| 工作 | 大模型 | 确定性程序 |
|---|---|---|
| 理解自然语言 Prompt | 是 | 辅助校验 |
| 发现隐含目标 | 是 | 否 |
| 生成候选方案 | 是 | 约束过滤 |
| 提出反例和假设 | 是 | 记录与量化 |
| 规则触发判断 | 可辅助 | 是 |
| 权限校验 | 否 | 是 |
| 状态机执行 | 否 | 是 |
| 数据迁移 | 生成建议 | 确定性执行 |
| 风险公式计算 | 否 | 是 |
| 约束冲突检测 | 可辅助 | 是 |
| 测试、编译、Lint | 否 | 是 |
| 运行结果验证 | 可解释 | 工具提供证据 |
| 规则权重更新 | 可建议 | 受控程序执行 |

概括：

> **大模型负责理解、联想、假设、创造和解释。规则引擎负责边界。
> 规划器负责选择。工具负责执行。验证器负责证明。
> 事件系统负责审计。治理系统负责长期规则变更。**

---

## 21. 规则不能因为一次成功就自动升级 🟡

经验学习需要**规则晋升机制**：

```
一次观察 → 候选经验 → 启发式规则 → 影子模式运行 → 软策略
→ 质量门禁 → 硬规则
```

示例：Agent 发现"大型导出使用同步接口容易超时"，不能立即变成"所有导出
都必须异步"。正确过程：

1. 记录失败案例；
2. 收集数据量、响应时间和环境；
3. 形成有范围的启发式规则；
4. 在后续任务中进行影子判断；
5. 验证命中率；
6. 确定阈值；
7. 再升级为组织策略。

软规则权重更新（η 为学习率）：

```
w_{t+1} = (1 − η)·w_t + η·OutcomeScore_t
```

**硬规则不允许 Agent 自主修改，必须通过治理流程。**

---

## 22. 允许 Agent 合理违反软规则 🔴

定义例外机制：

```typescript
interface RuleException {
  ruleId: string;
  taskId: string;
  reason: string;
  expectedBenefit: number;
  addedRisk: number;
  compensatingControls: string[];
  requiredEvidence: string[];
  scope: string[];
  expiresAt?: string;
  approvedBy?: string;
}
```

示例：默认规则"函数复杂度不得超过 10"，但某个性能关键解析器拆分后反而
产生大量对象分配和上下文切换。Agent 可以选择保留复杂度为 12 的函数，
但必须说明：为什么不拆、带来什么收益、增加什么风险、通过哪些测试补偿、
这个例外只适用于哪个函数。

> **规则有弹性，但例外必须可解释、可审计、可回收。**

---

## 23. 多 Agent 如何组合 🟡

多 Agent 不应自由聊天后凭感觉合并，每个 Agent 提交**标准化提案**：

```typescript
interface AgentProposal {
  agentId: string;
  transactionIds: string[];
  findings: string[];
  assumptions: AssumptionRecord[];
  proposedPlan: CandidatePlan;
  affectedContracts: string[];
  predictedMetrics: TaskProfile;
  risks: RiskAtom[];
  requiredEvidence: ProofObligation[];
  conflicts: string[];
}
```

协调器根据任务图处理：依赖关系、冲突关系、所有权、公共契约、状态版本、
证据完整性。

**是否拆分 Agent 的量化判断**：

```
MultiAgentBenefit = ParallelBenefit + SpecializationBenefit + IndependentReviewBenefit
                  − CoordinationCost − ContextSyncCost − MergeRisk
```

只有收益为正才拆分；否则使用一个 Agent 的多个内部视角更好。

---

## 24. 示例：文件审批 Prompt 🟡（assessor 风格的原子化示意）

Prompt：

> 在 snaplink-console 增加文件审批功能，审批后记录审计并通知申请人。

**1. 原子化**

```yaml
goals:
  - 增加文件审批能力
actors:
  - 申请人
  - 审批人
  - 管理员
objects:
  - 文件
  - 审批单
  - 审计事件
  - 通知
operations:
  - 创建审批
  - 通过
  - 拒绝
  - 撤回
  - 记录审计
  - 发送通知
affected_systems:
  - snaplink-console
  - snaplink
  - aero-id
  - aero-vault
  - snaplink-audit-governance
  - aero-im
unknowns:
  - 是否多级审批
  - 是否允许审批转交
  - 通知失败是否影响审批
  - 审批状态是否存储在 aero-vault
```

**2. 示例量化（示意值）**

```
scope: 0.86          coupling: 0.84        risk: 0.71
uncertainty: 0.42    reversibility: 0.58   testability: 0.76
evidence: 0.61       novelty: 0.37
```

自动得到：

```
execution_mode: architecture_change
autonomy:
  information: 0.90
  planning: 0.82
  execution: 0.43
  learning: 0.15
planning_exploration: 0.49
action_exploration: 0.09
required:
  - impact_map
  - approval_state_machine
  - permission_matrix
  - api_event_contract
  - idempotency_strategy
  - audit_integrity_test
  - notification_retry_test
  - rollback_plan
```

**3. 激活的规则**：产品流程、角色权限、状态机、跨项目契约、审计完整性、
消息可靠性、幂等、版本兼容、端到端验证。

**4. 被抑制的规则**（没有证据表明需要时）：数据库分库、微服务重构、
布隆过滤器、缓存体系改造、全新工作流引擎、全局前端架构重写。

这就是规则路由的价值。

---

## 25. 规范表达：MUST / SHOULD / MAY ✅（仓库门禁已覆盖主体）

### MUST（必须遵守）

- Prompt 必须先原子化，不能直接转代码。
- 所有事实、假设和证据必须区分。
- 硬约束必须先于方案评分执行。
- 每个决策事务必须有明确目标和可观察结果。
- 每个重要状态变化必须有验证义务。
- **未实际执行的验证不得声称通过。**
- 高风险行动必须有回滚、补偿或人工门禁。
- 所有长期规则必须有版本、范围和来源。
- 规则冲突必须显式暴露，不能静默忽略。

### SHOULD（默认遵守）

- 非简单任务应生成多个差异化候选方案。
- 应使用多维任务画像，而不是单一复杂度分数。
- 应动态激活和抑制规则。
- 应优先最低充分复杂度。
- 应采用滚动规划，而不是一次性长计划。
- 应优先可验证、可回滚的行动。
- 应把高影响未知项转化为调查或实验。
- 应保存决策、证据和实际结果。

### MAY（允许自主决定）

- 可以主动发现 Prompt 没有直接表达的用户问题。
- 可以挑战用户或旧架构中的隐含假设。
- 可以在低风险可回滚范围内进行实验。
- 可以在有充分理由和补偿控制时违反软规则。
- 可以根据任务动态调整规划深度和专业视角。
- 可以提出超出当前实现范围的长期建议，但不能擅自扩大当前交付范围。

---

## 26. 最终表达

```
AgentDecision = ConstraintSolve + MultiObjectiveOptimize
              + BoundedAutonomy + RollingFeedback + EvidenceLearning
```

更直白地说：

| 组件 | 负责什么 |
|---|---|
| 规则 | 确定不能做什么 |
| 任务目标 | 确定要达到什么 |
| 证据 | 确定当前知道什么 |
| 量化模型 | 确定风险、复杂度和自主空间 |
| Agent | 在可行空间内主动选择怎么做 |
| 工具 | 产生真实世界反馈 |
| 验证 | 判断是否真的完成 |
| 学习系统 | 让下一次决策更好 |

真正理想的规范不是把 Agent 变成流水线工人，而是：

> **把不可妥协的边界结构化，把可选择部分参数化，把创造性保留在裁量
> 包络内，再用真实证据持续修正它的判断。**

这样才能同时获得：规范性、一致性、可审计性、可验证性、灵活性、创造性、
适应性、主观能动性。

---

## 27. 落地路线（与仓库改造对应）

> 运行时内核与治理模型的落地批次分别见 `docs/AADM_RUNTIME.md` §28（映射）
> 与 `docs/AADM_GOVERNANCE.md` §35-36（补充要点与映射）。本表为理论层
> 批次，三份文档的批次可合并执行，统一受 `make ci` + eval 门禁约束。

按性价比排序的落地批次（每批带 eval 用例，`make ci` 门禁）：

| 批次 | 内容 | 对应章节 | 落点 | 状态 |
|---|---|---|---|---|
| P1 | 多维任务画像 + 自主性向量结构化 JSON（12 维向量、四维自主性、L0-L3 Load 路由 + 硬触发器 + 迟滞阈值、规划/行动探索度） | 8/9/10 + AADM-R §23 | `pbatch/profile.py` + `pi-batch profile`；assess --json/文本尾部附画像 | ✅ |
| P2 | 规则五层显式化 + 激活/抑制机制（level=invariant 永不跳过/激活度 1.0、suppress_on 显式抑制、必选规则不受抑制、schema 校验） | 12/13/14 | `pbatch/rule_matcher.py` + ui-specs/rules.yaml | ✅ |
| P3 | 裁量包络结构化输出（allowed/forbidden/maxRisk/max_change_surface/required_proofs/rollback/human_gate/parallelism/horizon） | 11 | profile.py `_discretion_envelope`；随 assess --json 输出 | ✅ |
| P4 | 假设记录 + 处理决策表（AssumptionRecord：风险=(1-C)×I×(1-R)；低→默认值/较低→可回滚/中→查代码/高→确认/极高→阻止不可逆） | 17 | profile.py `assumption_records`（源自 product 推演链） | ✅ |
| P5 | 软规则例外记录（RuleException：expectedBenefit/addedRisk/compensating/scope/默认 90 天复查；不豁免 invariant/必选） | 22 | `pi-batch learn exception` → docs/rules/exceptions/ | ✅ |
| P6 | 规则晋升闭环：draft → 例外（90 天复查）→ **影子命中率裁决** → 人工批准 → **`learn promote` 并入注册表**（RCA 前置/文件存在/重名拒绝 fail closed，草案存档 promoted/） | 21 | `pi-batch learn draft/exception/shadow/promote` | ✅ |
| P7 | 组合算子扩展（OR/LOOP/ESCALATE） | 6 | ✅ **OR 已实现**：stage `or_tasks: true`（候选任务按序执行，第一个通过验证器者胜出，全败 fail closed，schema 强制 tasks 列表）；LOOP=gate_fix_rounds/--max-rounds；ESCALATE=assessor L0-L3 路由 | ✅ |
| P8 | 类型化超图 | 5 | ✅ `pbatch/hypergraph.py` + `pi-batch graph`：节点（atom/transaction/capability/artifact/rule/agent/evidence）+ 超边（多输入共同约束）、结构校验 fail closed、guards 多条件求值、依赖环检测、冲突显式暴露、级联失效、JSON 持久化 | ✅ |
| P9 | 原子化/因果/时间语义收尾 | 2/6 + 治理 §6/§8 | ✅ `atomize`（MUST #1 字面落地 + 超图导出）；`causal`（因果假设生命周期）；`temporal`（证据衰减）；设备织网 Checkpoint 断点续传 | ✅ |
| P10 | Pareto/策略/估计/护栏/分叉/退役/预算收尾 | 15 + R §5/§21 + G §6/§26/§35 | ✅ `pareto`（前沿+Utility+MUC——AADM §15 唯一缺项）；`runtime_policy` 声明式配置（start_mode 下界）；`estimates` p50/p90 区间；`goal_model` 护栏指标；`should_fork` 分叉判据；`devices revoke` 退役；checkpoint digest 校验；`recovery`/`budget`（回滚决策+错误预算） | ✅ |

> 与仓库证据标准一致：AI 产出是提案，落地项须按证据标准核对后提升为
> 维护文档；未落地公式不冒充已验证。
