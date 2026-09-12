# sverpweb 采购审批试点（首切片）通用 Workflow 契约 Intake v1
> 模式：架构 intake（非实现）；冻结首阶段合同与验收边界，不进行跨仓库改造。  
> 适用范围：`sverpweb-stable` 采购申请/审批首切片，`snaplink` / `aero-id` / `aero-vault` / `aero-im` / `snaplink-audit-governance` / `snaplink-console`。  
> 说明：本轮**不认定 sverpweb 为多租户系统**；仅保持其当前单租户边界，同时在合同中携带 `tenant_id + department_id` 进行服务端上下文校验。

---

## Actors and Trust Boundaries

### 人员角色
- **申请人（Applicant）**：发起与变更采购草稿，提交后只能读取和修改与自己草稿相关的内容。
- **审批人（Dept/Finance Approver）**：对指定审批任务作出决定；可由组织/职位映射到处理人。
- **平台管理员（Platform Admin）**：管理策略、流程定义、组织映射、角色策略；可触发补偿、回放与审计核验。
- **审计/合规（Auditor）**：只读审核时间线与治理事实，不做业务状态判断。

### 服务角色
- **`snaplink`（身份/租户/授权权威）**
  - Source of truth：身份、租户上下文、授权决策与撤销状态。
  - 不应决定业务领域事实、文件权限永久真值或审批执行结果。
- **`aero-id`（组织/部门权威）**
  - Source of truth：组织/部门 ID、职位/成员关系、组织层级快照（过渡可由 `sverpweb` 引用桥接，最终收敛到 `aero-id`）。
  - 人名不能作为稳定主键；必须使用 `tenant_id + department_id`/subject ID。
- **`sverpweb-stable`（采购业务 + 首个试点流程执行）**
  - Source of truth：采购申请业务状态、首切片流程运行控制与命令执行（不承载审计真值）。
  - 不应混写用户/ACL/audit 表。
- **`aero-vault`（资产/ACL）**
  - Source of truth：文件与对象、版本、共享/访问控制（资源归属）。
- **`aero-im`（通知）**
  - Source of truth：通知发送记录（delivery outcomes，失败重试/降级）。
  - 不作审批决策与最终状态确认。
- **`snaplink-audit-governance`（治理事实源）**
  - Source of truth：不可变审计、事件链、时间线、完整性校验。
  - 不能反写业务状态。
- **`snaplink-console`（管理视图）**
  - Source of truth：查询聚合：调用治理时间线与管理 API，仅用于人可见证据投影。

### 自动化/Agent 参与者
- **Workflow/定时器（Service automation）**：预算检查、逾期扫描、重试触发（不做审批决定）。
- **Agent（受约束能力主体）**：仅可做建议/低风险预处理（如材料完整性扫描），不能提交最终审批与高风险执行命令。

### 信任边界与失败边界（冻结）
- **授权：fail_closed**
- **审计关键事件：durable_async**（本地 outbox 先入账，异步持久投递）
- **通知：best_effort_async**
- **部门/组织映射：tenant_id + department_id 为约束主键基础**

---

## Entry and Preconditions

### 参与链路入口
- 入口：`sverpweb` 采购前端/API（保留现有 API 形态，不新增端点）
- 目标对象：`PurchaseRequest`（现有表/状态机不扩展结构）
- 业务流程：先冻结合同、再对齐现有状态机行为（`draft -> pending_approval -> approved/rejected`）与新契约映射关系

### 前置条件（Preconditions）
- `snaplink` 的身份与租户上下文通过服务端会话/Token 校验通过。
- `aero-id` 提供的 `department_id` 与 `tenant_id` 可解析且与上下文一致（禁止前端字段篡改）。
- 请求内含幂等键（`idempotency_key`）与执行上下文（`operation_id/correlation_id`）。
- 所需审批路径配置可由流程定义解析（单部门/双部门策略清晰存在）。
- `snaplink-audit-governance` 入账链路已注册（即使下游暂时慢，仍需本地 durable outbox）。
- `aero-im` 可用性降级不阻塞核心流程（best effort）。

### 预期源真值与审计证据（入口）
- **Source of Truth**
  - 业务：`sverpweb` 采购业务记录 + 现有申请状态
  - 授权/租户：`snaplink`
  - 组织：`aero-id`
- **Expected Audit Evidence**
  - `request.accepted`（或 `workflow.instance.created`）包含 `operation_id/correlation_id/tenant_id/idempotency_key`
  - `permission.denied`（若授权失败）或 `request.denied`

---

## Purchase Request Happy Path

> 示例：普通金额、单部门后审批通过的标准场景

| 步骤 | 执行者 | 发生事件与状态 | Source of Truth | 期望审计证据 |
|---|---|---|---|---|
| 1. 创建/维护草稿 | 申请人（人） | 业务对象创建/更新；`PurchaseRequest` 草稿状态 | `sverpweb`（采购业务） | 可选业务审计事件（若已有），`audit` 可记录 `request.state.changed`（非强制） |
| 2. 提交申请 | 申请人（人） | 创建/更新 `ProcessInstance` 状态 `pending_approval`；创建首个 `WorkItem`（department manager） | `sverpweb` workflow slice | `wf.instance.created` + `wf.workitem.created` + `wf.state.changed(draft→pending_approval)` |
| 3. 部门审批 | 部门审批人（人） | `WorkItem` 由 `pending` → `approved` | `sverpweb`（WorkItem/ApprovalDecision） | `approval.submitted` + `approval.approved` + 权限快照 |
| 4. 执行前资格确认 | `sverpweb` 服务 | 确认流程策略与组织/权限快照仍有效；创建 `Execution` | `sverpweb` | `execution.planned` + `wf.state.changed(pending_approval→approved)` |
| 5. 执行业务确认命令 | `sverpweb`（服务） | 触发领域命令（如采购确认/下单）并写入业务状态 | 业务侧为 `sverpweb`；命令边界在 `Execution` | `execution.started` + `execution.completed` |
| 6. 完成/可见 | 申请人（人） | 流程可见“审批通过且已执行” | `sverpweb`（业务状态） + `Execution` | `wf.execution.succeeded` + `state.changed`；治理时间线应可重建执行先后关系 |

### 边界强调（该场景中的关键区分）
- `approved`：来自 `ApprovalDecision`，仅表示已获批准资格。
- `executed`：来自 `Execution` 完成+领域命令成功，代表业务效果落地。
- 审计事实是治理证据，不作为业务状态来源。

---

## Department and Finance Approval Variants

### 变体 A：金额低/低风险（仅部门审批）
- 步骤：提交 → 部门审批通过 → 执行触发
- 适用：金额与风险在部门阈值内
- **Source of Truth**
  - 部门决策：`WorkItem + ApprovalDecision`
  - 执行资格：`ProcessInstance` 判定 `approved`
- **Expected Audit Evidence**
  - `wf.workitem.created(type=department_approval)`
  - `approval.approved`（department）
  - `wf.state.changed(pending_approval→approved)`
  - `execution.started/completed`

### 变体 B：金额高/高风险（部门 + 财务）
- 步骤：提交 → 部门审批 → 财务审批 → 执行触发
- 规则：财务审批是强制追加步骤；`approved` 需两者都通过后才生成最终执行令牌。
- **Source of Truth**
  - 审批路由：`ProcessDefinition`（策略） + `WorkItem`（两条待办）
  - 最终批准：`ApprovalDecision` 集合（全量通过）
- **Expected Audit Evidence**
  - `wf.workitem.created(type=finance_approval)`
  - `approval.approved`（dept）
  - `approval.approved`（finance）
  - `wf.state.changed(pending_approval→approved)`（仅第二位通过后）
  - `execution.started/completed`

### 变体 C：拒绝/退回
- 步骤：任一审批人执行 `rejected` 或 `returned`
- 结果：进入失败终态（不执行；`rejected` 或 `returned`）并关闭待办
- **Source of Truth**
  - 决策状态：`ApprovalDecision` 明确记录
  - 流程状态：`ProcessInstance`（rejected/closed）
- **Expected Audit Evidence**
  - `approval.rejected` 或 `approval.returned`
  - `wf.state.changed(pending_approval→rejected)`
  - `wf.workitem.cancelled`（其余待办）

---

## Automation and Agent Variants

### Service Automation 变体（低风险）
- **预算/规则预检**：在提交后/待执行前运行，只做结构化检查，不替代审批。
- **SLA 监控与升级**：过期自动触发 `workitem.expired` 与升降级行为（如通知主管/财务）
- **Source of Truth**：`Execution`/`WorkItem` 的调度与计时信息由 `sverpweb` 流程层持有
- **Expected Audit Evidence**：
  - `operation.started`（timer/service_task）
  - `wf.workitem.expired`
  - `wf.state.changed(pending_approval→expired/rejected)`（策略所定）

### Agent 变体（受限）
- **Agent 建议任务**：自动检查附件完整性、重复提交风险、字段一致性，产出建议，不写业务主状态。
- **Agent 执行任务（仅低风险）**：可在最终批准前执行“建议性动作”，必须产生不可否认建议记录。
- **Source of Truth**：
  - `WorkItem` 类型必须为 `agent_task`
  - `ApprovalDecision.actor_type=agent` 只允许在 `suggestion`/`assist` 模式（不生成最终批准）
- **Expected Audit Evidence**
  - `agent.task.completed` + `actor=agent` + `evidence_ref`（建议或校验产物）
  - 必须有 `actor_scope` 与 `grant_id`（短期/限定资源）

---

## Shared Asset and Audit Side Scenario

### 场景：采购附件上传与审批上下文联动
1. 申请人上传附件 → 调用 `aero-vault`（单一资产权威） -> 得到 `asset_id`
2. `sverpweb` 在采购申请记录保存 `asset_id` 引用（不存完整资产真值）
3. 审批通过后执行时进行资产访问策略更新（如共享/查看范围）
4. 最终完成后如有撤销，执行对应资产访问撤销

### Source of Truth
- 文件/ACL 真值：`aero-vault`
- 组织/部门策略：`aero-id`
- 流程与审批真值：`sverpweb` Workflow 实体 + `snaplink-audit-governance` 的治理事件

### Expected Audit Evidence（全链）
- `asset.created / asset.granted / asset.revoked`（来自治理层/统一审计记录）
- `wf.state.changed(pending_approval→approved/...)`
- `execution.started/completed`（审批完成触发执行时）
- `snaplink -> sverpweb -> audit-governance -> aero-im -> console` 验证链路：  
  - `sverpweb` 持久化 outbox 事件成功入库（本地）
  - 异步转发至 `snaplink-audit-governance`
  - 通知由 `aero-im` best effort 发送（可失败重试，不阻塞核心状态）

---

## Failure, Timeout, Rejection and Retry Paths

### A. 鉴权失败（fail_closed）
- 触发：Token/会话失效、租户上下文不一致、组织成员关系不满足
- **Source of Truth**：`snaplink` + `aero-id`
- **Expected Audit Evidence**：
  - `request.denied`
  - `permission.denied`（含拒绝原因码）

### B. 审批超时/逾期
- 触发：`WorkItem` 在到期前未处理
- 处理：`workitem` 进入 `expired`，流程按策略走拒绝或重新路由
- **Source of Truth**：`WorkItem` + `ProcessInstance`
- **Expected Audit Evidence**：
  - `wf.workitem.expired`
  - `wf.state.changed(pending_approval→expired/rejected)`
  - 任何自动升级通知应有 `notification.attempted`（`aero-im`）

### C. 审批拒绝
- 触发：任一关键审批人拒绝
- 处理：终止执行，不进入 `approved`；后续只允许重提新流程
- **Source of Truth**：`ApprovalDecision` 为拒绝记录；`ProcessInstance` 终态 `rejected`
- **Expected Audit Evidence**：
  - `approval.rejected`
  - `wf.state.changed(pending_approval→rejected)`
  - `wf.workitem.completed`

### D. 执行失败与重试
- 触发：业务命令执行失败（暂态）
- 处理：`Execution` 允许受控重试；每次重试带幂等键与执行序列
- **Source of Truth**：`Execution`（command result + retry_count）与业务服务回写
- **Expected Audit Evidence**：
  - `execution.failed`（含 `error_signature`）
  - `execution.retry`（含 attempt + backoff id）
  - 最终 `execution.completed` 或 `execution.aborted`（终态）

### E. 审计 outbox 发送失败
- 处理：本地 durable_async 入库后返回可落盘，不丢记录；核心流程可继续（高风险动作可标记待确认）
- **Source of Truth**：本地 audit outbox + `snaplink-audit-governance` 投递状态
- **Expected Audit Evidence**：
  - 本地 outbox 记录（持久）
  - `ledgered` 前不可将关键事件误示为“最终审计完成”
  - 失败重放记录（retry_count）可追溯

### F. 通知失败
- 处理：best effort 重试+丢弃不阻塞核心
- **Source of Truth**：`aero-im` delivery status
- **Expected Audit Evidence**：
  - `notify.failed` 可选；不得作为流程成功判定依据

---

## Revocation, Compensation and Recovery

### 撤销（Revocation）
- **撤销前置权力**（用户/权限）：`snaplink` 立即生效，后续请求必须按新上下文拒绝
- **组织变更**（岗位/部门）：`aero-id` 更新后导致后续授权重新评估
- **Source of Truth**：`snaplink`、`aero-id`
- **Expected Audit Evidence**：
  - `permission.revoked`
  - `capability.revoked`
  - 与流程关联的 `workitem.cancelled`（如涉及待办）

### 取消（Cancellation）与补偿（Compensation）区分
- **Cancellation**（取消）：
  - 未执行或执行前中止：`ProcessInstance` = `cancelled`
  - Source of Truth：`ProcessInstance`
  - Audit：`wf.cancelled`（不产生 Compensation）
- **Compensation**（补偿）：
  - 执行已发生且操作可逆：生成 `Compensation` 记录与 `compensation_task`
  - Source of Truth：`Execution` + `Compensation`
  - Audit：`compensation.requested`/`compensation.started`/`compensation.succeeded|failed`
- 不可逆操作：标记 `non_compensable=true`，记录 `restore.requested` 由运营介入，禁止伪造已补偿

### Recovery（恢复）
- 执行失败后若可重放：使用 `operation_id/correlation_id` 进行幂等重入
- 若补偿失败：人工干预，保留全部审计链证据，不允许状态猜测
- **Expected Audit Evidence**：
  - `recovery.requested`
  - `recovery.approved`（如需）
  - `execution.replayed`（若允许）
  - `wf.state.changed(executing→failed/recovered)`

---

## User-visible Progress and Operator Evidence

### 用户可见进度（仅展示业务与流程进度）
- 草稿
- 待提交
- 待审批
- 部门审批中 / 财务审批中
- 已批准（待执行）
- 执行中
- 已执行（完成）
- 已拒绝 / 已取消 / 已过期
- 已补偿 / 补偿中

### 关键规则
- UI 显示 `已批准` 不代表已执行。
- 用户可见执行完成需对应 `execution.completed` 与业务执行状态可见性。
- 取消与补偿需明确区分：取消=未执行，补偿=已执行后的恢复动作。

### 可见证据（用户侧）
- 审批结果显示必须基于 `WorkItem/ApprovalDecision` 与 `Execution` 最后状态。
- 任何“通过/失败”文案必须有来源（不可仅依赖前端本地文本）。

### 运维/管理员证据（`snaplink-console`）
- 决策时间线：`snaplink-audit-governance` 事件序列 + 关键字段（tenant_id、workflow_instance_id、actor、outcome）
- 监控（Telemetry）：
  - 可见：延迟、失败率、重试率、超时率、队列积压
  - 非决定性：Trace/日志/指标不用于业务真值断言，只用于故障定位
- Operator evidence checklist（每次验收必须可检）：
  1. 同一 `workflow_instance_id` 的 `approve/reject` 与 `execute` 在时间线上可追溯
  2. `approved` 与 `executed` 两事件分离可见
  3. 审计链与业务链可通过 `operation_id` 对齐

---

## Tenant Isolation Scenarios

> 注意：本阶段不进行 sverpweb 多租户改造，仅冻结面向未来的 tenant-aware 契约字段与校验。

### Scenario T1：前端传入租户字段篡改
- 防护：服务端忽略客户端租户字段，使用 `snaplink` 上下文+`aero-id` 校验
- Source of Truth：`snaplink` / `aero-id`
- Audit：`request.denied`（reason: tenant_mismatch）

### Scenario T2：同名部门冲突
- 防护：按 `tenant_id + department_id` 解析，不允许名字作为稳定主键；名称仅做展示
- Source of Truth：`aero-id`
- Audit：`workflow.invalid_reference` + `operation.denied`（若映射失败）

### Scenario T3：单租户实例中的租户切换串扰（回归测试）
- 防护：即便当前是单租户实例，也必须固定实例级 tenant 上下文；跨域读写必须显式授权
- Source of Truth：`snaplink` 上下文
- Audit：`tenant.context.checked` + 审批/执行事件中 `tenant_id` 与上下文一致性比对

### Scenario T4：跨租户查询/配置误读（管理侧）
- 防护：Console 与任何管理查询需显式 tenant-filter 与管理员授权
- Source of Truth：`snaplink`（权限） + `snaplink-audit-governance`（审计）
- Audit：`permission.denied` 或有权限时仅返回该 tenant 的时间线与证据

---

## First Slice Acceptance Gates（冻结核验口）

1. Workflow 与业务域状态未混写；`approved` 与 `executed` 分离审计。
2. 任一业务状态写入者唯一权威：采购写入在 `sverpweb`，审批权威来自审批角色决策，授权来自 `snaplink`。
3. 审计来源不作为业务数据库状态源。
4. tenant/dept 由服务端上下文校验；前端文本字段无权改写权限语义。
5. 关键事件使用 durable_async outbox；通知为 best_effort_async；权限路径 fail_closed。
