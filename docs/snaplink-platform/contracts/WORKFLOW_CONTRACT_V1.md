# Generic Workflow Contract v1

> **状态：DRAFT / PROPOSED**  
> **契约版本：`workflow.v1`**  
> **首个试点：`sverpweb-stable` 的采购申请审批**

## 1. Purpose

本契约提供企业通用的申请、审批、执行、超时、撤销、恢复和补偿模型，适用于：

- 采购申请；
- 报销、付款和预算申请；
- 文件/媒体共享或公开发布申请；
- 权限申请；
- 用户入职和账号变更；
- 质量处置、库存调整和其他需要审批的业务动作。

它只负责**流程编排和决策记录**，不取代采购、库存、文件或账户领域的业务聚合。

## 2. Non-goals

本契约不定义：

- 任意业务领域的全部字段；
- 共享数据库或统一 ORM Entity；
- 任意脚本执行引擎；
- 由审计账本恢复业务状态；
- 由 Agent 自主改变审批策略；
- 一个涵盖所有业务的万能 status 字段；
- 让 `approved` 自动等价于 `executed`。

## 3. Terminology

| 名称 | 语义 |
|---|---|
| Business Aggregate | 业务领域的真实事实，例如 PurchaseRequest |
| ProcessDefinition | 可版本化的流程定义 |
| ProcessInstance | 某个业务聚合运行的一次流程 |
| WorkItem | 一个等待人、服务、Agent 或定时器处理的节点 |
| ApprovalDecision | 有授权依据的审批决定 |
| Execution | 对业务域 typed command 的实际执行 |
| Compensation | 已产生副作用后的冲正/恢复动作 |
| Operation | 一次跨服务可关联的操作 |
| Initiator | 原始发起者 |
| Executor | 实际执行主体 |
| Approver | 产生审批决定的主体 |

## 4. Common execution context

每个流程命令和事件必须携带或能服务端派生：

```yaml
execution_context:
  request_id: string
  operation_id: string
  correlation_id: string
  causation_id: string|null
  trace_id: string
  span_id: string|null
  tenant_id: string
  principal:
    type: human|service|automation|agent|system
    id: string
  initiator:
    type: human|service|automation|agent|system
    id: string
  on_behalf_of: string|null
  capability_grant_id: string|null
  workflow_instance_id: string|null
  execution_run_id: string|null
  idempotency_key: string
  deadline: timestamp|null
  source_system: string
```

约束：

- `tenant_id` 必须由服务端身份、域名/client 绑定和 membership 上下文决定；
- `department_id` 必须与 `tenant_id` 一起解析；名称只作展示；
- `request_id` 是一次 HTTP/RPC 请求标识；`operation_id` 是一次完整业务操作标识；
- `causation_id` 指向直接触发事件；`correlation_id` 关联完整业务链；
- 异步 Worker、Agent 和插件不得丢弃这些关联字段。

## 5. ProcessDefinition

### 5.1 Contract

```yaml
process_definition:
  definition_id: string
  definition_name: string
  definition_version: integer
  domain: string
  owner_service: string
  status: draft|active|deprecated|retired
  nodes: []
  transitions: []
  approval_policy_refs: []
  failure_policies: {}
  input_schema_ref: string
  output_schema_ref: string|null
  created_at: timestamp
  created_by: string
```

### 5.2 Node types

第一版只允许有限节点：

```text
start
human_task
approval
service_task
agent_task
timer
condition
parallel
join
compensation
end
```

节点必须有稳定 `node_id`。节点类型、输入 schema、输出 schema 和可调用命令必须明确声明。

### 5.3 Definition immutability

- `definition_id + definition_version` 唯一；
- 已有实例固定引用创建时的版本；
- 新规则创建新版本，不原地改变运行中流程；
- `retired` 定义不可创建新实例，但必须保留读取和历史回放能力；
- 定义修改必须通过配置变更审计并经过 schema 校验。

## 6. ProcessInstance

```yaml
process_instance:
  workflow_instance_id: string
  tenant_id: string
  definition_id: string
  definition_version: integer
  aggregate_type: string
  aggregate_id: string
  initiator_subject_id: string
  department_id: string|null
  status: draft|submitted|running|waiting|approved|execution_pending|executing|
          executed|completed|rejected|returned|cancelled|expired|failed|
          compensation_pending|compensated
  current_version: integer
  operation_id: string
  idempotency_key: string
  context_digest: string
  due_at: timestamp|null
  created_at: timestamp
  updated_at: timestamp
```

### 6.1 Status meaning

| 状态 | 语义 |
|---|---|
| `draft` | 申请尚未提交 |
| `submitted` | 已提交，等待流程路由 |
| `waiting` | 等待一个或多个 WorkItem |
| `approved` | 所需批准完成，尚未表示业务执行成功 |
| `execution_pending` | 已获得执行资格，等待命令执行 |
| `executing` | 业务命令正在执行 |
| `executed` | 业务命令已成功执行 |
| `completed` | 流程收尾完成 |
| `rejected` | 审批拒绝 |
| `returned` | 退回补充后重新提交 |
| `cancelled` | 流程被取消，通常尚未产生副作用 |
| `expired` | 超过 SLA 或策略期限 |
| `failed` | 执行或系统过程失败 |
| `compensation_pending` | 已发生或可能发生副作用，等待补偿或对账 |
| `compensated` | 补偿完成，不代表原始事实消失 |

### 6.2 State transition rules

- 只有定义中声明的 transition 可执行；
- 每次 transition 使用 `expected current_version`；
- 版本不匹配返回 `workflow.version_conflict`，不得覆盖新状态；
- `approved -> completed` 不允许直接跳转，必须经过 `Execution`；
- `rejected/cancelled/expired` 不得自动触发原业务执行；
- `compensated` 不能修改原始 `executed` 事实；
- 所有 transition 都产生不可变的 domain audit event。

## 7. WorkItem

```yaml
work_item:
  work_item_id: string
  workflow_instance_id: string
  node_id: string
  tenant_id: string
  kind: human|service|agent|timer
  candidate_scope:
    type: user|department|role|group|service|agent
    ids: []
  assignee_id: string|null
  status: pending|claimed|approved|rejected|returned|deferred|expired|cancelled|completed
  decision_deadline: timestamp|null
  claim_version: integer
  retry_count: integer
  execution_run_id: string|null
  created_at: timestamp
  updated_at: timestamp
```

规则：

- 候选人由审批策略和当前授权上下文解析；
- 任务 claim 使用 lease，避免两个主体同时处理；
- 权限撤销后未完成任务必须重新检查资格；
- WorkItem 关闭时不得留下有效的 Agent Grant；
- 超时、重分配和升级都是可审计的状态变化。

## 8. ApprovalDecision

```yaml
approval_decision:
  decision_id: string
  workflow_instance_id: string
  work_item_id: string
  tenant_id: string
  decision: approve|reject|return|defer|abstain
  actor_type: human|service|automation|agent
  actor_id: string
  initiator_id: string|null
  on_behalf_of: string|null
  authority_snapshot:
    tenant_id: string
    department_id: string|null
    roles: []
    permissions: []
    policy_id: string
    policy_version: string
    captured_at: timestamp
  reason_code: string
  reason_ref: string|null
  aggregate_version: integer
  decided_at: timestamp
  supersedes_decision_id: string|null
```

### 8.1 Approval rules

- 申请人默认不能批准自己发起的申请；
- Agent 默认不能产生最终高风险批准；
- 代理审批必须有 `on_behalf_of` 和有效 delegation；
- 审批权限在决定时验证，并记录 authority snapshot；
- 金额、风险、部门和租户条件由版本化 policy 决定；
- 申请关键字段改变后，旧决定必须标记 superseded 并重新审批；
- 决策记录不能删除，撤销使用新的反向或补偿决定。

### 8.2 Approval modes

```text
single       一个批准人
sequence     按顺序批准
all          全部候选人批准
any          任一候选人批准
quorum       达到最少人数
conditional  根据金额/风险增加节点
```

## 9. Execution

```yaml
execution:
  execution_id: string
  workflow_instance_id: string
  tenant_id: string
  command_name: string
  command_schema_version: string
  command_payload_ref: string
  actor_type: service|automation|agent
  actor_id: string
  capability_grant_id: string|null
  status: pending|running|succeeded|failed|aborted
  attempt_no: integer
  idempotency_key: string
  aggregate_version_before: integer
  aggregate_version_after: integer|null
  error_code: string|null
  started_at: timestamp|null
  completed_at: timestamp|null
```

执行要求：

- 只调用已声明的 typed domain command；
- 命令由业务域重新校验当前租户、权限、聚合版本和审批条件；
- 每次重试保持同一业务幂等键并增加 attempt；
- `succeeded` 必须有业务域确认结果；
- Worker 不能仅凭流程状态将业务聚合标记为完成。

## 10. Compensation

```yaml
compensation:
  compensation_id: string
  workflow_instance_id: string
  execution_id: string
  tenant_id: string
  type: rollback|reverse|manual_recovery
  reversible: boolean
  compensation_command: string|null
  status: queued|running|succeeded|failed|not_applicable
  trigger_reason: string
  requested_by: string
  approval_id: string|null
  started_at: timestamp|null
  completed_at: timestamp|null
```

补偿规则：

- 领域服务必须声明操作是否可逆；
- 已执行的副作用不能通过删除原始事件来回滚；
- 不可逆操作进入人工恢复、冲正或法律保留流程；
- 补偿命令必须独立幂等并受新的权限/审批校验；
- audit-governance replay 只能重建证据，不能直接执行补偿。

## 11. Policy and routing

流程引擎不硬编码所有业务规则。路由可引用策略：

```yaml
approval_policy_ref:
  policy_id: purchase.approval.route
  policy_version: "3"
  input:
    amount_minor: 100000
    currency_code: CNY
    department_id: dept-1
    risk_flags: []
```

策略输出必须是受 schema 约束的结构：

```yaml
route:
  required_steps:
    - department_approval
    - finance_approval
  quorum: 1
  separation_of_duties: true
  agent_mode: suggestion_only
  effective_until: timestamp
```

禁止以自然语言、任意脚本或 Agent 自由文本决定审批人和权限。

## 12. Purchase approval example

```text
PurchaseRequest draft
  → submit
ProcessInstance purchase-approval@v1
  → department WorkItem
  → optional finance WorkItem when policy threshold matches
  → ApprovalDecision collection complete
  → approved / execution_pending
  → purchase.request.confirm
  → Execution.succeeded
  → executed / completed
```

附件处理：

```text
user upload → aero-vault asset_id
sverpweb stores asset_id reference
approval/execution checks asset reference
ACL/share remains owned by aero-vault
```

## 13. Agent and automation

### 13.1 Agent task

Agent 只能作为 `WorkItem.kind=agent` 或受限 `Execution.actor_type=agent` 出现，并必须关联：

```text
agent_id
agent_version
initiator_id
on_behalf_of
capability_grant_id
workflow_instance_id
execution_run_id
resource_scope
action_scope
expiry
revocation_epoch
```

### 13.2 Automation task

Timer、重试、Outbox relay 和补偿调度器使用 `automation`/`service` actor，不冒充人类批准人。策略自动批准必须记录 policy ID、版本和输入摘要。

### 13.3 High-risk boundary

以下操作默认 `human_required` 或额外双人审批：

- 付款、发货和预算提交；
- 删除或恢复关键资产；
- 修改 ACL、角色和租户策略；
- 跨租户共享；
- 互联网公开发布；
- Agent 权限扩大。

## 14. Audit and telemetry

### 14.1 Audit events

```text
governance.workflow.created@v1
governance.workflow.submitted@v1
governance.workflow.status-changed@v1
governance.workitem.created@v1
governance.workitem.assigned@v1
governance.workitem.expired@v1
governance.approval.recorded@v1
governance.execution.started@v1
governance.execution.completed@v1
governance.execution.failed@v1
governance.compensation.requested@v1
governance.compensation.completed@v1
governance.capability.revoked@v1
```

事件必须包含 tenant、actor provenance、operation、aggregate/workflow version 和 outcome。

### 14.2 Telemetry

- 每个流程、WorkItem、命令和插件 RPC 有 OTel span；
- 指标记录时延、队列深度、重试、SLA、失败和撤销延迟；
- Trace/metrics/logs 不替代不可变审计事件；
- 原始 prompt、Token、密码和业务敏感正文不进入审计 payload。

## 15. Errors

统一错误至少包含：

```json
{
  "code": "workflow.version_conflict",
  "message": "workflow version changed",
  "trace_id": "...",
  "operation_id": "...",
  "details": {}
}
```

首批错误码：

```text
workflow.invalid_definition
workflow.definition_retired
workflow.invalid_transition
workflow.version_conflict
workflow.duplicate_request
workflow.work_item_not_assignable
workflow.approval_not_authorized
workflow.separation_of_duties_violation
workflow.capability_expired
workflow.capability_revoked
workflow.execution_not_approved
workflow.execution_failed
workflow.compensation_not_supported
workflow.tenant_mismatch
workflow.audit_pending
```

## 16. Persistence and migration boundary

新增持久化实现前，必须完成独立 Persistence Design Report，至少包括：

- API DTO / Domain Model / Persistence Model / Read Model 四模型分离；
- internal PK、business key、idempotency key 区分；
- 金额使用 minor unit + currency，不使用浮点；
- 状态全集和 transition constraint；
- `tenant_id`、唯一约束、查询驱动索引；
- claim lease、并发版本、历史和删除语义；
- expand → migrate/backfill → switch read → stop old write → contract；
- 备份、恢复和回滚窗口。

第一阶段不建立跨项目共享表；sverpweb 只新增自己拥有的 Workflow 表，并通过 typed API/事件与其他服务通信。

## 17. Compatibility

- API 旧状态保持读取兼容；
- 新增字段先 optional；
- 老客户端看到 `approved` 时不能推断 `executed`；
- 事件消费者必须幂等；
- 流程定义按版本固定；
- 旧流程定义退役后仍可查询和回放；
- 审计事件永不覆盖；更正/撤销通过新事件完成。

## 18. Acceptance criteria

1. 采购申请可提交并生成唯一 ProcessInstance；
2. 部门审批和金额触发的财务审批可按定义版本路由；
3. 申请人不能自审批，Agent 不能自审批或提权；
4. `approved`、`executing`、`executed`、`completed` 可独立查询；
5. 重复提交不会生成重复流程；
6. 版本冲突不会覆盖新状态；
7. 拒绝、退回、超时和取消不会触发未批准执行；
8. 执行失败可重试，已执行副作用按补偿契约处理；
9. 所有关键状态和决定能在 audit-governance timeline 中按 operation_id 查询；
10. Agent Grant 到期或撤销后不能继续执行；
11. tenant/dept 伪造和跨租户访问 fail closed；
12. minimal/full/production 的模块和依赖差异可由 inventory/lock 证明。
