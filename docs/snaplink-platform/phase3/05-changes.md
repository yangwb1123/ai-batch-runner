# 平台总体架构推进：跨仓库变更设计

> 状态：PROPOSED；本文件描述第一垂直切片的实施边界，不等于批准代码变更。

## API and RPC Changes

### sverpweb-stable（Primary）

保留现有采购 API 形态，增加或内部生成以下字段，不通过客户端直接指定最终状态：

```text
operation_id
workflow_instance_id
idempotency_key
expected_version
asset_ids[]
```

建议的领域命令（服务端内部/typed contract）：

```text
purchase.request.submit
purchase.request.approve
purchase.request.reject
purchase.request.cancel
purchase.request.confirm
purchase.request.compensate
```

规则：

- `approve` 只创建/提交 `ApprovalDecision`，不能直接将业务聚合标成执行完成；
- `confirm` 必须校验流程已获得全部必需批准、聚合版本未过期、当前租户和 capability 有效；
- `cancel` 只允许在定义允许的状态中执行；
- `compensate` 必须由领域补偿契约支持，不能通用地反写旧状态；
- 所有命令使用幂等键和 `expected_version`，版本冲突返回明确错误。

### snaplink

仅扩展必要的契约/权限目录，不将采购业务状态移入 Snaplink：

```text
governance.workflow.start
governance.workflow.read
governance.workflow.approve
governance.workflow.cancel
governance.workflow.compensate
```

具体权限仍需映射到现有 `client_id`/role/scope 体系，并保留旧权限兼容期。租户上下文由签名 token、域名/client 绑定和服务端 membership 决定。

### aero-id

冻结组织读取/事件契约：

```text
organization.department.read
organization.membership.read
organization.membership.changed
```

跨服务引用采用：

```text
tenant_id + department_id + subject_id
```

名称只用于显示，不作为授权匹配或外键替代品。第一阶段若 aero-id API 尚不具备完整能力，使用明确的过渡适配器和投影，不能悄悄按名称匹配。

### aero-vault

本切片复用现有资产/ACL API。sverpweb 只传递 `asset_id` 和业务关联，不创建第二份文件 ACL。若新增接口，只允许补充稳定的 `asset.link`/`asset.access.check` typed contract。

### audit-governance

优先复用现有 `/api/v1/events`、batch/gRPC ingest、operation timeline 和 receipt。若现有事件 envelope 不足，新增向后兼容 schema，不改变既有账本语义。

### aero-im

复用现有通知 API；通知输入必须自包含业务摘要和 `operation_id`，不能让发送服务同步回取业务库上下文。

## Process and Approval Schema

### ProcessDefinition

```text
definition_id
definition_name
definition_version
domain
status
nodes
transitions
approval_policy_refs
failure_policies
owner_service
created_at
```

定义版本不可原地修改，运行中的实例固定引用 `definition_id + definition_version`。

### ProcessInstance

```text
workflow_instance_id
tenant_id
definition_id
definition_version
aggregate_type
aggregate_id
initiator_subject_id
department_id
status
current_version
operation_id
idempotency_key
due_at
context_digest
created_at
updated_at
```

### WorkItem

```text
work_item_id
workflow_instance_id
node_id
kind: human | service | agent | timer
candidate_scope
assignee_subject_id
status
deadline
claim_count
retry_count
```

### ApprovalDecision

```text
decision_id
work_item_id
workflow_instance_id
decision: approve | reject | return | defer
actor_type
actor_id
initiator_id
on_behalf_of
authority_snapshot
policy_id
policy_version
reason_code
reason_digest
decided_at
```

### Execution

```text
execution_id
workflow_instance_id
command_name
attempt_no
status
idempotency_key
aggregate_version_before
aggregate_version_after
actor_type
actor_id
error_code
started_at
completed_at
```

### Compensation

```text
compensation_id
execution_id
workflow_instance_id
type
reversible
status
compensation_command
requested_by
approval_id
started_at
completed_at
```

## Asset and Permission Schema

### 资源引用

sverpweb 仅保存：

```text
business_tenant_id
asset_id
aggregate_type
aggregate_id
purpose
created_by
```

### 共享范围

由 aero-vault 统一处理：

```text
private
department
tenant_internal
selected_principals
external_grant
share_link
internet_public
```

资源原属租户不因共享改变。跨租户共享必须新建显式 grant，且包含接收租户、主体、动作、过期时间和审计关联。

### 访问动作

```text
vault.asset.read
vault.asset.preview
vault.asset.download
vault.asset.write
vault.asset.share
vault.asset.delete
vault.asset.restore
vault.asset.manage_acl
```

所有动作必须由后端 PEP/PDP 重新校验。前端路由和菜单只能做能力投影。

## Audit Event Changes

### 事件注册

建议第一阶段注册：

```text
governance.workflow.created@v1
governance.workflow.submitted@v1
governance.workflow.status_changed@v1
governance.workitem.created@v1
governance.workitem.assigned@v1
governance.workitem.expired@v1
governance.approval.recorded@v1
governance.execution.started@v1
governance.execution.completed@v1
governance.execution.failed@v1
governance.compensation.requested@v1
governance.compensation.completed@v1
governance.permission.denied@v1
governance.capability.revoked@v1
vault.asset.linked@v1
vault.asset.accessed@v1
```

### Actor provenance

现有 audit envelope 的 actor/operation 字段应标准化承载：

```text
actor_type: human | service | automation | agent | system
actor_id
initiator_id
on_behalf_of
delegation_id
capability_grant_id
agent_id
agent_version
workflow_instance_id
execution_run_id
```

若 v1 envelope 尚不能增加顶层字段，短期可放入受限、版本化的 payload；长期应定义明确的 v2 schema，不能依赖自由文本。

### 审计内容

记录：

- 谁、何时、在哪个租户、通过何种主体执行；
- 对哪个 aggregate/asset/work item；
- 哪项 policy/capability 允许或拒绝；
- before/after 的安全摘要或 digest；
- operation/correlation/causation/trace 关联；
- 结果、失败码、重试和补偿关系。

不记录：密码、refresh token、完整 bearer token、原始 Agent prompt、原始文件正文和无必要的个人敏感信息。

## Telemetry and Trace Changes

### Trace

为每个请求、流程实例、WorkItem、领域命令和插件 RPC 创建 span，并透传：

```text
trace_id
span_id
operation_id
workflow_instance_id
execution_run_id
plugin_id
```

### Metrics

建议指标：

```text
workflow_instances_total
workflow_workitem_age_seconds
workflow_approval_decisions_total
workflow_execution_duration_seconds
workflow_execution_failures_total
workflow_retries_total
workflow_sla_breaches_total
authorization_denied_total
audit_outbox_pending_total
audit_ledger_lag_seconds
agent_tool_calls_total
agent_grant_revocations_total
```

用户 ID、asset ID、operation ID 不作为无限基数 metrics label；详细值进入结构化日志或审计事件。

### 日志

日志必须带 `operation_id` 和 `tenant_id` 的脱敏/受控引用。Telemetry 只用于诊断和 SLO，不能替代 audit-governance 的法务/业务事实。

## Agent Capability Changes

### Agent Task Grant

Agent 任务必须绑定：

```text
grant_id
agent_id
initiator_id
on_behalf_of
tenant_id
workflow_instance_id
execution_run_id
allowed_actions
resource_scope
issued_at
expires_at
max_operations
max_bytes
revocation_epoch
audience
```

### Agent 规则

- 默认只读或建议模式；
- Agent 权限不超过可委托主体；
- Agent 不能自我审批、自我提权或修改自身 Grant；
- 高风险动作需要人类审批和短期执行 Grant；
- 每个工具调用经过服务端 PEP，不能由 Agent 文本自行授权；
- 取消 workflow 时同时撤销未使用的 Grant；
- Agent 输出保存摘要/digest/reference，不默认保存敏感原文。

### Automation

定时器、Outbox relay、补偿调度器使用独立 service/automation actor，不冒充人类用户。策略自动批准必须记录 policy ID、版本和决策理由。

## Database Migrations

第一阶段不做跨项目共享数据库。sverpweb 迁移建议采用 expand/contract：

1. 新增独立 workflow/process/workitem/decision/execution/compensation 表；
2. 新增必要的 `operation_id`、`workflow_instance_id`、`idempotency_key` 索引；
3. 保持旧采购 API 和旧状态字段读兼容；
4. 建立旧状态到 Workflow 状态的只读映射；
5. 双读/对账验证后，再将流程状态读取切换到新模块；
6. 最后清理仅用于过渡的重复字段。

不得把 Workflow 表、ACL 表和审计事实表合并。

组织引用迁移：

- 保留现有展示名称；
- 增加 `department_id` 和 `department_tenant_id`；
- 对历史无稳定 ID 的数据标记 `unresolved`，不猜测映射；
- 经过人工或组织服务对账后再升级为强约束。

审计 outbox：

- 扩展事件类型、actor provenance 和 workflow 关联；
- 本地 outbox 与业务变更同事务；
- 保留重试、死信、永久拒收状态；
- 不删除旧审计事件。

## Plugin Manifest and Profile Changes

首切片只建立契约和 static module composition，不启用任意 runtime plugin。Manifest 至少声明：

```yaml
id: workflow.approval
version: 1.0.0
contract_version: v1
profiles: [minimal, full, production]
provides:
  - governance.workflow.start
  - governance.workflow.approve
requires:
  - identity.tenant-context.v1
  - audit.append.v1
failure_policy: durable_async
security:
  capabilities:
    - governance.workflow.approve
```

后续外部插件使用 Unix Socket gRPC；生产使用 mTLS、digest、签名和 provenance。

Profile 约束：

- minimal：Workflow 可作为同进程 bounded module；无 Kafka/WORM/外部 Agent；
- full：可加载审计、Vault、组织和通知适配器；
- production：必须配置持久化、mTLS、WORM/KMS、Outbox、限流、观测和恢复门禁；
- production 不能仅由环境变量或菜单开关表示。

## UI and Route Changes

sverpweb UI 增加：

- 采购流程进度；
- 当前待审批节点；
- `已审批` 与 `已执行` 分开显示；
- 退回、取消、补偿和失败原因；
- 审计关联入口。

路由与权限：

```text
compiled route
∩ backend capability
∩ tenant route policy
∩ user permission
```

`/system/routes` 只管理路由策略，不生成不存在的组件，也不授予 API 权限。

Console 第一阶段可复用现有 audit timeline，不新增业务写入能力。若以后增加平台流程管理页，必须通过管理 API 和自身审计事件完成。

## Compatibility and Versioning

- 现有采购 API 保持向后兼容；
- 老状态仍可读取，新增 Workflow 状态通过映射暴露；
- `ProcessDefinition` 按版本固定实例；
- 事件新增使用 `@v1`，不复用语义不同的旧事件名；
- consumer 必须幂等并能安全处理重复事件；
- 新字段先 optional，再经过对账升级为 required；
- 组织 ID 映射不以名称兼容作为长期方案；
- Agent grant 和 audit actor schema 必须有版本兼容窗口。

## Deployment Order

1. 主仓库冻结 Workflow、Audit actor、Capability、Manifest 合同；
2. 在 `sverpweb-stable` 增加独立 Workflow 表/模块和 adapter，不改变旧 API 外部语义；
3. 接入 Snaplink 的 workflow/approval 权限；
4. 接入稳定的组织 ID 读取和映射；
5. 将关键流程事件经 durable Outbox 投递到 audit-governance；
6. 接入现有 aero-vault asset reference；
7. 接入 aero-im 异步通知；
8. 完成拒绝、超时、撤销、执行失败和补偿测试；
9. 通过组合 E2E 后再启用更复杂的会签、Agent 执行和外部插件；
10. 生产部署前再启用 WORM、mTLS、KMS、HA 和独立 Worker。

## Rollback, Revocation and Recovery Plan

### Rollback

- 代码回滚前先停止新的流程实例创建；
- 保持当前流程定义版本可读；
- 旧 API 继续读取旧业务状态；
- Outbox 事件不可删除，使用兼容 consumer 或 DLQ 重放；
- 数据迁移采用 expand/contract，禁止直接破坏旧字段。

### Revocation

- 撤销用户 session/token 或 membership；
- 撤销审批 capability 和 Agent grant；
- 取消未执行 WorkItem；
- 将撤销事件写入审计；
- 后续请求必须重新鉴权，不依赖旧 UI 状态。

### Recovery

- `approved` 但未执行：取消或重新路由，不需要业务补偿；
- 已执行且可逆：调用领域 `compensate` 命令；
- 不可逆：创建人工恢复/冲正流程，不篡改原始审计事实；
- audit-governance 的 replay 只用于无副作用重建和审计，不直接恢复采购数据。

## Failure Matrix

| 组件/路径 | 失败策略 | 结果 |
|---|---|---|
| Snaplink 身份/授权 | `fail_closed` | 不创建、不审批、不执行 |
| aero-id 审批资格 | `fail_closed` 或可信投影 | 不能确认资格则等待/拒绝 |
| sverpweb 业务事务 | `fail_closed` | 不推进错误状态 |
| Workflow Worker | `local_queue` + bounded retry | 任务保留，超限转人工 |
| audit outbox 本地写入 | `fail_closed` | 关键变更不报告成功 |
| audit-governance 远程投递 | `durable_async` | 本地可靠留存，重试/DLQ |
| aero-im 通知 | `best_effort_async` | 业务继续，记录 delivery failure |
| aero-vault asset reference | `fail_closed` | 附件不可确认则不提交/不执行 |
| Agent Runtime | `degrade` 或高风险 `fail_closed` | 可转人工，不越权执行 |
| OTel exporter | `degrade` | 不影响业务，保留本地诊断 |
| 外部插件 | Manifest 指定 | required 失败阻止功能，optional 可降级 |

## Test Plan

### Unit

- Workflow 状态转移和非法转移；
- 审批策略、会签/顺序、SoD；
- `approved != executed`；
- 幂等、版本冲突和重复提交；
- 取消、超时、重试、补偿；
- Capability Grant 资源/动作/租户/过期校验；
- Actor provenance 和敏感字段脱敏。

### Contract

- Snaplink permission/capability contract；
- aero-id department/membership contract；
- audit event schema and receipt；
- asset reference/ACL contract；
- plugin Manifest、lock 和 Profile inventory。

### Integration

- sverpweb outbox → audit-governance ledger；
- 审计失败重试和 DLQ；
- 组织成员变化后的审批资格；
- Vault 资源共享和撤销；
- Agent 短期 grant 过期/撤销；
- aero-im 通知失败不阻断业务。

### Composition E2E

1. 用户创建并提交采购申请；
2. 部门审批通过；
3. 金额触发财务审批；
4. 审批通过但执行失败；
5. 重试或补偿；
6. 审计时间线可还原全链路；
7. 拒绝/超时/取消不产生错误执行；
8. 租户伪造、部门名称映射、跨租户访问均拒绝；
9. Agent 只能执行限范围动作，撤销后立即失效；
10. minimal/full/production 生成的模块 inventory 与实际产物一致。

### Production Gate

- `sverpweb` 仍明确单租户；
- 关键审计事件 durable；
- 无 dev auth、无任意插件、无长期 Agent Token；
- 有 WORM、密钥、恢复、回滚和故障演练证据；
- 所有仓库各自 `make ci`/`cli.py check` 通过；
- suite E2E 和契约版本对账通过。
