# Project Studio：从业务讨论到可验证实现

## 目标

真实项目不是“一个需求 → 一条固定 Pipeline”。它是一个有依赖的设计图：

```text
业务问题/目标
  ├── 需求与验收
  ├── 产品范围与用户旅程
  ├── 领域模型与业务规则
  ├── UX/UI 信息架构与交互状态
  ├── 架构和模块边界
  ├── 数据与持久化
  ├── API/权限/审计
  └── 技术选型与 ADR
          ↓
       实施 DAG
          ↓
    代码/测试/发布/运营
```

`ai-batch-runner` 已经具备其中的大部分零件：`assess`、`profile`、`impact`、`org`、`context`、`adr`、`campaign`、`advance`、`eval`、`migrate` 和 `studio`。应把它们统一到一个**结构化项目设计账本**，而不是继续增加互相独立的 prompt。

## 1. 命令入口

```bash
pi-batch studio init --out docs/studio.yaml
pi-batch studio plan --project . --spec docs/studio.yaml --json
pi-batch studio validate --project . --spec docs/studio.yaml --strict
pi-batch studio rehearse --project . --spec docs/studio.yaml --budget-minutes 240 --json
```

这些命令默认只读；`init` 只在目标文件不存在时创建模板，`--out` 才会显式写报告。
设计 artifact 的生成仍交给经过批准的 pipeline，`studio` 负责结构、依赖、证据、状态和推进门禁。

## 2. 统一对象：Design Ledger

每个项目拥有一个版本化的设计账本。账本保存事实、提案、决策和验证结果，不把模型生成的 Markdown 当作完成证明。

```yaml
schema_version: studio/v1
project: aps
status: proposed

brief:
  goal: "让 PMC 从订单可靠生成并发布可执行生产计划"
  non_goals:
    - "不替代 ERP/MES/WMS/EAM/QA 的权威交易"
    - "未接入的生产源不推断为零"

artifacts:
  - id: requirements
    type: requirements
    status: proposed
    path: docs/design/requirements.json
  - id: domain
    type: domain_model
    status: proposed
    path: docs/design/domain.json
    depends_on: [requirements]
  - id: ux
    type: ux_spec
    status: proposed
    path: docs/design/ux.json
    depends_on: [requirements, product]
  - id: architecture
    type: architecture
    status: proposed
    path: docs/design/architecture.json
    depends_on: [domain, data]
  - id: api
    type: api_contract
    status: proposed
    path: docs/design/api.json
    depends_on: [domain, architecture]
  - id: implementation
    type: implementation_plan
    status: proposed
    path: docs/design/plan.json
    depends_on: [requirements, domain, ux, architecture, api]
```

每个 artifact 都必须有：

- `schema_version` 和 `artifact_type`；
- `status`：`proposed/approved/implemented/verified/blocked`；
- `evidence`：文件、符号、接口或业务访谈来源；
- `assumptions`：未经确认的假设及验证计划；
- `links`：指向上游需求、下游任务和验收；
- `acceptance`：可执行命令或明确的 Given/When/Then。

## 2. APS 的设计账本

### requirements

必须回答：

- 谁使用：集团计划员、工厂计划员、车间主管、工艺、采购、设备、执行员；
- 解决什么：订单分配、可执行性判断、排程、异常和交期复盘；
- 数据来自哪里：ERP、扫描、物料、采购、维护、质量、仓储、物流；
- 缺源时怎么显示：`unknown/partial/unavailable`，绝不当作 0；
- 验收如何判断：数量、终站、权限、版本、审计和失败路径。

### product

必须明确：

```text
L0：查询/局部安排
L1：计划和日排程
L2：多资源与异常重排
L3：多工厂、外发、跨系统产品化能力
```

并写清楚不做什么，防止把 APS 变成 ERP、MES、WMS、采购或容器优化系统。

### domain_model

核心对象：

```text
SalesOrderLine
ProductionDemand
SteelPlanAllocation
ProductionSegment
OperationInstance
ScheduleRun / ScheduleRevision
DailyArrangement
ProductionEvent
QualityRelease
StockReceipt
ShipmentEvent
ExceptionCase
```

核心不变量：

- 分配量不能超过订单可生产剩余量；
- 生产事实不能被普通计划编辑覆盖；
- 终站完成、质量放行、入库和出货不能混为一谈；
- 拆批和外发必须保留父子关系；
- 顺流数量必须来自真实终站生产事实；
- 未知数据不能自动变成 0。

### ux_spec

每个页面必须声明：

- 目标用户和任务频率；
- 主信息、异常信息和次要信息；
- loading/empty/error/permission/conflict 状态；
- 读写边界；
- 批量操作的预检和确认；
- 详情下钻和审计入口。

APS 页面可拆成：

```text
钢板计划工作台
订单 360° 详情
工序表/Gantt
日计划/现场看板
异常中心
设置和版本发布
统计与复盘
```

### architecture/data/api

每个写 API 应声明：

```text
permission
resource_scope
csrf
version / If-Match
idempotency_key
reason
transaction_boundary
audit_event
```

每个外部读模型应声明：

```text
source_system
source_record_id
as_of
freshness
source_status
data_quality
```

### implementation_plan

计划不是线性列表，而是 DAG：

```text
P0 统一数量/状态/未知口径
P1 订单需求 → 钢板计划分配
P1 订单级工序实例和 phaseSchedule
P1 校验/发布/版本/审计
P2 物料、设备、外发和异常
P3 入库、出货、装柜
P4 多工厂、拆批、跨厂转运
```

## 3. 自动验证矩阵

Project Studio 不用一个总分掩盖硬错误，而是输出指标向量和硬门禁：

| 指标 | 含义 |
|---|---|
| requirement_coverage | 每个需求是否有领域、页面、API、任务和验收映射 |
| evidence_coverage | 结论是否有文件/符号/接口证据 |
| domain_invariant_coverage | 每条关键业务规则是否有测试或验证器 |
| state_transition_coverage | 状态转移是否有命令、权限、事件和回滚语义 |
| api_contract_coverage | API 是否有错误、分页、幂等、版本和权限 |
| ux_state_coverage | 页面是否覆盖加载、空态、失败、无权和冲突 |
| audit_coverage | 高风险写操作是否有审计字段和 outbox |
| acceptance_coverage | 验收项中实际执行并通过的比例 |
| plan_traceability | 实施任务是否能追溯到需求和验收 |
| critical_path | 依赖 DAG 的关键路径和预计时间 |
| unresolved_high_risk | 未解决的高风险假设数量 |

硬门禁包括：

```text
schema_invalid
missing_source_evidence
unresolved_high_risk_assumption
unmapped_requirement
permission_missing
audit_missing
state_transition_without_command
cyclic_plan
acceptance_not_executed
fabricated_pass
```

“设计完整度 90%”不能绕过这些门禁。

## 4. 自适应路由

不是所有项目都运行完整 17 节点：

| 模式 | 典型任务 | 需要的节点 |
|---|---|---|
| L0 | 单按钮/局部样式 | requirement → developer → testing |
| L1 | 普通功能 | requirement → product/domain → API/UI → developer → testing |
| L2 | 模块、权限、数据库或并发 | requirement → product → domain → architecture → data → API → UX → plan → implementation → review |
| L3 | APS、多工厂、迁移、生产数据 | 全部早期设计节点 + source comparison + 对抗审查 + 发布/运维/回滚演练 |

`assess/profile/impact` 决定起始模式；硬触发器（生产数据、删除、迁移、审计、跨项目）不能被低分覆盖。

## 5. 与 Pipeline、组合和 Campaign 的关系

```text
Design Ledger：保存系统认知和证据（`pi-batch studio`）
DAG：表达 artifact 和任务的依赖
Pipeline：执行一个经过批准的局部工作流
Campaign：发现并选择可实施方向
Migrate：比较 source/target、生成迁移差异
Advance：对目标项目做持续质量推进
Org：提供岗位规范和节点门禁
```

推荐执行方式：

1. `assess/profile/impact` 判断范围和风险；
2. `context/project/adr` 装载项目事实和既有决策；
3. 生成或更新 Design Ledger；
4. 确定性 schema/traceability/gate 验证；
5. `rehearse` 计算波次、预算、关键路径和阻塞；
6. 人工批准设计门；
7. 用 campaign/pipeline 实施一个 DAG 波次；
8. 用项目验证器和 acceptance gate 验证；
9. 更新 ADR、facts、migration ledger 和 advance 状态；
10. 将失败根因沉淀为规则或 eval。

## 6. 不把 AI 判断伪装成量化

可以量化的是：

- 文件、符号、接口和测试是否存在；
- 字段和关系是否满足 schema；
- DAG 是否有环；
- 需求到验收的映射覆盖率；
- 门禁执行和退出码；
- 变更文件、预计时间和关键路径；
- 未决假设和风险数量。

不能仅凭数字证明：

- 业务模型一定正确；
- UI 一定好用；
- 架构一定优雅；
- 源项目和目标项目语义完全等价。

这些需要有证据的角色审查、对抗审查和人工决策。数字是路由和停止信号，不是真理。
