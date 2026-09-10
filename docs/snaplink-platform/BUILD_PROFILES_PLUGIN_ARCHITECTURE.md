# 跨项目构建 Profile 与插件架构

> **状态：提案（PROPOSED）**
>
> 日期：2026-09-07  
> 适用范围：snaplink、aero-vault、snaplink-audit-governance、aero-im、sverpweb-stable、snaplink-console、iris-ui 及未来的组合仓库。  
> 关联文档：[`COMBINATION_MODEL.md`](./COMBINATION_MODEL.md)、[`MULTI_REPO_OPERATIONS.md`](./MULTI_REPO_OPERATIONS.md)、[`decisions/04-composition.md`](./decisions/04-composition.md)。

本文回答三个问题：

1. 为什么最小实现不应一开始就引入完整插件系统；
2. 如何让完整版本和生产版本具备可裁剪、可扩展、可审计的构建能力；
3. 如何让后端 Profile、前端构建、`/system/routes`、用户权限和商业/社区交付保持一致。

本文是架构提案，不代表所有项目已经完成改造。代码事实与提案分别标注为 **Verified** 和 **PROPOSED**。

---

## 1. 摘要

采用渐进式组合架构：

```text
                    ┌────────────────────────────┐
                    │  Profile Resolver           │
                    │  minimal/full/production    │
                    └──────────────┬─────────────┘
                                   │
                    ┌──────────────▼─────────────┐
                    │  静态模块集合               │
                    │  core + selected modules    │
                    └──────────────┬─────────────┘
                                   │ build
                    ┌──────────────▼─────────────┐
                    │  可验证构建产物              │
                    │  inventory + lock + SBOM    │
                    └──────────────┬─────────────┘
                                   │ runtime
       ┌───────────────────────────▼───────────────────────────┐
       │  Capability / Plugin Host                              │
       │  only for full/production; minimal keeps this optional  │
       └───────────────┬───────────────────────┬────────────────┘
                       │                       │
                precompiled module       external plugin
                fixed route slot          typed RPC/mTLS
```

核心原则：

- **minimal 是简单产品，不是 full 的残缺复制品。**
- **full 是完整内置能力的组合。**
- **production 是 full 加生产约束，不只是多几个菜单。**
- **插件不是任意代码加载器。** Go `.so` 不作为通用插件机制；不可信或重依赖功能优先使用独立进程 + 类型化协议。
- **前端不负责安全授权。** 前端只消费构建能力、后端能力和用户权限的交集；最终授权始终由后端执行。
- **Profile 解决交付边界，Plugin 解决扩展边界，License 解决商业授权，`/system/routes` 解决菜单投影。**

---

## 2. 当前状态

### 2.1 组合边界（Verified）

现有组合模型已经把各仓库定义为独立产品，通过协议组合，而不是一个必须锁步发布的超级仓库：

| 产品 | 主要职责 |
|---|---|
| `snaplink` | 认证、OAuth/OIDC、授权、管理面 |
| `aero-vault` | 文件、对象、分享、文件生命周期 |
| `aero-im` | 消息、通知、会话相关通信 |
| `snaplink-audit-governance` | 审计事件、完整性、治理和归档 |
| `snaplink-console` | Snaplink 管理端及组合管理入口 |
| `sverpweb-stable` | ERP 业务 API、Web 应用、系统导航和路由定义 |
| `iris-ui` | UI 组件、Admin Layout、NavMenu 和插件消费基础设施 |

跨项目的职责、契约和组合验证规则以 [`COMBINATION_MODEL.md`](./COMBINATION_MODEL.md) 为准。

### 2.2 `sso-server` 的现状（Verified）

`sso-server` 已经实现了 NGINX 风格的正向构建 Profile 和模块清单机制：

- `prototype`：最小 OAuth/PKCE 原型；
- `minimal`：在 prototype 上增加 OIDC、ID Token、UserInfo、Logout 和 tracing；
- `full`：完整 stock `sso-server` 组合；
- `standard`：历史兼容组合；
- `standard-kafka`：standard 加 Kafka 审计模块；
- `production`：当前是 `full` 的兼容别名，不是独立的代码组合；
- `billing`：独立服务，不属于 SSO Profile 层级。

现有机制包括：

- Profile 继承和模块解析；
- capability dependency closure；
- 生成模块注册代码；
- `modules.lock.json`、构建输入和嵌入式 inventory；
- `modules` / `modules --json` 检查构建身份；
- 外部插件 Host 的类型化、认证、生命周期和 provenance 设计。

`sso-server` 的插件设计明确区分：

```text
cold module             编译期静态选择并链接
hot precompiled module  已在二进制内，通过固定 slot 启停
hot external module     独立进程，通过类型化 RPC 接入
```

现有设计明确不支持任意 Go `.so` 动态加载。详见 Snaplink 仓库的 `docs/plugin-system.md` 和 ADR-0009。

### 2.3 其他项目现状（Verified / 部分提案）

当前其他项目可以独立构建和部署，但尚未形成与 `sso-server` 完全一致的跨语言 Profile/Plugin 契约：

- Go 服务已有清晰的命令入口和可选集成，但需要补充统一的 Profile、模块 inventory 和插件 manifest；
- `aero-im` 的 Cargo features 适合作为 Rust 侧构建边界，但需要明确 minimal/full/production 的能力语义；
- `sverpweb-stable` 已有数据库驱动的系统路由定义和 CRUD 页面，尚未把路由定义与前端插件能力清单统一；
- `snaplink-console` 和 `iris-ui` 可以通过编译期 registry、能力发现和路由投影接入，但不应下载并执行任意 Dart/JavaScript 代码。

上述四项是现状判断和改造方向，不代表对应功能已经完成。

---

## 3. 目标与非目标

### 3.1 目标

1. 每个后端可独立生成 `minimal`、`full`、`production` 产物；
2. 每个产物能自描述 Profile、模块、能力、锁文件摘要和构建版本；
3. minimal 不被完整插件 Host、重型依赖和外部集成污染；
4. full 能通过静态模块或受控外部插件装配可选能力；
5. production 强制生产安全、审计、可观测性和依赖策略；
6. 前端能够根据构建能力、后端能力、用户权限和路由配置进行一致投影；
7. 社区贡献者可以低成本构建和测试核心；
8. 商业扩展不需要 fork 核心代码；
9. 跨仓库只共享协议和版本化契约，不共享数据库和内部实现。

### 3.2 非目标

本提案不做以下事情：

- 不把所有代码拆成运行时动态插件；
- 不通过前端隐藏菜单实现安全或商业授权；
- 不引入一个跨所有项目的共享数据库；
- 不要求所有项目锁步发布；
- 不把 `production` 简化成“打开全部 feature flag”；
- 不为 minimal 引入完整的生成代际、远程插件监督器和所有生产依赖；
- 不直接复制其他开源项目的代码、协议或许可证实现；
- 不在没有真实客户/部署压力的情况下为每个小功能创建插件。

---

## 4. Profile 语义

### 4.1 三个统一 Profile

| Profile | 构建目标 | 运行语义 | 插件能力 |
|---|---|---|---|
| `minimal` | 最小可用核心 | 单体、低依赖、易开发和易贡献 | 无 Plugin Host 或仅保留极小接口 |
| `full` | 完整内置能力 | 组合所有已支持模块和适配器 | 静态 registry，可启用受控外部插件 |
| `production` | 生产交付 | full + 持久化、安全、审计、观测和运维约束 | 签名、版本、mTLS、生命周期和失败策略强制 |

`production` 可以与 `full` 共享绝大多数代码，但不能仅用菜单或配置伪装成生产版本。至少应有可验证的生产策略：

- 必须配置持久化后端；
- 必须配置 TLS/mTLS 或明确的边界代理；
- 必须启用审计和关键操作记录；
- 必须暴露 readiness、metrics、trace 和版本信息；
- 禁止 dev auth、明文密钥、未签名插件和任意外部脚本；
- 启动时校验 `required_capabilities` 和 Profile policy。

### 4.2 兼容 Profile

迁移期间可以保留项目本地的兼容 Profile：

```text
standard          历史默认组合
standard-kafka    standard + Kafka
prototype         minimal 之前的更小实验组合
```

兼容 Profile 不必强行改名，但必须在 `modules --json` 中明确其身份，避免把 `standard` 错报为 `production`。

### 4.3 Profile 的真实边界

Profile 至少需要影响以下内容中的一部分，才算真实构建边界：

- 编译进二进制的包/模块；
- 外部依赖和容器镜像；
- 路由注册和 capability registry；
- 默认配置和启动校验；
- 产物 inventory、lock 和 SBOM；
- Profile-specific 测试和部署门禁。

仅仅在 YAML 里把路由设为 `enabled: false`，不能证明依赖被排除，也不能作为物理构建隔离证据。

---

## 5. 模块和插件分类

### 5.1 Kernel（不可移除内核）

所有 Profile 必须保留的内容：

- 进程生命周期和配置解析；
- 核心身份、租户和请求上下文；
- 安全边界、认证/授权不变量；
- 错误模型、请求 ID、trace ID；
- 基础健康检查和关闭流程；
- 数据迁移和版本兼容的最低协议；
- 资源限制和拒绝策略。

Kernel 不允许被插件替换来绕过安全规则。

### 5.2 Cold module（编译期模块）

Profile 选择后静态编译并在启动时注册：

```text
core + audit-adapter + storage-s3 + messaging-nats
```

适用于：

- 有较重依赖的集成；
- 需要减少攻击面和镜像体积的功能；
- 不需要不停机替换的业务模块；
- 需要通过 SBOM/lock 证明是否存在的模块。

### 5.3 Hot precompiled module（预编译热模块）

代码已在二进制内，只允许在固定路由或固定 capability slot 中启停。必须具备：

- generation；
- request/dependency/background lease；
- readiness；
- graceful drain；
- 反向依赖关闭；
- 受限的 transition audit。

不能让运行时配置凭空创建一个源码中不存在的模块。

### 5.4 Hot external module（外部插件）

不进入主进程地址空间，通过类型化协议运行：

```text
host ── Unix domain socket / mTLS ── plugin worker
```

适用于：

- 第三方集成；
- AI/embedding/搜索等重依赖能力；
- 可能崩溃或升级节奏不同的组件；
- 社区和合作伙伴开发的扩展。

不允许提供通用的：

```text
Execute(any)
```

每个数据面必须有固定方法和最小输入，例如：

```text
DeliverAuditBatch
EvaluatePolicy
SendNotification
ResolveStorageObject
```

### 5.5 UI plugin（前端插件）

UI 插件不是任意远程 JavaScript/Dart 下载器，而是以下两类之一：

1. **bundled plugin**：在前端构建时编译进 registry；
2. **trusted micro/iframe plugin**：来自签名、版本和来源均受控的外部地址。

前端插件可以贡献：

- 路由；
- 菜单；
- 页面组件；
- API client；
- 权限声明；
- i18n；
- 设置项和能力说明。

---

## 6. 统一模块 Manifest

每个模块或插件应提供版本化 manifest。建议字段如下：

```json
{
  "id": "audit-governance",
  "version": "1.0.0",
  "contractVersion": "v1",
  "kind": "external-plugin",
  "profiles": ["full", "production"],
  "provides": ["audit.write", "audit.read"],
  "requires": ["identity.context", "event.envelope.v1"],
  "entrypoint": {
    "transport": "unix-grpc",
    "socket": "audit.sock"
  },
  "security": {
    "signatureRequired": true,
    "mtlsRequiredInProduction": true,
    "privateKeysAllowed": false
  },
  "failurePolicy": "durable_async",
  "ui": {
    "mode": "bundled",
    "routes": ["system/audit"],
    "permissions": ["audit.event.read"]
  }
}
```

### 6.1 Manifest 校验

构建或安装前必须拒绝：

- 未知模块 ID；
- 重复 capability provider；
- 缺失 required capability；
- 版本不兼容；
- Profile 不允许的模块；
- 生产环境未签名插件；
- 未声明的入口或传输；
- 任意 shell/template/脚本表达式；
- 试图访问私钥或未授权租户数据的插件。

### 6.2 Capability 依赖

能力解析采用正向选择：

```text
profile
  → selected modules
  → required capabilities closure
  → conflict/cycle validation
  → generated registry
  → lock + inventory
  → build
```

解析失败应发生在编译前，而不是服务启动半小时后才发现缺少依赖。

---

## 7. 后端项目落地映射

以下是建议，不代表已经全部实现。

### 7.1 snaplink

`sso-server` 作为参考实现：

- 保留现有 `prototype/minimal/full/standard`；
- 逐步把 `production` 从 `full` 兼容别名升级为生产策略；
- 继续使用静态模块和外部类型化插件；
- 保持 OAuth/OIDC、密钥、token、OP session 和 primary audit sink 为 cold/kernel；
- 任何外部身份插件不得获取签名私钥和原始 bearer token。

### 7.2 aero-vault

候选 Profile：

```text
minimal:
  本地文件/元数据核心、基础授权、基础审计日志

full:
  S3/MinIO、缩略图、AI、杀毒、分享、消息和治理适配器

production:
  durable storage、TLS/mTLS、审计 outbox、限流、备份、观测、插件签名
```

删除、分享、权限和文件完整性属于核心生命周期；对象存储、AI、杀毒和通知应通过端口/适配器接入。

### 7.3 snaplink-audit-governance

候选 Profile：

```text
minimal:
  HTTP/gRPC 核心、规范化事件、SQLite/本地开发 sink

full:
  PostgreSQL、NATS/Kafka、projector、exporter、outbox relay

production:
  TLS/mTLS、哈希链、归档、保留策略、恢复演练、容量和告警门禁
```

审计主链、去重、完整性和事件规范属于 kernel；Kafka、对象归档、外部 SIEM 属于模块。

### 7.4 aero-im

Rust 侧使用 Cargo features 或独立 composition root：

```bash
cargo build --release --no-default-features --features minimal
cargo build --release --no-default-features --features full
cargo build --release --no-default-features --features production
```

候选划分：

- minimal：核心消息、会话和基础存储；
- full：NATS、Redis、推送、搜索、审计适配器；
- production：TLS、租户限流、持久化、观测、恢复和容量约束。

不要把所有 Cargo crate 都做成动态插件；编译期 feature 足以解决大部分 Rust 依赖裁剪问题。

---

## 8. 前端项目落地映射

### 8.1 sverpweb-stable

建议增加编译期 Profile：

```bash
pnpm build --mode minimal
pnpm build --mode full
pnpm build --mode production
```

生成：

```text
apps/web/src/generated/build-profile.ts
apps/web/src/generated/plugin-registry.ts
apps/web/dist/capabilities.json
```

插件 registry 示例：

```ts
export interface FrontendPlugin {
  id: string
  capabilities: string[]
  routes: RouteRecordRaw[]
  menus: MenuContribution[]
  permissions: string[]
  mount(context: PluginContext): void
}
```

构建时决定哪些组件进入 bundle；运行时只决定是否显示和启用。

### 8.2 snaplink-console

Flutter 使用：

```bash
flutter build web \
  --dart-define=BUILD_PROFILE=minimal
```

建议生成：

```dart
const buildProfile = String.fromEnvironment(
  "BUILD_PROFILE",
  defaultValue: "full",
);

const compiledCapabilities = <String>{
  "identity.users.read",
  "identity.clients.write",
};
```

生产构建不能执行远程下载的任意 Dart 代码。外部 UI 只能通过受控 iframe/micro frontend 或预编译插件提供。

### 8.3 iris-ui

`iris-ui` 不应拥有业务 Profile，而应提供通用能力：

- `AdminLayout`；
- `NavMenu`；
- 插件菜单 slot；
- 路由节点类型；
- capability-aware UI helper；
- iframe/micro frontend 容器的安全边界。

业务 Profile 由 `sverpweb` 和 `snaplink-console` 决定，避免 UI 基础库被某个业务产品锁死。

---

## 9. `/system/routes` 与插件的关系

`/system/routes` 是**路由目录和权限投影层**，不是代码加载器。

最终可用路由必须满足：

```text
compiled frontend route
  ∩ backend capability
  ∩ user permission
  ∩ system route definition
```

### 9.1 三种路由

| 路由类型 | 要求 |
|---|---|
| `component` | 前端必须已经编译对应组件和 runtime route key |
| `iframe` | URL 必须符合协议、来源和 CSP/allowlist 策略 |
| `micro` | 必须有版本、来源、签名和隔离策略 |

仅在 `/system/routes` 新建一个 `component` 记录，不会自动生成 Vue/Flutter 页面；如果 registry 没有对应组件，应显示为不可用或拒绝启用。

### 9.2 菜单刷新

路由 CRUD 成功后：

1. 后端持久化 route definition；
2. 生成审计事件；
3. 前端重新加载 effective navigation；
4. 重新与编译能力和用户权限求交集；
5. 更新侧边栏和路由守卫。

前端可隐藏未授权功能，但所有写入和敏感读取必须由后端重新鉴权。

---

## 10. 能力发现协议

每个服务建议提供无敏感信息的能力接口：

```http
GET /api/system/capabilities
```

返回：

```json
{
  "service": "aero-vault",
  "version": "1.4.0",
  "profile": "production",
  "contractVersion": "v1",
  "capabilities": [
    "vault.file.read",
    "vault.file.write",
    "vault.share.create"
  ],
  "plugins": [
    {
      "id": "storage-s3",
      "version": "1.0.0",
      "state": "ready",
      "capabilities": ["vault.storage.object"]
    }
  ]
}
```

能力接口不能泄露：

- 密钥；
- 内部地址；
- 租户列表；
- 插件凭据；
- 详细失败堆栈。

生产环境必须区分：

```text
compiled      编译进去了
configured    配置存在
ready         依赖就绪
authorized    当前调用者被允许
```

不能因为 `compiled=true` 就认为用户可以使用该能力。

---

## 11. 插件生命周期和失败策略

### 11.1 生命周期

```text
DISCOVERED
   ↓
VALIDATED
   ↓
STARTING
   ↓
READY
   ↓
ACTIVE
   ↓
QUIESCING
   ↓
DRAINING
   ↓
STOPPED
```

外部插件握手至少需要：

- protocol version；
- module id/version；
- profile；
- capability list；
- binary digest/provenance；
- readiness；
- heartbeat；
- quiesce；
- shutdown。

### 11.2 失败策略

| 能力 | 推荐策略 |
|---|---|
| 授权判断 | `fail_closed` |
| 主要审计写入 | `durable_async`，不能静默丢失 |
| 通知 | `best_effort_async`，保留重试和状态 |
| 缩略图/搜索 | `degrade`，不阻断核心事务 |
| 外部存储 | 按读写风险分别定义，不能默认 fail-open |
| UI 扩展 | 隔离失败，保留主应用可用 |

禁止把所有插件都统一成 fail-open 或 fail-closed；失败策略必须是 capability 的契约。

---

## 12. 安全模型

### 12.1 进程和权限隔离

插件默认不允许访问：

- OAuth 私钥；
- refresh token；
- 未脱敏的凭据；
- 非本租户数据；
- 任意文件系统；
- 任意 shell/网络目标。

插件通过最小 Capability Grant 获取：

```text
plugin id
module version
tenant scope
allowed capabilities
expiry
resource limits
transport identity
```

### 12.2 供应链

生产插件必须有：

- SHA-256 digest；
- 签名 provenance；
- 来源仓库/构建流水线；
- Profile 绑定；
- 依赖锁文件；
- SBOM；
- 撤销和拒绝列表。

### 12.3 前端安全

前端能力只影响 UX，不是安全边界：

```text
隐藏菜单 ≠ 禁止调用 API
```

后端必须独立验证：session、tenant、scope、role、resource relation 和 capability。

---

## 13. 社区和商业化策略

### 13.1 社区原则

- minimal 默认可运行、低依赖、低门槛；
- full 的 Profile、manifest 和插件协议公开；
- 社区可以自行构建 full，不依赖商业服务；
- 核心安全和基础能力不人为残缺；
- PR 默认只跑 minimal 核心门禁，full/production 可在 CI 夜间或发布门禁执行；
- 插件 API 采用语义化版本和兼容窗口；
- 插件行为和权限必须有文档和测试。

### 13.2 商业化边界

可以将能力分成：

```text
构建 Profile       决定交付哪些组件
License/Entitlement 决定租户是否有权使用
Plugin              决定如何扩展
托管/支持           决定商业服务内容
```

不建议把“商业版”实现成一个社区无法验证的黑盒。更健康的方式是：

- 核心和扩展协议开放；
- 企业插件可以单独授权；
- 托管、支持、合规交付和运维服务商业化；
- 后端 License 校验，而不是前端删按钮。

### 13.3 维护成本控制

第一阶段只把边界清晰且依赖较重的功能插件化：

```text
audit / object-storage / AI / external-message / SIEM
```

不要把核心登录、权限、数据一致性和简单业务逻辑全部拆成插件。

---

## 14. 版本和兼容性

四种版本必须分别管理：

| 版本 | 说明 |
|---|---|
| Profile version | minimal/full/production 的组合定义 |
| Module version | 模块自身发布版本 |
| Contract version | RPC/API/Event/Manifest 协议版本 |
| Product version | 整个服务或前端发行版本 |

兼容矩阵至少包含：

```yaml
service: aero-vault
profile: production
module: storage-s3
module_version: 1.4.x
contract_version: audit-event.v1
host_versions: [1.3.x, 1.4.x]
frontend_versions: [2.0.x]
```

升级规则：

1. 先检查 capability 和 contract closure；
2. 再检查模块版本窗口；
3. 再执行 schema/migration；
4. 通过 readiness 后才切换；
5. 旧版本保持兼容窗口；
6. 不兼容升级必须生成明确的 migration plan。

---

## 15. 构建和发布接口

### 15.1 Go

建议统一提供：

```bash
make build-minimal
make build-full
make build-production
make modules-check
make modules-smoke

./service version
./service modules --json
```

生成目录建议：

```text
dist/modules/<profile>/
  service
  modules.lock.json
  inventory.json
  capabilities.json
  sbom.spdx.json
  evidence/
```

### 15.2 Rust

```bash
cargo build --release --no-default-features --features minimal
cargo build --release --no-default-features --features full
cargo build --release --no-default-features --features production
```

同时生成：

```text
build/profile.json
build/capabilities.json
build/plugin-lock.json
```

### 15.3 TypeScript/Bun

```bash
pnpm build --mode minimal
pnpm build --mode full
pnpm build --mode production
pnpm capabilities:check
```

禁止用生产运行时 `eval`、任意远程 `import()` 或未签名脚本来模拟插件系统。

### 15.4 Flutter

```bash
flutter build web --release \
  --dart-define=BUILD_PROFILE=minimal

flutter build web --release \
  --dart-define=BUILD_PROFILE=production
```

构建产物必须写入 Profile 和 compiled capability，便于 console 与后端能力对账。

---

## 16. 迁移计划

### Phase 0：契约冻结

- [ ] 定义 Profile、Module、Plugin、Capability、Manifest schema；
- [ ] 定义 `capabilities` 和 `modules --json` 输出；
- [ ] 定义失败策略和安全边界；
- [ ] 将现有 `sso-server` 机制作为参考，不直接复制实现。

### Phase 1：Go 服务试点

优先选择 `aero-vault` 或 `snaplink-audit-governance`：

- [ ] minimal composition root；
- [ ] full static registry；
- [ ] production startup policy；
- [ ] 模块 inventory/lock；
- [ ] 一个外部类型化插件（建议 audit 或 storage）；
- [ ] 无插件、错误插件、版本冲突和超时测试。

### Phase 2：Rust 服务

- [ ] 将 Cargo features 映射到 capability；
- [ ] 区分编译期 feature 与运行期 plugin；
- [ ] 生成 Profile inventory；
- [ ] 增加生产 TLS/观测/限流门禁。

### Phase 3：Web 和 Flutter

- [ ] 编译期 frontend plugin registry；
- [ ] 后端 capability discovery；
- [ ] `/system/routes` 与 registry 做交集；
- [ ] component/iframe/micro 三类路由验证；
- [ ] 菜单隐藏和 API 鉴权分离测试。

### Phase 4：组合仓库

计划中的组合仓库负责：

- compose 拓扑；
- Profile 组合；
- compatibility matrix；
- 真实版本 E2E；
- 契约测试；
- 发行包和部署证据。

它不承载业务代码，也不替代各产品仓库的单仓库 CI。

---

## 17. 验收门禁

### 17.1 Minimal

- [ ] 核心能力可独立启动；
- [ ] 不依赖完整 Plugin Host；
- [ ] 不拉入未选择的重型依赖；
- [ ] `modules --json` 能证明编译集合；
- [ ] 最小文档和测试可在普通开发机完成；
- [ ] 禁止通过 minimal 绕过安全内核。

### 17.2 Full

- [ ] 所有声明模块有明确 provider；
- [ ] capability closure 无缺失、冲突和循环；
- [ ] 静态模块 registry 可复现；
- [ ] 外部插件握手、健康和关闭流程可测试；
- [ ] 组合 E2E 通过；
- [ ] 前端能力和后端能力有对账结果。

### 17.3 Production

- [ ] 无 dev auth 和未签名插件；
- [ ] TLS/mTLS、审计和持久化策略已验证；
- [ ] readiness/metrics/trace 可用；
- [ ] 插件有 digest、provenance、SBOM；
- [ ] 失败策略和恢复演练有证据；
- [ ] 兼容矩阵和升级/回滚方案存在；
- [ ] License 不会被前端绕过，后端重新鉴权。

### 17.4 Community

- [ ] minimal 构建命令公开且稳定；
- [ ] full Profile 和插件协议可由社区构建；
- [ ] 贡献者无需安装全部商业/生产依赖；
- [ ] 插件 API 有文档、示例和兼容窗口；
- [ ] 核心能力没有通过隐藏代码制造人工残缺；
- [ ] 许可证和第三方依赖边界经过审查。

---

## 18. 主要风险和决策点

| 风险/决策 | 当前建议 |
|---|---|
| Profile 数量爆炸 | 先固定 minimal/full/production，其他作为模块参数 |
| Go `.so` ABI 风险 | 不采用，使用静态模块或外部 typed RPC |
| Plugin Host 污染 minimal | 独立 composition root，minimal 不导入 Host |
| 前端动态代码风险 | 优先 bundled registry，外部只允许签名 iframe/micro |
| 商业版造成社区不信任 | 核心和协议开放，商业化放在插件/托管/支持/合规交付 |
| 生产 Profile 只是 full 别名 | 保留兼容别名，但逐步加入真实生产 policy |
| 跨仓库版本失控 | suite compatibility matrix + contract version |
| 插件故障拖垮主服务 | 独立进程、超时、资源配额、失败策略和 drain |
| `/system/routes` 生成不存在的页面 | registry 与 route definition 做启动/运行时校验 |

---

## 19. 参考项目

以下项目用于借鉴设计思想，不直接复制代码或许可证：

| 项目 | 参考点 |
|---|---|
| OpenTelemetry Collector | core/contrib/custom distribution 和组件构建器 |
| Caddy | 静态模块、`xcaddy`、模块 registry |
| NGINX/OpenResty | 构建期模块、核心与扩展边界 |
| Keycloak | 身份 Provider SPI、认证扩展和事件监听器 |
| HashiCorp Vault | 外部插件进程、版本握手、签名和生命周期 |
| Envoy | 类型化扩展点、扩展工厂和动态配置 |
| Grafana | 前后端插件、签名、插件目录和能力声明 |
| Kong / Apache APISIX | 网关插件、配置和执行顺序 |
| VS Code | Manifest、扩展宿主、权限和社区生态 |
| Backstage | 前后端插件与应用组合 |
| WordPress / Odoo | 社区版、模块生态和商业扩展治理 |

最优先研究顺序：

```text
OpenTelemetry Collector → Caddy → Keycloak → Vault → Grafana/Backstage
```

---

## 20. 未决问题

以下问题在开始跨仓库实现前需要人工裁决：

1. `production` 是否从 `full` 兼容别名升级为独立 Profile policy；
2. 外部插件协议采用 gRPC、Unix Socket gRPC，还是统一 protobuf transport；
3. Manifest 放在每个仓库，还是由未来的 suite 仓库汇总；
4. 能力命名使用 `domain.resource.action` 还是同时保留旧 scope 名；
5. 社区是否允许构建 full 的全部模块，还是仅限制商业插件和托管能力；
6. 前端 micro plugin 是否需要独立签名服务和版本目录；
7. 第一试点选择 `aero-vault` 还是 `snaplink-audit-governance`；
8. 哪些能力必须 fail-closed，哪些能力可以 durable-async 或 degrade。

在这些问题没有裁决前，不应同时修改所有仓库的生产代码。建议先完成 Phase 0 的 schema、manifest 示例和一个 Go 服务试点。
