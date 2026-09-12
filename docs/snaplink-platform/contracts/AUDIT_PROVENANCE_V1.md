# Audit Provenance Contract v1

> **状态：DRAFT / PROPOSED**  
> **契约版本：`audit-provenance.v1`**  
> **接收方：`snaplink-audit-governance`**

## 1. Boundary

```text
业务服务：保存当前业务状态并产生事实
Audit Governance：保存不可变事实、关联链、完整性和合规证据
OTel：保存技术调用链、指标和运行诊断
```

Audit Governance 不负责替业务服务决定当前状态，也不通过审计回调修改业务数据库。

## 2. Actor model

```yaml
actor:
  type: human|service|automation|agent|system|anonymous
  id: string
  display_name: string|null
  tenant_id: string

initiator:
  type: human|service|automation|agent|system
  id: string

executor:
  type: human|service|automation|agent|system
  id: string

approver:
  type: human|service|automation|agent|system|null
  id: string|null

provenance:
  delegation_id: string|null
  capability_grant_id: string|null
  agent_id: string|null
  agent_version: string|null
  model_provider: string|null
  model_version: string|null
  tool_name: string|null
  policy_id: string|null
  policy_version: string|null
  approval_id: string|null
```

`actor` 表示发出当前事件的主体；`initiator` 表示最初发起者；`executor` 表示实际执行者。未发生委托时可以相同，但字段语义不能合并。

## 3. Required event envelope

```yaml
audit_event:
  event_id: string
  source_system: string
  event_type: string
  schema_id: string
  schema_version: integer
  occurred_at: timestamp
  received_at: timestamp|null
  tenant_id: string
  operation_id: string
  causation_id: string|null
  correlation_id: string|null
  trace_id: string|null
  span_id: string|null
  actor: Actor
  targets: []
  aggregate_type: string
  aggregate_id: string
  aggregate_version: integer|null
  action: string
  outcome: success|failure|denied|pending|cancelled|reversed
  reason: string|null
  changed_fields: []
  payload_digest: string|null
  payload_ref: string|null
  data_classification: public|internal|confidential|secret|regulated
  retention_class: string
  idempotency_key: string
  workflow_instance_id: string|null
  execution_run_id: string|null
```

当前事件正文遵循：

```text
事实进入 append-only ledger
→ hash chain
→ projection/index
→ WORM archive
```

## 4. Event naming

事件名格式：

```text
<domain>.<resource>.<action>@vN
```

首批 Workflow 事件：

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
vault.asset.linked@v1
vault.asset.accessed@v1
platform.operation.denied@v1
```

事件名是机器契约，不在名称中附加注释、租户、Actor 或自由文本。

## 5. Required audit coverage

### Security and identity

```text
identity.session.created
identity.session.revoked
identity.permission.granted
identity.permission.denied
identity.membership.changed
identity.tenant.context-denied
```

### Workflow

```text
governance.workflow.created
governance.workflow.submitted
governance.workflow.status-changed
governance.approval.recorded
governance.execution.started
governance.execution.completed
governance.execution.failed
governance.compensation.requested
governance.compensation.completed
```

### Assets

```text
vault.asset.created
vault.asset.version-created
vault.asset.acl-granted
vault.asset.acl-revoked
vault.asset.shared
vault.asset.published
vault.asset.unpublished
vault.asset.accessed
vault.asset.deleted
vault.asset.restored
```

具体系统应在事件注册表中声明哪些事件是高风险、强留存、Legal Hold eligible 或高频聚合。

## 6. Sensitive payload policy

允许记录：

- 资源 ID、类型、版本和安全 digest；
- before/after 的安全字段差异；
- 失败码、策略 ID、Grant ID 和审批 ID；
- `payload_ref` 指向受控、具备访问授权的外部证据。

禁止记录：

- 密码、refresh token、完整 bearer token；
- 原始文件、媒体正文和密钥；
- 未脱敏的 Agent prompt 或模型上下文；
- 不必要的身份证明和个人敏感资料。

## 7. Delivery semantics

### Local transaction

业务变更必须和本地 audit outbox 同事务：

```text
business state transition
+ aggregate version update
+ audit_outbox insert
```

### Remote delivery

- relay 使用 `event_id + idempotency_key` 幂等；
- 失败按 retryable/permanent 分类；
- 5xx/网络/429 进入有界重试；
- schema/tenant/auth 4xx 进入终态或 DLQ；
- 不删除已提交的本地事实；
- API receipt 必须与 event_id 匹配；
- 关键操作不能因“没有同步网络回执”而伪造 `archived`。

### Lifecycle

```text
accepted → ledgered → indexed → archived
```

业务系统在 UI 中应区分“本地已接收”和“治理账本已落地”。

## 8. Read and export audit

以下操作本身也必须审计：

- 查询审计事件；
- 查询操作时间线；
- 导出或下载合规数据；
- 创建/批准/拒绝恢复申请；
- 修改留存、Legal Hold 或来源配置。

审计读权限按租户和 platform scope 单独控制。查询人不因能读审计而自动能读业务资源。

## 9. Corrections and reversals

Audit event 是不可变事实：

```text
不能：update/delete 原事件
必须：append correction/revocation/reversal event
```

例如：

```text
approval.approved
→ approval.revoked
→ approval.reapproval.requested
```

撤销业务授权不等于撤销历史事实；恢复业务状态不等于删除原始执行记录。

## 10. Audit vs telemetry

| 项目 | Audit | Telemetry |
|---|---|---|
| 目的 | 证明业务事实和责任 | 定位性能和运行问题 |
| 存储 | append-only ledger/WORM | Metrics/Trace/Logs |
| 是否可删除 | 按留存和合规规则，不可篡改 | 可按运营策略采样/过期 |
| 内容 | actor、target、action、outcome、change | latency、error、queue、span |
| 是否能替代授权 | 不能 | 不能 |

高频资源读取可按请求聚合，但权限变更、拒绝、删除、恢复、跨租户访问和 Agent 工具调用不能无规则丢失。

## 11. Current audit-governance mapping

当前 `snaplink-audit-governance` 已有：

- EventEnvelope、Write/Batch/Stream ingest；
- operation/aggregate timeline；
- hash-linked ledger；
- WORM archive；
- idempotency；
- Legal Hold、export、restore approval；
- self-audit 和 read-path audit（以仓库 README/API 为 Verified 证据）。

本契约新增/收紧的是业务服务的 actor provenance、Agent 委托、Workflow 关联和关键事件 Outbox 纪律。

## 12. Acceptance criteria

1. 人、服务、自动化、Agent 事件可区分；
2. 一次采购可按 operation_id 还原提交、审批、执行和补偿；
3. 拒绝、撤销和失败事件不静默丢失；
4. 审计远程不可用时本地 outbox 可重试；
5. WORM 归档中的事件正文和 receipt/hash 可验证；
6. 读审计、导出和恢复审批自身也有审计轨迹；
7. 事件不包含 Token、密码、原始 prompt 或文件正文。
