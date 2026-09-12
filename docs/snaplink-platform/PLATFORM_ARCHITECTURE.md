# Snaplink Platform 总体架构

> **状态：提案（PROPOSED）**  
> **版本：0.1**  
> **日期：2026-09-11**  
> **适用范围：** `snaplink`、`aero-vault`、`snaplink-audit-governance`、`aero-im`、`aero-id`、`sverpweb-stable`、`snaplink-console`、`iris-ui` 以及未来的组合仓库。

本文定义 Snaplink Platform 的总体边界、领域职责、业务流程、权限、审计、可观测性、Agent/自动化治理、Profile 和插件组合方式。

本文是跨仓库的架构契约和决策输入，不代表所有机制已经实现。事实状态使用以下标记：

- **Verified**：已在当前仓库或相关项目中看到实现或测试证据；
- **PROPOSED**：目标架构或待人工批准的设计；
- **TRANSITIONAL**：当前存在但应迁移的实现。

相关文档：

- [`BUILD_PROFILES_PLUGIN_ARCHITECTURE.md`](./BUILD_PROFILES_PLUGIN_ARCHITECTURE.md)：Profile、模块和插件构建细节；
- [`COMBINATION_MODEL.md`](./COMBINATION_MODEL.md)：跨项目组合和职责边界；
- [`MULTI_REPO_OPERATIONS.md`](./MULTI_REPO_OPERATIONS.md)：多仓库开发、隔离和发布；
- [`decisions/04-composition.md`](./decisions/04-composition.md)：组合模型决策；
- `snaplink-audit-governance/README.md`：审计账本、WORM、Outbox 和恢复治理能力；
- `snaplink/docs/plugin-system.md` 和 `snaplink/docs/adr/ADR-0009-static-and-runtime-modules.md`：插件实现参考。

---

## 1. 架构结论

Snaplink Platform 采用：

```text
稳定的平台内核
  + 版本化契约
  + 高内聚领域模块
  + 受控插件组合
  + 独立治理平面
  + 可水平扩展的数据平面
```

领域和横切能力不能混为一个模型：

```text
Identity / Tenant       谁是谁、当前属于哪个租户
Organization            部门、组织层级和成员关系
Authorization           当前请求能不能做
Business Workflow       申请、审批、执行、补偿
Business Domain         采购、库存、质量、报销等业务事实
Asset                   文件、媒体、附件及其生命周期
Audit Governance        不可篡改的业务事实和治理证据
Observability           指标、Trace、日志和性能
Agent Runtime           受限的自动化和 Agent 执行
```

核心原则：

1. **Workflow 管流程，业务域管事实。** 采购审批不应变成通用用户表或权限表中的几个字段。
2. **权限决定能否执行，审计记录已经发生了什么。** 审计不是权限源，也不能替代业务状态。
3. **所有业务变更、权限决策、自动化和 Agent 操作都必须可追溯。**
4. **审计事实不可删除；可撤销和可恢复通过新的补偿事实完成。**
5. **Agent 只能获得下放的临时能力，不能成为权限事实源或审批的隐形主体。**
6. **插件通过契约组合，不通过共享数据库或任意代码注入组合。**
7. **租户边界由服务端上下文决定，不能由前端提交的 `tenant_id` 赋予。**
8. **minimal 保持简单，full 组合完整能力，production 增加真实的安全、持久化和运维约束。**
9. **高内聚意味着模块拥有自己的事实、迁移、事件、测试和故障边界。**
10. **低耦合意味着跨模块只依赖稳定的 API、RPC、事件和 Capability，而不依赖实现细节。**

---

## 2. 目标与非目标

### 2.1 目标

- 每个服务可以独立构建和部署 `minimal`、`full`、`production`；
- 组合仓库可以按 Profile 选择模块和插件，并生成可验证的 inventory、lock 和 SBOM；
- 身份、租户、组织、资源、流程、审计、观测和 Agent 有清晰边界；
- 普通 API、异步 Worker、审计消费者和 Agent Runtime 可以独立水平扩展；
- 关键变更具备幂等、版本、审计、撤销、恢复和补偿语义；
- 社区版保持可理解、低依赖和可自助构建；
- 企业版通过受控模块、插件、托管能力和合规交付扩展，而不是 fork 核心；
- 插件故障、网络故障、审计延迟和依赖缺失不会造成静默越权或静默数据丢失；
- 跨仓库可以独立发布，同时通过兼容矩阵和契约测试保持组合可用。

### 2.2 非目标

本文不要求：

- 把所有代码都拆成动态插件；
- 把所有业务表合并到一个共享数据库；
- 让审计服务直接修改采购、文件或用户的业务状态；
- 用前端菜单隐藏代替后端权限；
- 让任意 Agent 或插件执行 shell、任意 SQL 或任意网络访问；
- 为每个小功能单独创建微服务；
- 将所有不可逆操作伪装成可回滚；
- 在没有契约、版本和验收证据时同时修改所有仓库。

---

## 3. 系统上下文

```text
                    ┌─────────────────────────┐
                    │ Web / Flutter / API     │
                    │ Console / ERP / Agent   │
                    └────────────┬────────────┘
                                 │ HTTPS/OIDC/API
                    ┌────────────▼────────────┐
                    │ Edge / API Gateway       │
                    │ TLS、基础限流、路由       │
                    └────────────┬────────────┘
                                 │
       ┌─────────────────────────▼─────────────────────────┐
       │ Platform Kernel                                    │
       │ Tenant Context / Operation / PEP / Capability      │
       │ Plugin Registry / Idempotency / Outbox / Health    │
       └────────────┬────────────┬────────────┬─────────────┘
                    │            │            │
       ┌────────────▼───┐ ┌──────▼──────┐ ┌───▼────────────┐
       │ Identity / Org  │ │ Workflow    │ │ Business       │
       │ Snaplink/aero-id│ │ Process     │ │ Domain Plugins │
       └────────────┬───┘ └──────┬──────┘ └───┬────────────┘
                    │            │            │
       ┌────────────▼────────────▼────────────▼────────────┐
       │ Asset / Audit / Agent / Integration Adapters       │
       │ aero-vault | audit-governance | Agent Runtime      │
       └──────────────────┬─────────────────────────────────┘
                          │
       ┌──────────────────▼─────────────────────────────────┐
       │ PostgreSQL / SQLite / Object Storage / NATS/Kafka   │
       │ OpenTelemetry / Prometheus / WORM / KMS             │
       └────────────────────────────────────────────────────┘
```

### 3.1 Control Plane 与 Data Plane

#### Control Plane

负责：

- 租户和组织配置；
- Profile 和插件注册；
- Capability、授权策略和审批策略；
- 流程定义及版本；
- 审计留存、Legal Hold、导出和恢复审批；
- 插件生命周期和兼容矩阵；
- 运行状态、配置版本和治理操作。

#### Data Plane

负责：

- 用户请求；
- 文件和媒体访问；
- 采购、质量、库存等业务命令；
- 流程任务执行；
- Agent 工具调用；
- 事件投递和后台任务。

控制面不可用时，数据面根据 capability 的失败策略分别处理。授权、删除、恢复和跨租户共享不能默认 fail-open。

---

## 4. 领域职责和权威归属

| 领域 | 推荐权威系统 | 拥有的事实 | 不应拥有 |
|---|---|---|---|
| 身份 | Snaplink | 用户、账号、MFA、会话、状态、全局 subject | 文件 ACL、采购状态 |
| 租户 | Snaplink | tenant、domain、client、membership、租户上下文 | 业务部门明细 |
| 组织 | `aero-id`；当前可由 sverpweb 过渡 | 部门层级、部门成员、职位关系 | 用户密码、文件对象 |
| 授权 | Snaplink + 各资源服务 | 身份权限、资源 ACL、Capability 决策 | 审计历史 |
| 资源 | aero-vault | 文件、媒体、版本、对象、ACL、分享、保留 | ERP 单据业务状态 |
| 流程 | Workflow/Process 模块或服务 | 流程定义、流程实例、待办、审批决定、补偿状态 | 采购金额、库存余额等领域事实 |
| 业务 | sverpweb 等领域服务 | 采购、库存、质量、报销、订单等聚合 | 通用用户和文件授权 |
| 审计治理 | snaplink-audit-governance | 不可变审计事件、哈希链、WORM、治理审批证据 | 业务当前状态、ACL 当前值 |
| 技术观测 | OTel/Prometheus/日志系统 | Trace、指标、运行日志 | 法定业务审计事实 |
| 自动化 | Agent Runtime / Scheduler | 任务执行、工具调用、运行状态 | 全局权限和审批事实 |
| UI | console、sverpweb、iris-ui | 页面、路由投影、用户个性化 | 最终授权 |

任何过渡实现只能有一个写入权威。若 sverpweb 暂时维护组织数据，`aero-id` 不能同时无规则地修改同一份组织事实。

---

## 5. 统一执行上下文

所有跨服务请求都应携带或派生统一的 `ExecutionContext`：

```text
request_id
operation_id
correlation_id
causation_id
trace_id / span_id
tenant_id              服务端解析并校验
principal               human/service/automation/agent/system
initiator               原始发起者
on_behalf_of            委托主体
capability_grant_id
workflow_instance_id
execution_run_id
idempotency_key
deadline / cancellation
client_id / source_system
```

约束：

- `tenant_id` 由签名 Token、域名解析和服务端 membership 共同确定；
- 客户端提交的租户字段只能作为一致性校验，不能改变上下文；
- 跨租户访问必须是显式授权，不得因为同一个 global subject 自动放行；
- 下游服务不得丢弃 `operation_id`、`tenant_id` 或委托链；
- Agent 和自动化不能复用人的长期访问 Token；
- 每个异步任务必须保存父操作和因果关系。

---

## 6. 权限和 Capability 模型

### 6.1 最终授权公式

```text
allow =
  authenticated
  ∩ tenant_context_valid
  ∩ active_membership
  ∩ role_or_scope
  ∩ resource_acl
  ∩ capability_grant
  ∩ workflow_policy
  ∩ risk_policy
  ∩ approval_requirement
```

Agent 的权限是交集，不是新增一套超级权限：

```text
Agent effective permission
  = initiator 可委托权限
  ∩ agent profile
  ∩ task grant
  ∩ resource ACL
  ∩ tenant policy
```

### 6.2 Capability Grant

Capability Grant 至少约束：

```text
grant_id
subject_type / subject_id
agent_id 或 service_id
tenant_id
resource_type / resource_id / path_prefix
actions
issued_at / expires_at
workflow_instance_id
execution_run_id
max_operations
max_bytes / max_cost
network_allowlist
revocation_epoch
audience
issuer
```

高风险 Capability 必须：

- 短期有效；
- 资源和动作明确绑定；
- 可单独撤销；
- 不能被执行者修改；
- 可关联审批和操作；
- 每次使用可被幂等和审计。

### 6.3 权限撤销

撤销必须立即影响未来请求，而不是只写一条审计记录：

```text
撤销用户会话/token
撤销租户 membership
撤销部门成员关系
撤销角色或 scope
撤销资源 ACL
撤销分享链接
撤销 Capability Grant
取消 workflow/run
提升 revocation_epoch
```

对于强撤销场景，不使用长期 S3 presigned URL；采用 Vault 代理下载或短时效能力 URL。

---

## 7. 共享文件和媒体资产

### 7.1 访问范围

| 范围 | 语义 |
|---|---|
| `private` | 仅个人或指定 owner |
| `department` | 指定部门成员，可继承组织层级策略 |
| `tenant_internal` | 当前租户有效成员 |
| `selected_principals` | 指定用户、部门、群组或角色 |
| `external_grant` | 另一租户的明确授权，带有效期和动作范围 |
| `share_link` | 受密码、次数、过期时间约束的分享链接 |
| `internet_public` | 明确发布到互联网的资源 |

`tenant_internal` 不等于 `everyone`。匿名公共访问必须走独立的发布或分享流程。

### 7.2 资源归属

资产模型建议支持：

```text
asset_id
resource_tenant_id
owner_type: user | department | tenant | service
owner_id
asset_kind: file | image | video | attachment | document
object_version
retention_policy
classification
status
```

共享不会改变资源原属租户。跨租户授权增加的是 grant，不是修改 `resource_tenant_id`。

### 7.3 资源授权权威

当前 aero-vault 已有用户、部门、群组、角色 ACL、分享和公共资源基础（Verified）。目标模型为：

```text
Snaplink       身份、租户成员
Org Service    部门成员投影
Aero Vault     对象、版本、ACL、分享和恢复
sverpweb       asset_id 与业务对象的关联
Audit          所有授权和访问事实
```

sverpweb 当前 `FileShareStore` 属于 TRANSITIONAL 实现。它不能与 Vault 长期共同决定权限；同步不能只依赖部门名称，应改为稳定的 `tenant_id + department_id` 和事件/Outbox 投影。

### 7.4 资产操作事件

建议统一事件：

```text
asset.created
asset.version.created
asset.metadata.updated
asset.owner.transferred
asset.acl.granted
asset.acl.revoked
asset.share.created
asset.share.revoked
asset.published
asset.unpublished
asset.previewed
asset.downloaded
asset.access.denied
asset.deleted
asset.restored
```

文件正文、密码、Token 和原始请求体不进入审计事件；使用安全字段差异、digest 或 `payload_ref`。

---

## 8. 通用业务流程和审批模型

申请、审批、确认、执行、撤销、补偿是独立的通用流程域，不能放进用户、ACL、资产或审计模型。

### 8.1 通用对象

```text
BusinessAggregate      采购申请、报销单、文件共享申请
ProcessDefinition      流程图及版本
ProcessInstance        一次实际流程
WorkItem               人工、服务、定时器或 Agent 待办
ApprovalDecision       审批决定和权限快照
CommandExecution       对业务域命令的执行记录
Compensation           取消、冲正、恢复和补偿
```

流程实例至少保存：

```text
tenant_id
process_instance_id
definition_id + definition_version
subject_type + subject_id
initiator
current_status
current_version
operation_id
idempotency_key
created_at / updated_at
due_at / SLA
```

### 8.2 节点类型

第一版采用有限、可验证节点：

```text
start
human_task
approval
service_task
agent_task
timer
condition / branch
parallel / join
compensation
end
```

不允许生产环境使用任意脚本解释审批和权限。条件表达式应使用受限 DSL 或策略服务，并限制执行时间、深度和外部访问。

### 8.3 状态分离

流程、任务和业务聚合不能共享一个万能状态枚举：

```text
Process:
  draft → submitted → running → waiting
  → completed | rejected | cancelled | expired | failed

WorkItem:
  pending → claimed → approved | rejected | returned
  → expired | cancelled

Execution:
  pending → running → succeeded | failed
  → compensating → compensated

BusinessAggregate:
  由采购、库存、质量等领域自行定义
```

`approved` 不等于 `executed`，审批通过只是获得执行资格。

### 8.4 采购示例

```text
PurchaseRequest: draft
      │ submit
      ▼
ProcessInstance: PurchaseApproval@v3
      │
      ├── budget.check（service task）
      ├── department.manager.approve（human approval）
      ├── finance.approve（金额策略触发）
      └── purchase.order.confirm（domain command）
                         │
                         ▼
                PurchaseRequest: approved/ordered
```

采购插件拥有金额、供应商、预算和采购状态；Workflow 只调用版本化领域命令，不直接读写采购表。

### 8.5 审批规则

必须支持：

- 顺序审批、会签、或签、最少人数；
- 金额和风险分级；
- 按部门、岗位、角色解析候选人；
- 委托审批和超时升级；
- 申请人不能审批自己的申请；
- 关键字段改变后审批自动失效；
- 记录审批时的权限、部门和策略快照；
- 审批决定不可删除，只能追加撤销或更正决定。

### 8.6 自动化和 Agent 流程

```text
human_task        真人处理
automation_task   预定义策略或系统任务
agent_task        受 Capability 限制的 Agent 任务
```

Agent 可以：

- 校验材料；
- 生成建议；
- 执行低风险、范围明确的动作；
- 代办已经批准的任务。

Agent 默认不能：

- 自己批准自己发起的申请；
- 修改审批策略；
- 修改自己的 Grant；
- 绕过领域服务直接写数据库；
- 直接执行任意 shell 或任意网络调用；
- 以自然语言结果代替后端授权。

高风险操作采用：

```text
Agent 建议 → 人工审批 → 短期 Grant → 领域命令 → 审计
```

### 8.7 撤销、恢复和补偿

- 尚未执行：取消流程、撤销待办和 Capability；
- 执行中：取消任务、fencing、终止运行；
- 已执行：调用领域提供的补偿命令；
- 不可逆操作：记录事实并执行冲正，不删除历史。

例如付款、发货或文件下载不能通用回滚。领域插件必须声明：

```text
reversible: true/false
compensation_command
required_approval
maximum_recovery_window
```

---

## 9. 审计、埋点和可观测性

### 9.1 分层

```text
Metrics        数量、延迟、错误率、队列深度
Trace          服务调用链和节点耗时
Logs           诊断上下文
Business Audit 事实、权限、审批、变更和治理证据
Product Event  页面行为和功能使用，可选
```

OpenTelemetry 不能替代业务审计；业务审计也不应承载高基数性能指标。

### 9.2 Audit Governance 集成

`snaplink-audit-governance` 是横切治理平面，负责：

- 统一事件接入；
- schema 校验和版本化；
- operation timeline；
- append-only ledger；
- 分段哈希链和完整性验证；
- WORM 归档；
- Legal Hold；
- 合规导出；
- 恢复申请、审批和证据；
- 跨租户、跨系统查询权限。

它不负责：

- 直接决定某个用户能否读取 Vault 文件；
- 直接修改采购、用户或文件的业务状态；
- 用审计事件回放替代真实的领域恢复命令。

### 9.3 统一审计事件

每个插件至少发出：

```text
request.accepted / request.denied
operation.started / operation.completed / operation.failed
state.changed
permission.granted / permission.denied / permission.revoked
approval.submitted / approval.approved / approval.rejected
execution.started / execution.completed / execution.failed
restore.requested / restore.approved / restore.rejected / restore.completed
```

事件字段至少包含：

```text
event_id
source_system
event_type + schema_version
occurred_at
tenant_id
operation_id
causation_id
correlation_id
trace_id / span_id
actor
initiator / on_behalf_of
workflow_instance_id
execution_run_id
targets
aggregate_type + aggregate_id + aggregate_version
action
outcome
reason
changed_fields / digest / payload_ref
data_classification
retention_class
idempotency_key
```

Actor 必须区分：

```text
human
service
automation
agent
system
anonymous
```

### 9.4 Outbox 和可靠性

业务状态变更和审计 Outbox 必须在同一个本地事务中提交：

```text
业务状态变更
+ aggregate version
+ audit_outbox
+ domain outbox
```

然后由 relay 投递到 audit-governance。不能把一次远程 HTTP 调用作为唯一的审计保证。

推荐状态语义：

```text
accepted  本地可靠接收
ledgered  已进入不可变账本
indexed   查询投影可见
archived  已完成 WORM 归档
```

高风险操作至少要求本地 Outbox 成功；删除、恢复、公开发布、跨租户共享和权限变更可要求 `ledgered` 后才向调用方报告最终成功。审计服务暂时不可用时，必须进入等待或失败关闭，不能静默丢失。

### 9.5 读操作

以下访问也应有审计策略：

- 文件预览、下载和导出；
- 审计事件查询和导出；
- 权限和配置查看；
- Agent 读取敏感数据；
- 被拒绝的请求。

大文件流式读取不按每个字节埋点，而按一次请求记录资源、范围、大小、结果和 digest。低风险高频读取可以聚合，但安全事件、权限事件和高风险访问不能无规则采样。

### 9.6 当前实现注意事项

sverpweb 的 `emitAuditEvent` 当前在发送失败时捕获异常并继续业务响应，这是 TRANSITIONAL 行为。对于通知类事件可以保留 best-effort；对于安全和业务关键变更必须改为 durable outbox/required 模式。

---

## 10. 插件和模块架构

### 10.1 依赖方向

```text
UI / API Adapter
       ↓
Application Service / Workflow
       ↓
Domain Model
       ↓
Ports / Contracts
       ↓
Infrastructure Adapter / External Plugin
```

跨领域依赖推荐：

```text
Workflow → typed domain command
Domain   → Asset/Audit/Identity port
Agent    → Workflow task + Capability broker
UI       → capability and route contract
```

禁止：

```text
Workflow → 直接读取采购数据库
sverpweb → 直接修改 Vault ACL 表
UI       → 自行判断最终权限
Agent    → 直接访问所有服务数据库
Audit    → 反向修改业务状态
```

### 10.2 插件类型

#### Static module

可信核心能力在编译期静态链接，适用于：

- 核心身份和安全边界；
- 简单 Workflow；
- 基础存储；
- 核心业务模块。

#### Precompiled module

代码已在二进制内，通过固定 registry/slot 启停。适用于：

- 可选但可信的能力；
- 需要较低调用开销的模块；
- 仍需被 Profile 裁剪的模块。

#### External typed plugin

高依赖、不同信任边界或需要独立扩缩容的能力使用：

```text
Unix Socket + gRPC/Protobuf
远程部署：TLS/mTLS
```

例如：

- audit-governance；
- 对象存储；
- 搜索、AI、杀毒；
- SIEM、消息、外部通知；
- Agent Runtime。

不采用 Go `.so` 作为跨项目通用插件机制。

#### Frontend plugin

优先使用构建期 bundled registry。只有经过来源、签名、版本、CSP 和权限校验的外部 micro/iframe 才能运行。

### 10.3 Plugin Manifest

```yaml
plugin:
  id: workflow.approval
  version: 1.0.0
  contract_version: v1
  kind: static-module
  profiles: [minimal, full, production]
  provides:
    - workflow.start.v1
    - workflow.approve.v1
  requires:
    - identity.tenant-context.v1
    - audit.append.v1
  optional_requires:
    - agent.task.v1
  routes: []
  events:
    publishes:
      - workflow.state.changed.v1
  migrations:
    - 001_workflow.sql
  security:
    capabilities:
      - workflow.start
      - workflow.approve
  failure_policy: durable_async
  resource_limits:
    cpu: "1"
    memory_mb: 512
```

Manifest 还应描述：

- health/readiness；
- transport 和 endpoint；
- 配置 schema；
- 依赖的数据库/队列/存储；
- migration 方向和回滚策略；
- provenance、digest、签名；
- 所属租户范围；
- UI capability 和路由贡献。

### 10.4 组合解析

```text
Profile
  → selected plugins
  → required capability closure
  → version compatibility
  → conflict/cycle validation
  → generated registry
  → lock + inventory + SBOM
  → build
```

解析失败必须发生在编译或启动前：

- 缺少 required capability；
- 版本不兼容；
- 重复 provider；
- 依赖循环；
- Profile 不允许；
- 生产插件未签名；
- 未声明入口、权限或迁移。

### 10.5 生命周期

```text
DISCOVERED
  → VALIDATED
  → STARTING
  → READY
  → ACTIVE
  → QUIESCING
  → DRAINING
  → STOPPED
```

外部插件握手需要确认：

- protocol version；
- plugin id/version；
- Profile；
- capabilities；
- binary digest/provenance；
- readiness；
- heartbeat；
- quiesce/shutdown；
- resource limit。

### 10.6 插件故障策略

| 能力 | 默认策略 |
|---|---|
| 身份和授权 | `fail_closed` |
| 审计 Outbox | `durable_async`，不得静默丢失 |
| 删除、恢复、ACL 变更 | `required` 或 `pending` |
| 通知 | `best_effort_async` + 重试 |
| 缩略图、搜索、推荐 | `degrade` |
| 外部对象存储 | 按读写和数据敏感度单独定义 |
| UI 插件 | 隔离失败，不拖垮主应用 |

---

## 11. Profile 组合

### 11.1 minimal

```text
Platform Kernel
静态核心模块
本地/轻量持久化
基础身份或本地开发身份
基础资源访问
简单串行 Workflow
基础日志和健康检查
```

约束：

- 不要求 Kafka、WORM、外部 Agent 和完整 Plugin Host；
- 不导入未选择的重型依赖；
- 仍保留租户和后端授权不变量；
- 可在普通开发机独立构建和测试。

### 11.2 full

```text
Snaplink identity
Organization service
Aero Vault
Generic Workflow
Audit Governance
Outbox / NATS / Kafka
OpenTelemetry
Agent Runtime
External integrations
```

约束：

- 所有模块有明确 capability provider；
- 可组合静态模块和受控外部插件；
- 支持完整的共享资产、审批、审计和 Agent 场景。

### 11.3 production

在 full 基础上强制：

```text
PostgreSQL/HA persistence
Kafka/NATS durable transport
TLS/mTLS
WORM + retention
KMS/HSM
signed plugin lock
SBOM/provenance
rate limit / quota / backpressure
outbox/retry/DLQ
health/readiness/metrics/trace
approval/SoD/revocation/recovery
backup/restore/drill
```

`production` 不能仅是 `full` 的别名，也不能只由环境变量声明。构建和启动必须对生产策略执行 fail-closed 校验。

---

## 12. 可伸缩和弹性设计

### 12.1 无状态入口

API、网关和 UI Server 尽量无状态：

- session 使用 Snaplink 或专用持久化；
- 文件内容使用 Object Storage；
- Workflow、Outbox 和审计状态使用持久化存储；
- 不在进程内保存必须依赖单实例的状态。

### 12.2 独立扩展单元

```text
API replicas
Workflow workers
Agent workers
Audit relay workers
Audit projector workers
Notification workers
Search/index workers
```

不同队列可以独立扩容和限流，不把所有工作放进 Web 请求线程。

### 12.3 分区和隔离

优先以以下维度分区：

```text
tenant_id
aggregate_id
workflow_instance_id
```

要求：

- 单租户异常不能拖垮其他租户；
- 每租户有配额、速率和并发限制；
- 队列消费者可按租户或业务域扩展；
- 热租户可独立迁移或分片；
- 高风险租户可使用独立数据库/实例。

### 12.4 背压和资源预算

所有 RPC、任务和插件调用必须有：

```text
timeout
deadline
max_payload
max_retries
backoff + jitter
circuit breaker
bulkhead
queue limit
cost/token/bytes budget
```

Agent 还需要：

- 最大运行时长；
- 最大工具调用次数；
- 最大文件读写量；
- 网络目标白名单；
- 单任务成本预算；
- 超时和取消传播。

### 12.5 一致性模型

- 关键业务状态：本地事务 + 乐观版本；
- 跨服务传播：Outbox + 幂等消费者；
- 可重建投影：事件/账本为事实源；
- 权限撤销：本地即时或短 TTL/epoch 约束；
- 通知和搜索：最终一致；
- 审计：本地可靠接收，按策略要求达到 ledgered/archived。

不能为了弹性把授权、租户边界或删除保护改成不受控的最终一致。

---

## 13. 数据、事件和契约原则

### 13.1 不共享业务表

每个领域拥有自己的表和迁移。跨领域只保存稳定引用：

```text
asset_id
purchase_request_id
workflow_instance_id
user_subject_id
external_reference
```

引用必须同时带租户上下文或由服务端补齐并校验。

### 13.2 事件必须可演进

事件至少包含：

```text
event_type
schema_id
schema_version
producer
occurred_at
operation_id
aggregate_version
payload_digest
```

消费者必须：

- 幂等；
- 支持已知旧版本；
- 对未知关键字段安全忽略或拒绝；
- 对不兼容版本进入 DLQ；
- 记录处理结果和重试状态。

### 13.3 领域命令优先

当操作改变业务状态时优先使用带版本和幂等的领域命令：

```text
purchase.approve
purchase.confirm
asset.grant
asset.revoke
asset.restore
workflow.cancel
```

不要使用泛化的：

```text
update(resource, arbitrary_json)
```

来绕过业务校验和权限审计。

---

## 14. 前端、路由和能力投影

### 14.1 有效能力

```text
compiled capability
  ∩ backend capability
  ∩ current tenant policy
  ∩ user/agent permission
  ∩ route definition
```

前端可以隐藏未授权菜单，但后端必须独立执行最终授权。

### 14.2 `/system/routes`

`/system/routes` 是路由和菜单策略目录，不是代码加载器：

- `component` 必须匹配已编译的 route key；
- `iframe` 必须匹配来源、CSP 和协议策略；
- `micro` 必须匹配版本、来源、签名和隔离策略；
- 新增不存在的组件只能进入不可用状态，不能凭数据库记录生成代码；
- 路由变更必须产生审计事件并刷新 effective navigation。

### 14.3 资产引用

Logo、公告图、企业媒体等不保存任意 URL，而保存：

```text
asset_id
purpose
tenant_id
optional domain_id
```

读取仍由 aero-vault 根据当前身份和资源 ACL 判断。

---

## 15. 部署模式

### 15.1 本地和社区

```text
一个组合进程或少量进程
SQLite/本地对象目录
本地 NATS 可选
审计本地 Outbox
无外部动态插件
```

目标是快速开发、可测试和低门槛贡献。

### 15.2 Full 组合

```text
Snaplink
sverpweb / domain services
aero-vault
aero-im
workflow workers
audit-governance
audit relay/projector
NATS/Kafka
Object Storage
OTel
```

服务可独立升级，但需满足契约和兼容矩阵。

### 15.3 Production

建议按租户规模选择：

```text
小型：共享服务 + 强租户隔离
中型：共享控制面 + 业务服务分片
大型：独立业务实例/数据库 + 共享治理和身份
```

当前 sverpweb 仍是单租户实现（Verified/TRANSITIONAL），第一阶段应采用每租户独立实例/数据库，不应只迁移用户和菜单就打开共享多租户。

---

## 16. 当前项目映射

| 项目 | 目标角色 | 当前状态 |
|---|---|---|
| `snaplink` | Identity、Tenant、OAuth/OIDC、会话和基础授权 | 已有较完整基础；Profile/生产策略需统一 |
| `aero-vault` | Asset、Object ACL、分享、版本和恢复 | 已有 ACL/部门/分享基础；租户和生产策略需继续收紧 |
| `aero-id` | Organization、部门和成员关系 | 作为目标组织权威；需定义跨服务事件契约 |
| `sverpweb-stable` | ERP Domain、Workflow 首个试点、Web 路由 | 有业务审计和文件分享过渡实现；仍单租户 |
| `snaplink-audit-governance` | 审计、WORM、治理、恢复审批和操作回放 | 已有可运行参考实现和丰富治理能力 |
| `aero-im` | Notification、消息和异步协作 | 适合作为可选集成插件 |
| `snaplink-console` | Identity/Platform 管理 UI | 已有 OIDC 管理端和能力消费基础 |
| `iris-ui` | 通用 UI Shell、组件和前端插件 slot | 作为 UI 基础库，不拥有业务权限 |

---

## 17. 实施路线

### Phase 0：契约冻结

不改所有业务代码，先冻结：

- [ ] `ExecutionContext`；
- [ ] Principal、Delegation、Capability Grant；
- [ ] Plugin Manifest、lock、inventory；
- [ ] Profile schema 和 capability closure；
- [ ] Workflow/WorkItem/Approval/Compensation schema；
- [ ] Audit Event schema；
- [ ] Telemetry 和 Trace 关联规则；
- [ ] Revocation、Kill Switch 和 Recovery Contract；
- [ ] failure policy 和数据分类；
- [ ] 版本兼容与迁移规则。

### Phase 1：业务流程垂直切片

以 sverpweb 的采购流程为第一条验证链路：

```text
用户提交采购申请
→ 部门审批
→ 金额触发财务审批
→ 领域命令确认
→ 审计时间线
→ 撤销/拒绝/补偿
```

必须接入：

- Snaplink 租户和用户上下文；
- Workflow 通用模型；
- 业务领域命令；
- audit-governance；
- OTel；
- Outbox 和幂等；
- 后端权限和 SoD。

### Phase 2：共享资产垂直切片

```text
用户上传私有文件
→ 申请部门共享
→ 审批/授权
→ Vault ACL
→ 部门成员变化
→ 撤销
→ 删除和恢复
```

验证 sverpweb 的过渡 `FileShareStore` 是否可以退化为引用/投影，最终由 Vault 成为资源 ACL 权威。

### Phase 3：静态 Profile 和模块 registry

- [ ] 为一个 Go 服务完成 minimal/full/production；
- [ ] 生成模块 registry、inventory、lock 和 SBOM；
- [ ] 运行启动前 capability closure 校验；
- [ ] 添加缺失模块、版本冲突、Profile 污染测试；
- [ ] 添加 required/optional/degrade 失败策略测试。

### Phase 4：外部插件

先选一个真实边界明确的能力：

```text
audit-governance
或 object-storage/search/notification
```

完成：

- [ ] Unix Socket gRPC；
- [ ] 版本握手；
- [ ] mTLS 生产配置；
- [ ] capability allowlist；
- [ ] timeout/circuit breaker；
- [ ] drain/shutdown；
- [ ] digest/signature/provenance；
- [ ] 插件故障和恢复演练。

### Phase 5：组合仓库和扩展

- [ ] 真实版本兼容矩阵；
- [ ] 登录、租户、部门、资产、审批和 Agent E2E；
- [ ] 多租户隔离和跨租户授权 E2E；
- [ ] Profile 真实构建差异验收；
- [ ] 负载、故障、撤销、恢复和升级演练；
- [ ] 社区构建路径和商业插件路径分离。

---

## 18. 验收门禁

### 18.1 架构门禁

- [ ] 领域没有直接访问其他领域数据库；
- [ ] 所有跨域调用有版本化契约；
- [ ] 每个数据表有唯一 owner；
- [ ] 依赖图无循环；
- [ ] Profile 与实际产物 inventory 一致；
- [ ] 插件没有未声明 capability。

### 18.2 租户和权限门禁

- [ ] 客户端不能单独切换 `tenant_id`；
- [ ] 当前租户、membership 和资源租户一致性被服务端校验；
- [ ] 前端隐藏不能绕过后端；
- [ ] 跨租户访问必须有显式 grant；
- [ ] 权限撤销立即影响未来请求；
- [ ] Agent 权限不超过委托范围；
- [ ] Agent 不能自我审批或自我提权。

### 18.3 审计和观测门禁

- [ ] 成功、失败和拒绝操作可关联到 operation；
- [ ] 审计事件可通过时间线还原；
- [ ] 业务变更和 Outbox 原子关联；
- [ ] 审计失败不会静默丢失；
- [ ] WORM、哈希链和保留策略已验证；
- [ ] OTel Trace 与业务审计可关联但不相互替代；
- [ ] 不记录 Token、密码和原始敏感正文。

### 18.4 Workflow 门禁

- [ ] 流程定义固定版本；
- [ ] 审批人与申请人职责分离；
- [ ] `approved` 和 `executed` 分离；
- [ ] 关键字段变化会重新审批；
- [ ] 重试和重复提交幂等；
- [ ] 取消、超时、拒绝和补偿有明确状态；
- [ ] 不可逆操作明确标记并保留证据。

### 18.5 弹性门禁

- [ ] API 可水平扩展；
- [ ] Worker 可按队列和租户扩展；
- [ ] 插件有超时、熔断和资源预算；
- [ ] 队列有背压、重试和 DLQ；
- [ ] 单租户故障不会阻断全局；
- [ ] 数据库、对象存储和消息系统有 readiness 检查；
- [ ] 可执行停机、迁移、恢复和回滚演练。

---

## 19. 社区、商业和插件生态

### 19.1 社区版

- minimal 可独立构建；
- 核心契约、Profile 和插件协议公开；
- 社区无需商业密钥即可构建 full 的公开模块；
- 核心安全、租户和审计不被人为削弱；
- 贡献者可以只运行 minimal 门禁完成大多数修改。

### 19.2 商业扩展

商业边界可以放在：

```text
企业插件
托管服务
合规存储和运营
高级 Agent/AI 能力
企业支持和 SLA
```

License/Entitlement 必须在后端执行，不能只删除前端按钮。商业插件仍需遵守 Manifest、Capability、审计和租户边界。

### 19.3 插件撤销

插件被撤销、禁用或升级时：

- 停止新的 capability 授予；
- 撤销该插件的运行 Grant；
- 停止接收新的任务；
- 进入 quiesce/drain；
- 保留其业务数据和审计证据；
- 使用兼容版本或迁移方案处理存量数据；
- 不通过删除审计记录来掩盖插件行为。

---

## 20. 参考实现和借鉴项目

仅借鉴架构思想，不直接复制代码、协议或许可证实现：

| 项目 | 借鉴点 |
|---|---|
| OpenTelemetry Collector | core/contrib/custom distribution 和组件构建 |
| Caddy | 静态模块、构建时组合和模块 registry |
| Keycloak | Provider SPI、认证扩展和事件监听 |
| HashiCorp Vault | 外部插件、进程隔离、握手、签名和生命周期 |
| Envoy | 类型化扩展点和扩展工厂 |
| Grafana | 前后端插件、签名和插件目录 |
| Backstage | 应用组合和领域插件 |
| VS Code | Manifest、扩展宿主和权限边界 |
| Temporal / Camunda 类系统 | Durable Workflow、任务、计时器和补偿思想 |
| OpenFGA / Zanzibar 类系统 | 关系授权和资源关系建模思想 |

---

## 21. 未决决策

以下事项在 Phase 0 必须人工批准：

1. `production` 是否从 `full` 兼容别名升级为独立生产 policy；
2. Workflow 首阶段作为 sverpweb 内置模块，还是单独服务；
3. 组织权威最终由 `aero-id` 还是其他组织服务承担；
4. `department`、`tenant_internal` 是否增加显式 principal 类型；
5. Agent 是独立外部服务，还是先作为受限 Worker；
6. 跨租户资源 grant 的审批和有效期上限；
7. 哪些操作必须等待 `ledgered`，哪些可以本地 durable-async；
8. 前端 micro plugin 是否需要独立签名目录；
9. Manifest 是各仓库维护后由 suite 汇总，还是由中央 registry 管理；
10. 社区可以构建哪些企业插件，以及哪些能力仅提供托管服务。

---

## 22. 最终设计原则

```text
Identity 说明“谁”
Tenant 说明“在哪个租户”
Organization 说明“属于哪个部门”
Authorization 说明“能不能做”
Workflow 说明“还要经过什么流程”
Business Domain 说明“业务事实是什么”
Asset 说明“文件和媒体当前是什么状态”
Agent Runtime 说明“由什么自动化主体执行”
Audit Governance 说明“不可抵赖地发生过什么”
Observability 说明“系统是如何运行的”
Plugin System 说明“能力如何安全组合”
Profile 说明“本次交付包含什么”
```

最终目标不是把系统拆得越碎越好，而是让每个边界都能独立演进、独立扩展、独立失败、独立审计，并通过清晰契约组合成一个可理解、可伸缩、可恢复的平台。
