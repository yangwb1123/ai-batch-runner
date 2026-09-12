# Plugin Manifest Contract v1

> **状态：DRAFT / PROPOSED**  
> **契约版本：`plugin-manifest.v1`**

## 1. Purpose

描述模块/插件如何被 Profile 选择、验证、构建、启动、扩缩容、停用和升级。

插件不是任意代码加载器；插件必须在声明的契约和 Capability 边界内运行。

## 2. Plugin kinds

```text
static-module       可信模块，编译期静态链接
precompiled-module  已在二进制内，通过固定 slot 启停
external-plugin     独立进程，Unix Socket gRPC；远程生产使用 mTLS
frontend-bundled    前端构建期 registry
frontend-isolated   受控 iframe/micro，来源和签名明确
```

不使用 Go `.so` 作为跨项目通用插件 ABI。

## 3. Manifest

```yaml
plugin:
  id: string
  version: semver
  contract_version: string
  kind: static-module|precompiled-module|external-plugin|frontend-bundled|frontend-isolated
  profiles: [minimal, full, production]
  owner_project: string
  provides: []
  requires: []
  optional_requires: []
  conflicts: []
  entrypoint:
    transport: in-process|unix-grpc|mtls-grpc|bundled|iframe
    endpoint: string|null
    protocol: string|null
  routes: []
  events:
    publishes: []
    consumes: []
  commands: []
  config_schema: string|null
  migrations: []
  health:
    readiness: string|null
    liveness: string|null
    startup_timeout_seconds: integer
  security:
    capabilities: []
    tenant_scope: tenant|platform|explicit-grant
    signature_required: boolean
    mtls_required_in_production: boolean
    filesystem: []
    network_allowlist: []
  resource_limits:
    cpu: string
    memory_mb: integer
    workers: integer
    max_request_bytes: integer
    max_inflight: integer
  failure_policy: fail_closed|durable_async|local_queue|best_effort_async|degrade
  compatibility:
    host_versions: []
    contract_versions: []
  provenance:
    source_repository: string
    source_revision: string
    build_id: string
    digest: string
    signature: string|null
```

## 4. Capability composition

```text
profile
  → selected plugins
  → required capability closure
  → version compatibility
  → conflict/cycle validation
  → generated registry
  → plugin lock + inventory + SBOM
  → build/startup checks
```

`provides` 是能力声明，不代表当前调用者自动获得授权。授权仍由服务端 PEP/PDP 执行。

## 5. Profile rules

### minimal

- 核心 Kernel 和必要静态模块；
- 本地/轻量存储；
- 基础日志和健康检查；
- 不要求 Kafka、WORM、完整 Plugin Host 或外部 Agent；
- 不加载生产专用依赖。

### full

- 完整内置业务模块；
- 可选 audit、Vault、organization、notification adapter；
- 可启用受控 external typed plugin；
- 支持完整流程、共享资产和治理组合。

### production

- 所有 production plugin 必须有 digest/signature/provenance/SBOM；
- 外部 RPC 必须 TLS/mTLS；
- 必须配置持久化、Outbox、限流、观测和恢复；
- 禁止 dev auth、明文敏感配置、未批准插件和任意脚本；
- readiness 必须检查 required dependencies；
- `production` 不能只是 `full` 的别名。

## 6. Contract rules

- Plugin 只能依赖 declared `requires`；
- 依赖的是契约能力，不是具体兄弟项目内部包；
- 不直接访问其他插件数据库；
- 不共享 ORM entity 或隐式全局 singleton；
- 领域变化通过 typed command 或 versioned event；
- 每个命令支持 deadline、idempotency 和 tenant consistency；
- 每个 event 支持 schema version、causation 和 correlation；
- 插件不能修改 Kernel 的 tenant/authn/authz 安全不变量。

## 7. External plugin protocol

启动握手至少返回：

```yaml
handshake:
  plugin_id
  plugin_version
  contract_version
  profile
  capabilities
  digest
  protocol_version
  readiness
```

每次调用需要：

```text
tenant context
principal/delegation
capability grant (if required)
operation/correlation/trace IDs
deadline
idempotency key
```

外部插件必须支持：

```text
health
ready
heartbeat
quiesce
drain
shutdown
```

## 8. Lifecycle

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

插件禁用、撤销或升级时：

1. 停止发放新的 Capability；
2. 拒绝新任务；
3. revoke/fence 活跃执行；
4. drain 已有请求；
5. 保留业务数据和审计证据；
6. 使用兼容版本或迁移方案处理存量数据。

## 9. Failure policies

| 策略 | 语义 |
|---|---|
| `fail_closed` | 安全、授权和高风险能力失败即拒绝 |
| `durable_async` | 本地可靠接收后异步投递，不能静默丢失 |
| `local_queue` | 保留本地任务并有限重试 |
| `best_effort_async` | 通知等非核心能力失败不阻塞主流程 |
| `degrade` | 非核心能力不可用，返回受控降级 |

失败策略必须在 Manifest 声明，不能由调用方任意覆盖。

## 10. Lock and inventory

组合构建产物必须生成：

```text
plugin-lock.json
modules.lock.json
capabilities.json
inventory.json
sbom.spdx.json
```

lock 至少锁定：

```text
plugin id/version
contract version
digest/signature
profile
dependency closure
configuration digest
build provenance
```

未在 lock 中的生产插件不得启动。

## 11. Elasticity

- API 插件保持无状态；
- Worker 插件通过队列独立扩展；
- tenant/aggregate 作为分区键；
- 每插件有连接池、线程、内存、请求和队列上限；
- 外部插件故障由 timeout、circuit breaker、bulkhead 隔离；
- 高负载时背压，禁止无限排队；
- 热租户可独立分片或迁移；
- 停机升级使用 quiesce/drain，不直接杀死正在提交的关键操作。

## 12. Frontend plugin rules

- bundled plugin 必须在构建 manifest 中声明 route、capability 和资源；
- iframe/micro plugin 必须有来源 allowlist、CSP、版本、完整性和隔离；
- 前端插件不能授予后端权限；
- `/system/routes` 只能投影已编译且后端可用的 route；
- 加载失败应隔离到单个插件，不拖垮主壳；
- 禁止未签名远程脚本和任意 `eval`。

## 13. Acceptance criteria

1. Profile 解析能拒绝缺失依赖、冲突和循环；
2. 生产插件没有签名/digest/provenance 时启动失败；
3. 插件无法通过共享表绕过领域边界；
4. 外部插件支持握手、健康、超时和 drain；
5. 插件停用会撤销能力但保留数据和审计证据；
6. minimal 不需要完整生产基础设施；
7. 实际 inventory 与 lock、SBOM 和运行 capabilities 一致；
8. UI route 不会因为数据库记录存在而加载不存在的组件。
