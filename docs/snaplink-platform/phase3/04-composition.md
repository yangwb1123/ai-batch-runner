# 平台总体架构推进：组合设计

> 状态：PROPOSED；第一实现切片选择 `sverpweb-stable`，不创建新的 workflow 仓库。

## Primary Project

`sverpweb-stable`

理由：

- 当前采购申请和最小审批状态机已经存在；
- 业务 API、采购聚合、第一阶段 Workflow 运行边界在同一项目内；
- 可以先冻结通用 Workflow Contract，而不立即引入新的分布式服务；
- 该项目能作为后续 Workflow 模块抽取和其他企业流程复用的试点。

`aero-vault`、`snaplink-audit-governance` 和 `snaplink` 虽然是关键能力提供方，但本切片的主业务变更是 sverpweb 的采购流程契约和执行边界。

## Collaborating Projects

| 项目 | 本切片职责 | 变更边界 |
|---|---|---|
| `sverpweb-stable` | Workflow 首切片、采购聚合、Outbox、BFF/UI 状态 | 主变更项目 |
| `snaplink` | 身份、租户上下文、授权和撤销契约 | 只扩展必要的 workflow/approval 权限映射 |
| `aero-id` | 稳定 department ID、成员/岗位关系契约 | 冻结跨服务组织 ID 和事件约定 |
| `snaplink-audit-governance` | 审计事件接收、账本、时间线、治理证据 | 仅在现有契约不足时扩展 event schema；不写业务状态 |

## Unaffected Projects

| 项目 | 不变原因 |
|---|---|
| `aero-vault` | 首切片只复用现有资产/ACL 能力；sverpweb 保存 asset reference，不改 Vault 代码 |
| `aero-im` | 只复用现有通知接口；通知失败不影响核心流程 |
| `snaplink-console` | 首切片可通过已有治理查询能力查看证据；若新增专用 UI，另行提交独立变更 |
| `iris-ui` | 复用现有组件和布局；不改变 UI 基础库 |

这些项目仍可能出现在运行链路中，但“只消费已有 API”不等于本切片需要代码或契约变更。

## Data Sources of Truth

| 数据 | 唯一写入权威 | 其他项目允许做什么 |
|---|---|---|
| 用户、会话、Token、租户上下文 | `snaplink` | 验证、引用、缓存；不可自行改写 |
| 部门、组织、成员关系 | `aero-id`（当前过渡可读取 sverpweb） | 读取稳定 ID；不得按名称决定权限 |
| 采购申请/订单聚合 | `sverpweb-stable` | 通过 typed command/query 访问 |
| Workflow 定义/实例/WorkItem/Decision | `sverpweb-stable` 首切片 | 通过 workflow contract 读取或命令交互 |
| 文件/媒体/版本/ACL | `aero-vault` | 只保存 `asset_id` 引用，不复制 ACL 真值 |
| 通知发送状态 | `aero-im` | 只消费 delivery status |
| 审计事实/哈希链/WORM | `snaplink-audit-governance` | 发送事件、查询授权范围内的证据 |
| Trace/指标/运行日志 | OTel/观测系统 | 关联 operation/trace，不作为业务状态源 |
| Plugin/Profile inventory | 各项目构建产物 + suite lock | 组合仓库做对账，不直接修改项目内部 registry |

## Plugin and Module Composition

### 首切片

```text
sverpweb process composition root
  ├── identity adapter → snaplink
  ├── organization adapter → aero-id
  ├── workflow bounded module (static)
  ├── purchase domain (existing)
  ├── asset reference adapter → aero-vault
  ├── audit outbox adapter → audit-governance
  └── notification adapter → aero-im (optional)
```

首切片不创建动态 workflow plugin，不使用共享数据库，不把 workflow 状态写入 ACL/audit 表。

### 长期 Profile

```text
minimal:
  kernel + static domain modules + local adapters

full:
  kernel + static registry + audit/vault/org adapters
  + optional external typed plugins

production:
  full + signed manifest/lock + mTLS + durable storage
  + WORM/KMS + quotas + lifecycle + recovery gates
```

可信核心采用静态模块；重依赖、不同信任边界或需要独立扩缩容的功能使用 Unix Socket gRPC，远程运行时使用 mTLS。禁止 Go `.so` 作为通用插件 ABI。

## Synchronous Calls

同步调用保持短链，最大 3 跳：

1. `sverpweb -> snaplink`：验证当前租户、角色、审批/提交 capability；
2. `sverpweb -> aero-id`：查询或验证稳定部门/职位关系（可由缓存/投影减小调用频率）；
3. `sverpweb -> aero-vault`：上传附件或检查 asset reference（仅在业务需要时）；
4. `sverpweb -> aero-im`：不作为核心同步依赖；通知使用异步队列。

禁止：

- 每个文件读取都同步回源组织服务；
- workflow 同步调用 audit 再回调业务服务；
- `sverpweb` 直接读取其他项目数据库；
- 浏览器绕过 BFF 直接用管理凭据调用服务。

## Async Events and Outbox

### 业务事件

`sverpweb` 本地事务写入业务状态、流程状态和 audit/domain outbox，再由 relay 投递：

```text
purchase.submitted
workflow.instance.created
workflow.workitem.created
workflow.approval.recorded
workflow.execution.started
workflow.execution.completed
workflow.compensation.requested
asset.linked
```

### 外部消费者

- `snaplink-audit-governance`：接收并形成不可变审计事实；
- `aero-im`：消费通知事件并写 delivery status；
- console：通过管理查询读取，不直接消费业务数据库；
- future projector：可重建，只能作为投影，不是事实源。

### 投递要求

- `event_id + idempotency_key` 幂等；
- `operation_id`、`workflow_instance_id`、`aggregate_id` 必须透传；
- 失败重试、DLQ、backoff + jitter；
- `audit` 关键事件不可静默丢失；
- `notification` 可 best-effort，但必须保留发送结果和重试状态。

## Workflow Boundaries

Workflow 负责：

- 流程定义版本；
- 流程实例和节点状态；
- 待办分配、超时、升级；
- 审批决策和 authority snapshot；
- 调用 typed domain command；
- 取消、补偿和恢复编排。

采购域负责：

- 采购单字段、金额、供应商、预算和业务状态；
- `purchase.request.confirm` 的真实执行；
- 业务版本和幂等；
- 领域补偿/冲正命令。

Workflow 不应：

- 直接更新采购表；
- 把金额规则硬编码成不可版本化的流程脚本；
- 把批准状态当作执行成功；
- 将 Agent 输出作为审批事实。

## Permission and Capability Boundaries

### 权限

- `snaplink` 提供身份、租户和基础授权上下文；
- sverpweb 作为 PEP，在每个流程变更和 domain command 前重新验证；
- 资源 ACL 由资源属主服务判定；
- `/system/routes` 只做 UI 投影，不授予 API 权限。

### Agent

Agent task 的权限必须绑定：

```text
agent_id
initiator/on_behalf_of
workflow_instance_id
execution_run_id
tenant_id
resource scope
action scope
expiry
max operations/cost/bytes
revocation epoch
```

Agent 权限不超过委托人的可委托权限。高风险审批、删除、跨租户共享和权限修改仍需要人类或受控治理策略。

## Audit and Telemetry Boundaries

### audit-governance

- 接收关键业务、权限、审批、执行、撤销和恢复事件；
- 形成 append-only ledger、hash chain、timeline 和 WORM 证据；
- 提供按租户授权的查询、导出和恢复审批；
- 不反写采购、ACL 或用户业务状态。

### OTel/metrics/logs

- OTel 连接 `trace_id/span_id`；
- 指标观察队列、延迟、重试、错误和资源使用；
- 日志用于诊断；
- 任何 telemetry 缺失都不能被解释为业务操作没有发生。

## Agent and Automation Boundaries

| 执行者 | 允许 | 禁止 |
|---|---|---|
| Timer/Automation | 超时、重试、通知、补偿触发 | 自行改变审批授权 |
| Agent | 资料预审、建议、低风险已授权动作 | 自我审批、提权、任意写库 |
| Human | 依法定职责审批、取消和恢复申请 | 绕过后端 PEP |
| Service | 执行已批准 typed command | 伪造人类 actor 或审批 |

所有自动化和 Agent 执行都必须进入 `operation_id` 链并记录 actual executor 与 initiator。

## Optional Dependencies and Failure Policies

| 依赖 | 必选性 | 失败策略 | 业务结果 |
|---|---|---|---|
| snaplink authz | required | `fail_closed` | 不创建或不推进关键流程 |
| aero-id org query | required for approval route | `fail_closed` / `local_projection` | 无法确认审批资格时等待或拒绝 |
| aero-vault attachment | required when attachment referenced | `fail_closed` | 附件引用无效时不提交 |
| audit-governance ingest | required evidence | `durable_async` | 本地 outbox 可靠留存；关键操作可 pending |
| aero-im notification | optional | `best_effort_async` | 核心流程继续，delivery status 记录失败 |
| Agent Runtime | optional | `degrade` / `fail_closed` for assigned action | 无 Agent 不影响人工流程；受限任务失败则转人工 |
| OTel exporter | optional for local | `degrade` | 不影响业务，但记录本地诊断 |
| external plugin | profile-dependent | `required`/`degrade` per manifest | 启动或功能按 manifest 策略处理 |

## Forbidden Dependencies

- 直接数据库访问或共享表；
- 审计服务回调业务服务改变状态；
- sverpweb 与 Vault 双写 ACL；
- 用部门名称作为跨租户权限主键；
- 用长效人类 Token 驱动 Agent；
- 只靠前端菜单隐藏权限；
- 以 `approved` 直接标记 `executed`；
- Agent 自己审批、自我提权或修改自身 Grant；
- 任意动态代码、任意 shell 或 Go `.so` 插件；
- 通过 `full`/`production` 环境变量绕过构建和启动门禁。

## Elasticity and Partitioning

- sverpweb API 无状态；流程 Worker 按队列扩容；
- audit relay/projector/notification worker 独立扩容；
- Workflow 以 `tenant_id + workflow_instance_id` 做分区键；
- 审计以租户和 operation/aggregate stream 分区；
- 热租户可以独立队列、数据库或实例；
- 关键接口设置 timeout、retry、circuit breaker、bulkhead 和 queue limit；
- 每租户/每 Agent 有并发、字节、成本和调用次数预算；
- 读取投影可重建，业务和审计事实不可依赖单一内存实例；
- 删除、撤销、恢复等高风险操作必须优先一致性和可审计性，不因弹性改为 fail-open。
