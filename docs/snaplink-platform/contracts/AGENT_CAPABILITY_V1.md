# Agent Capability Grant Contract v1

> **状态：DRAFT / PROPOSED**  
> **契约版本：`agent-capability.v1`**

## 1. Purpose

定义自动化和 Agent 的最小执行能力，使 Agent 能参与企业流程而不成为权限源、审批源或隐式管理员。

## 2. Actor distinction

```text
initiator      原始发起者，可为 human/service/automation
on_behalf_of   被代表的主体
agent          实际 Agent 身份
executor       实际运行进程/Worker
approver       独立审批主体
```

审计中必须同时记录这些主体。不能把 Agent 调用伪装成用户直接调用。

## 3. Grant object

```yaml
capability_grant:
  grant_id: string
  version: integer
  tenant_id: string
  agent_id: string
  agent_version: string
  executor_id: string
  initiator_id: string
  on_behalf_of: string|null
  delegation_id: string|null
  workflow_instance_id: string|null
  execution_run_id: string
  audience: string
  allowed_actions: []
  resource_scope:
    type: asset|folder|aggregate|route|service|directory
    ids: []
    prefixes: []
  data_classification_max: public|internal|confidential|secret|regulated
  network_allowlist: []
  max_operations: integer
  max_bytes: integer
  max_cost_minor: integer|null
  issued_at: timestamp
  expires_at: timestamp
  revocation_epoch: integer
  approval_id: string|null
  policy_id: string
  policy_version: string
  status: active|expired|revoked|consumed|suspended
  issuer: string
  signature: string
```

## 4. Permission intersection

```text
Effective Agent Capability =
  delegator permissions
  ∩ agent profile
  ∩ workflow task scope
  ∩ grant allowed_actions/resources
  ∩ resource ACL
  ∩ tenant policy
  ∩ approval policy
```

Grant 只能收窄权限，不能扩大原主体的权限。

## 5. Issuance rules

- Grant 由受控的 Capability Broker 或授权服务签发；
- Agent 不能签发、修改或延长自己的 Grant；
- Grant 必须绑定 tenant、workflow/run、audience 和资源范围；
- 高风险命令必须有对应 `approval_id`；
- 默认过期时间短于人类会话或流程 SLA；
- Grant 使用计数、字节数和成本预算必须有上限；
- 签发、刷新、拒绝和撤销都产生审计事件。

## 6. Agent modes

```text
advisory          只输出建议/风险，不改变业务事实
precheck          只做结构化校验
approved-execution 已批准后执行限定命令
automation        依据固定策略执行低风险任务
```

默认模式是 `advisory` 或 `precheck`。以下操作默认不允许 Agent 自动完成：

- 付款、发货和预算提交；
- 删除或恢复关键资源；
- 修改 ACL、租户策略或角色；
- 跨租户共享；
- 互联网公开发布；
- 给自己或其他 Agent 提权。

## 7. Tool call

每次工具调用都要由服务端重新校验，不信任 Agent 的自然语言：

```yaml
tool_call:
  tool_call_id: string
  grant_id: string
  operation_id: string
  workflow_instance_id: string
  execution_run_id: string
  action: string
  resource: string
  arguments_digest: string
  started_at: timestamp
  deadline: timestamp
```

工具服务必须校验：

1. Grant signature、audience 和状态；
2. tenant 与资源一致；
3. action 在 allowlist；
4. 资源在 scope；
5. 未超过次数/字节/成本；
6. workflow 状态和审批仍然有效；
7. revocation epoch 未变化。

## 8. Revocation and kill switch

撤销来源包括：

```text
用户撤销委托
租户/部门成员关系变化
workflow cancelled
approval revoked
agent disabled
grant expired
security incident
revocation_epoch incremented
```

撤销流程：

```text
mark grant revoked
→ revoke/rotate execution token
→ cancel pending work item
→ fence running worker where possible
→ reject future tool calls
→ append audit event
```

不能保证已经发出的外部副作用被物理追回；必须把它标记为不可逆并进入补偿或人工恢复流程。

## 9. Self-approval and separation of duties

- `initiator_id == agent_id` 时，Agent 不得产生最终高风险批准；
- `executor_id == approver_id` 时，除非策略明确允许低风险自动化，否则拒绝；
- 流程策略应支持双人审批、最少人数和部门/财务职责分离；
- approval 只产生 decision，执行必须再次取得 execution grant。

## 10. Agent data policy

默认不保存：

- 原始提示词和完整上下文；
- bearer token、refresh token 和密码；
- 不必要的文件正文；
- 个人敏感数据原文。

保存：

- 输入/输出 digest；
- 工具调用和资源范围；
- 风险和策略结果；
- 受控 evidence reference；
- agent model/provider/version；
- 执行时长、attempt 和 outcome。

## 11. Failure policy

| 失败 | 处理 |
|---|---|
| Grant 无效/过期/撤销 | `fail_closed` |
| tenant/resource mismatch | `fail_closed` |
| Agent Runtime 不可用 | 低风险任务转人工/`degrade`，高风险执行失败关闭 |
| 工具超时 | 取消并记录，不默认重复副作用命令 |
| 可重试网络错误 | 保持幂等键的 bounded retry |
| 审计 outbox 失败 | 关键执行不报告最终成功 |
| Agent 建议失败 | 不改变业务状态，转人工 |

## 12. Audit events

```text
platform.agent.grant-issued@v1
platform.agent.grant-denied@v1
platform.agent.tool-called@v1
platform.agent.tool-denied@v1
platform.agent.grant-revoked@v1
governance.execution.started@v1
governance.execution.completed@v1
governance.execution.failed@v1
```

事件必须包含 `agent_id`、`initiator_id`、`grant_id`、`workflow_instance_id`、`execution_run_id` 和 policy/approval reference。

## 13. Acceptance criteria

1. Agent 无人类长期 Token 也能完成受限任务；
2. Agent 不能读取未授权租户或资源；
3. Grant 过期/撤销后新工具调用全部被拒绝；
4. Agent 不能自我审批或自我提权；
5. Agent 建议与最终批准可分离审计；
6. 工具调用可关联到 operation、workflow 和 execution run；
7. 超过调用/字节/成本预算后 fail closed；
8. 执行失败能够重试或转人工，不伪造完成。
