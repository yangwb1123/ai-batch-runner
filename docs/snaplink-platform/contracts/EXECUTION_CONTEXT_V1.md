# Execution Context Contract v1

> **状态：DRAFT / PROPOSED**  
> **契约版本：`execution-context.v1`**

## 1. Purpose

统一 HTTP、RPC、异步事件、Workflow、插件和 Agent 调用中的身份、租户、操作和追踪上下文。

它不是 Token 格式，也不是业务领域模型；它是跨边界的最小安全上下文。

## 2. Context object

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
    type: human|service|automation|agent|system|anonymous
    id: string
  initiator:
    type: human|service|automation|agent|system
    id: string
  on_behalf_of: string|null
  delegation_id: string|null
  capability_grant_id: string|null
  workflow_instance_id: string|null
  execution_run_id: string|null
  idempotency_key: string|null
  client_id: string|null
  source_system: string
  started_at: timestamp
  deadline: timestamp|null
  data_classification: public|internal|confidential|secret|regulated
```

## 3. Identifier semantics

| 字段 | 语义 | 生命周期 |
|---|---|---|
| `request_id` | 一次 HTTP/RPC 请求 | 请求范围 |
| `operation_id` | 一次完整业务操作 | 跨服务操作范围 |
| `correlation_id` | 一条业务链的关联 ID | 业务链范围 |
| `causation_id` | 直接触发当前事件的事件/操作 | 事件范围 |
| `trace_id` | 技术调用链 | Trace 范围 |
| `workflow_instance_id` | 一次流程实例 | Workflow 范围 |
| `execution_run_id` | 一次 Worker/Agent 执行 | 执行范围 |
| `idempotency_key` | 防止重复创建或执行 | 命令范围 |

同一项不得用一个字段代替另一个字段。尤其不能以 `trace_id` 替代业务 `operation_id`。

## 4. Tenant resolution

租户上下文按以下优先级服务端解析：

```text
签名身份 Token / client binding
  + domain/application mapping
  + active membership
  + resource tenant consistency
```

规则：

- 客户端传入的 `tenant_id` 只能用于一致性检查；
- 请求不得通过修改 JSON、URL 或 Header 中的租户字段切换租户；
- 资源读取必须检查 `principal.tenant_id` 与资源所属租户；
- 跨租户访问只能由显式 external grant 授权；
- 缺少租户边界的旧上下文不得自动推断，必须 fail closed；
- `tenant_id="*"` 只允许明确的受控平台主体，不允许普通用户使用。

## 5. Principal and delegation

### 5.1 Principal

```text
human       真人用户
service     业务服务或客户端
automation  定时器、relay、调度器
agent       Agent Runtime 中的具体 Agent
system      受控内部维护主体
anonymous   未认证访问，仅适用于公开资源
```

`principal` 表示当前执行者，`initiator` 表示原始发起者。不能把 Agent 操作伪装为 initiator 用户的直接操作。

### 5.2 Delegation

委托上下文必须有：

```text
delegation_id
initiator_id
executor_id
allowed_actions
resource_scope
tenant_id
issued_at
expires_at
revocation_epoch
```

委托权限只能收窄，不能扩大原主体权限。

## 6. Propagation

### HTTP

使用标准追踪 Header 和受控扩展 Header：

```text
traceparent
X-Request-Id
X-Operation-Id
X-Correlation-Id
X-Causation-Id
```

跨服务的租户和身份以签名 Token/内部认证上下文为准，不能信任普通可写 Header。

### RPC/plugin

Context 作为 protobuf metadata 或明确的 envelope 字段传递。插件必须校验 audience、tenant、grant 和 deadline。

### Async event

事件 envelope 必须保存：

```text
operation_id
correlation_id
causation_id
tenant_id
principal/initiator provenance
workflow_instance_id
execution_run_id
```

消费者不得用自己生成的 operation_id 覆盖生产者关联链。

## 7. Authorization binding

最终授权至少需要：

```text
ExecutionContext
  + current policy decision
  + resource ACL
  + capability grant (if delegated)
  + expected aggregate version
  + workflow/approval state (if applicable)
```

只验证 Token 有效不能证明有权执行特定业务命令。

## 8. Idempotency and concurrency

- 创建操作必须使用显式或服务端派生的幂等键；
- 重试保持同一 `operation_id` 或建立有因果关系的新尝试，不能创建无法关联的重复业务操作；
- 领域命令必须带 `expected_version`；
- 版本冲突返回结构化错误，不能覆盖新状态；
- Worker 和 Agent 重试必须记录 attempt/run ID。

## 9. Observability

- OTel span 使用同一 `trace_id`，并添加低基数属性 `operation_id`、`workflow_instance_id`、`plugin_id`；
- 业务审计使用 audit-governance，不用 Trace 作为法定事实；
- Metrics 不使用无限基数的 user/asset/operation ID label；
- 日志和事件按 `data_classification` 脱敏。

## 10. Failure behavior

| 缺失/不一致 | 处理 |
|---|---|
| tenant mismatch | `fail_closed` |
| principal 缺失 | `fail_closed` |
| grant 过期/撤销 | `fail_closed` |
| operation 关联缺失 | 关键写操作拒绝；普通诊断记录缺失告警 |
| deadline 已过 | 取消执行并写失败事件 |
| trace exporter 不可用 | telemetry degrade，不改变授权 |
| audit outbox 本地不可用 | 关键变更 fail closed |

## 11. Audit mapping

当前 audit-governance `EventEnvelope` 已有 `operation_id`、`causation_id`、`correlation_id`、`trace_id`、`actor`、`workflow_instance_id`、`execution_run_id` 等字段（Verified）。

本契约要求进一步标准化：

- actor type/id 与 initiator/executor 分离；
- delegation 和 capability grant 可追踪；
- source_system 与 signed client_id 绑定；
- tenant 由服务端解析并执行一致性校验；
- 不将敏感原文写入 payload。

若 v1 顶层 schema 无法兼容增加字段，短期使用版本化 `payload_ref`，长期发布兼容 v2，不通过自由文本隐式约定。

## 12. Compatibility

- v1 消费者必须忽略未知可选字段；
- 安全关键字段缺失时不能降级为匿名或全租户；
- Header 名称、RPC metadata 和事件字段有映射表；
- operation/tenant/provenance 不得被下游静默重写；
- 版本升级使用双读、兼容窗口和契约测试。

## 13. Acceptance criteria

1. 前端传入另一个租户 ID 不能改变服务端上下文；
2. 同一用户在不同租户中有不同 operation 权限；
3. Agent 事件能查到 initiator、executor、grant 和 workflow；
4. 异步重试能通过 causation/correlation 还原；
5. grant 过期或 revocation epoch 变化后执行被拒绝；
6. 业务 Trace 与 audit timeline 可通过 operation_id 关联；
7. 不存在上下文时关键写操作 fail closed。
