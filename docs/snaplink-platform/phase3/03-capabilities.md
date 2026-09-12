# 平台总体架构推进：能力发现与边界

> 状态：PROPOSED（基于 `01-intake.md`、`02-scenario.md` 与现有平台地图的契约草案）

## Required Capabilities

### A. Platform Kernel

- 服务端租户上下文解析与一致性校验；
- `ExecutionContext` 透传：`operation_id`、`correlation_id`、`causation_id`、`trace_id`；
- 幂等键与聚合版本校验；
- Capability Grant 校验、超时、取消与 revocation epoch；
- 插件/模块 registry、manifest、版本握手、健康检查；
- Outbox、重试、DLQ、背压和失败策略；
- 通用错误模型和 readiness。

### B. Identity and Organization

- 用户、服务、自动化和 Agent Principal；
- tenant membership、角色、scope 和会话撤销；
- 部门层级、职位、稳定 `department_id` 和成员关系；
- 委托主体、审批资格和权限快照。

### C. Generic Workflow

- ProcessDefinition 与版本；
- ProcessInstance 与业务聚合引用；
- WorkItem（human/service/agent/timer）；
- ApprovalDecision、会签/或签/顺序和职责分离；
- Execution、重试、超时、取消；
- Compensation、恢复和不可逆操作声明；
- typed domain command，不直接写业务表。

### D. Business Domain

- 采购申请、预算、采购确认等领域事实；
- `approved` 与 `executed` 的分离；
- 业务状态的唯一写入权威；
- 领域自身的补偿命令和不可逆边界。

### E. Asset and Shared Resource

- 文件、媒体、附件、版本、分享和 ACL；
- 用户、部门、租户内部公共、外部显式授权和互联网公开的范围区分；
- `asset_id` 引用与业务对象关联；
- 资源撤销、软删除、恢复和保留策略。

### F. Audit and Governance

- 统一业务审计事件和 schema version；
- durable outbox 与幂等投递；
- `accepted -> ledgered -> indexed -> archived` 生命周期；
- operation/aggregate timeline、WORM、Legal Hold、导出和恢复审批；
- Agent/automation 的 initiator、executor、grant 和 approval provenance。

### G. Telemetry

- OTel Trace、metrics、structured logs；
- Workflow queue age、SLA、执行延迟、重试率、授权拒绝率；
- audit 与 telemetry 通过 operation/trace 关联，但不相互替代；
- 高基数 ID 不作为指标 label 的无限维度。

### H. Plugin Composition and Elasticity

- minimal/full/production Profile；
- static module、precompiled module、external typed plugin；
- Manifest、capability closure、签名 lock、inventory 和 SBOM；
- 无状态 API、独立 Worker、按租户分区、资源预算、熔断和隔离。

## Existing Capabilities and Evidence

| 能力 | 项目 | 证据/状态 |
|---|---|---|
| OIDC、Token、租户和授权 | `snaplink` | Verified：现有 token、tenant、permission 和 session 模型 |
| 组织/部门模型 | `aero-id` | Verified/目标权威：平台地图声明 organization/department/membership 所有权 |
| 文件 ACL、部门 ACL、分享、公共资源 | `aero-vault` | Verified：`internal/access` 已有 tenant-scoped ACL、部门成员、share/public asset 基础 |
| 审计账本、操作时间线、WORM、恢复治理 | `snaplink-audit-governance` | Verified：README/API/proto 已有 ingest、ledger、archive、restore 和 replay |
| sverpweb 采购/审批业务 | `sverpweb-stable` | Verified/Transitional：已有采购申请和最小审批状态，仍单租户 |
| sverpweb Outbox 审计适配 | `sverpweb-stable` | Verified/Transitional：已有 audit outbox/forwarder；关键路径仍需 durable 语义收紧 |
| 通知和消息 | `aero-im` | Verified：作为通知适配器使用，非审批权威 |
| 管理端和路由投影 | `snaplink-console` | Verified：管理 UI、OIDC 和审计查询基础 |
| UI 组件与插件 slot | `iris-ui` | Verified/Transitional：作为 UI 基础库，不拥有业务数据 |
| Profile/静态模块/外部插件参考 | `snaplink` | Verified：sso-server 已有 profile、module registry、lock 和 typed external plugin 设计 |
| Agent 任务 Capability Grant | `ai-batch-runner` / AADM 设计 | Verified in tool design；跨产品运行时接入 Proposed |

## Capability Map

| 能力 | 主责任项目 | 首切片复用模式 | 长期模式 |
|---|---|---|---|
| tenant context | `snaplink` | reuse | reuse |
| identity principal | `snaplink` | reuse | reuse |
| department identity | `aero-id` | adapt/contract | reuse |
| authorization decision | `snaplink` + domain PEP | reuse/adapt | split by resource owner |
| generic workflow contract | `sverpweb-stable` | create bounded module | extractable contract/plugin |
| purchase aggregate | `sverpweb-stable` | reuse/adapt | domain-owned |
| approval policy | `sverpweb-stable` | create policy port | policy plugin/service |
| asset reference | `sverpweb-stable` | reuse ID reference | reuse |
| asset ACL | `aero-vault` | reuse existing API | reuse |
| audit event contract | `snaplink-audit-governance` + platform contract | adapt | reuse |
| audit outbox | `sverpweb-stable` | extend critical paths | per-domain adapter |
| telemetry | each service + OTel | adapt | reuse shared conventions |
| agent execution | `sverpweb-stable` worker boundary | exclude from final approval | external constrained runtime |
| notification | `aero-im` | reuse optional | external adapter |
| frontend route projection | `sverpweb-stable` | reuse existing navigation | capability-aware registry |
| plugin manifest | `snaplink` pattern + platform contract | create contract artifact | shared suite contract |

## Reuse / Adapt / Extend / Create / Exclude Decisions

### Reuse

- Snaplink authentication, tenant context and base authorization;
- aero-vault asset/ACL APIs;
- aero-id organization identifiers and membership API/event contract;
- audit-governance ingest, ledger, timeline, archive and restore governance;
- aero-im notification API;
- existing sverpweb purchase route shape and UI shell;
- existing `operation_id` and audit outbox concepts.

### Adapt

- sverpweb audit calls: distinguish ordinary best-effort events from critical durable events;
- sverpweb purchase status: preserve existing API shape while adding explicit workflow/execution states in a separate contract;
- department references: replace name matching with stable tenant-scoped IDs;
- audit actor data: add human/service/automation/agent provenance and delegation;
- console evidence view: show approval and execution separately when the existing view is extended.

### Extend

- `ExecutionContext` with grant, workflow, delegation and run IDs;
- event registry with workflow, approval, execution, compensation and asset-link events;
- PEP checks for workflow transitions and Agent task commands;
- outbox schema/relay rules and operational metrics;
- profile/module manifest and capability inventory;
- contract tests and E2E tests for tenant isolation and revocation.

### Create

- Workflow Contract v1;
- ProcessDefinition/ProcessInstance/WorkItem/ApprovalDecision/Execution/Compensation schemas;
- first `purchase-approval@v1` definition;
- policy port for amount/risk-based approval routing;
- typed domain command port (`purchase.request.confirm`);
- generic failure and compensation matrix;
- Agent task grant and provenance contract;
- platform plugin manifest and Profile lock contract.

### Exclude from the first slice

- sverpweb shared-instance multi-tenancy;
- a new independent workflow repository;
- arbitrary runtime code plugins or Go `.so` loading;
- automatic Agent final approval for payment/delete/cross-tenant actions;
- direct workflow writes to purchase tables owned by the domain;
- migrating all existing file-share ACLs at once;
- replacing the existing user/tenant model;
- production activation based only on environment variables.

## Capability Boundaries

1. Workflow stores process state and references a business aggregate; it does not own the aggregate.
2. Approval records a decision and authority snapshot; it does not directly mutate arbitrary business data.
3. Domain services execute typed commands and return versioned results.
4. Audit-governance receives immutable facts; it does not become the business state source.
5. Telemetry records technical behavior; it does not establish legal approval evidence.
6. Agent Runtime receives a bounded grant; it cannot issue or expand its own grant.
7. Asset ACL remains in aero-vault; sverpweb stores `asset_id` references only.
8. Route/menu configuration projects capabilities; it cannot grant backend authority.
9. Plugin composition uses declared contracts, not shared database tables.
10. `tenant_id` and `department_id` are server-validated and propagated through every boundary.

## Missing Contracts

- Workflow Contract v1 schema and event registry;
- approval policy DSL/port and rule version snapshot;
- organization ID and membership event contract;
- audit actor/delegation/Agent extension;
- critical-event durable outbox API for sverpweb;
- Agent Capability Grant and cancellation/fencing API;
- Profile/Plugin Manifest schema shared by Go, Rust, Bun and Flutter builds;
- composition suite E2E for purchase + asset + audit + revocation;
- explicit sverpweb single-tenant deployment guard in the first implementation.
