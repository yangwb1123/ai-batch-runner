# 实施批次分派 — Audit 平台实现前置任务清单（COMPOSE-2026-017 之后）

> **来源**：`v2/revision-plan.md`（契约修订规划）、`v2/audit-event-registry-v2.md`、
> `v2/scope-matrix-v2.md`、`v2/audit-ingestion-design-v2.md`（接入设计 v2），
> 全部任务与验收断言追溯到五份对抗审查（security/distributed/database/sre/qa/
> protocol）的已验证证据。
> **基线**：COMPOSE-2026-017（当前 compose 部署：sink dev-auth ON、两个
> producer 连接器 flag-off、sink 无 self-audit/read-audit、IdP token 缺
> tenant_id/roles —— gate 报告 Check 1 的已验证现实）。
> **批次模型**：契约按目标态批准（revision-plan §判断框架）；本清单是
> **各仓实施任务**，逐批挂验收断言门禁，不阻塞契约批准，阻塞生产 cutover。
> **两处归属校正**（按已验证代码）：①"dev-token 关闭"（S1）在 **sink**
> 仓库（`cmd/audit-api/main.go`），不在 IdP；②"discovery 元数据真实化"
> （F-3）在 **snaplink (IdP)** 仓库（`cmd/server/routes.go`），不在 sink。
> 下文按真实归属分派。

## 批次定义

| 批次 | 仓库 | 内容 | 前置 |
|---|---|---|---|
| **B1** | snaplink-audit-governance (sink) | 信任姿态（S1）+ 存储加固（F-01）+ self/read-audit（F13/F-06）+ 拓扑收敛 | COMPOSE-2026-017 |
| **B2** | aero-id | in-tx 审计（F5）+ 单一投递（F6）+ 401 失效（F8）+ 稳定 event_id（F4）+ 重放（F9）+ 激活门（F-03） | B1 |
| **B3** | aero-vault | 死信终态（F3）+ Ready 解耦（H1）+ 确定性 fact ID（F4）+ metrics（H6）+ scope 验证（F2 收尾）+ 激活门（F-03） | B1 |
| **B4** | snaplink (IdP) | claim 契约（S2）+ scope registry（P0-3）+ discovery 真值化（F-3）+ 端点加固（F-4/5/6/7/13）+ 治理连接器 | B1（联合验收：T-1.2） |
| **B5** | aero-im | outbox + in-tx 写入 + Rust relay + moderation 优先级 + scope 配给 | B1, B4 |
| **B6** | snaplink-console | 审计时间线读路径接入（F-06） | B1（read self-audit） |

> **信任路径联合门禁**：B1-1（dev-auth 关闭）与 B4-1（claim 契约）必须
> 同时完成才能跑 T-1.2 跨仓 e2e；二者合并为"真实流量"P0 门禁（revision-plan
> 条件 ①），先于任何生产流量。

---

## snaplink（IdP — dev-token 关闭的 IdP 侧、token claims、scope 注册）

> 注：dev-token 关闭本体在 B1（sink）；本仓承担信任路径的**铸币侧**义务。

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | snaplink (sso-server) | `infrastructure/defaultimpl/issue_payload.go` `buildAccessPayload`（ed25519/ecdsa/rsa 三 issuer 共用）；`token/jwt/generator.go:52-55`；`sso.go:894-960` | Access-token claim 契约落地：`buildAccessPayload` 增加 **`tenant_id`**（client 绑定解析）与 **`roles`**；JOSE header 补 **`kid`**；`iss` 配置化（allowlist，禁 Host 派生）；`/token` 读取并校验 `scope`（RFC 6749 §3.3） | T-8 row (a)：`POST /token`（client_credentials，`resource=audit-governance`）→ 200，header 含 kid，claims 含 iss/aud/scope/client_id/tenant_id/roles；T-1.2 联合 e2e（见 B1-7） | B1（联合验收） |
| 2 | snaplink | `cmd/server/routes.go` token handler + scope 注册存储（client 绑定 allowlist） | Scope registry（P0-3）：注册 scope-matrix-v2 全表（`audit:event:write`、`vault:blob:read/write`、`snaplink:token:validate`、admin scopes…）；未注册 scope 请求 → 400 `invalid_scope` | T-8 row (d)：未注册 scope → 400；按 scope-matrix-v2 配给后 vault/aero-id 端到端无 403（与 B3-5/B2 联合） | B4-1（同批） |
| 3 | snaplink | `cmd/server/routes.go:63-77,663-684`（OIDC discovery）；`routes_test.go:41`（TestOIDCDiscovery 重写） | Discovery 真值化（F-3）：`token_endpoint=/token`；无 `/authorize` 则删除 authorization_endpoint/response_types/authorization_code 广告；`/revoke` 实现或移除；认证方法收敛到 `client_secret_post`（实现 Basic 前不广告）；device grant 广告必须含 `device_authorization_endpoint`；issuer 来自配置 allowlist | T-2：discovery sweep 全绿（每个广告端点 2xx/405，绝不 404）；`metadata.token_endpoint == "/token"`；issuer 非 Host 派生 | 无（可并行） |
| 4 | snaplink | `cmd/server/routes.go:247-300`（/token）、`sso.go:909`（secret 比较）、`/introspect` handler | 端点加固（F-4/F-5/F-6/F-7/F-13）：仅 body 解析（禁 query-string 凭据）；Content-Type 强制 form-urlencoded；introspection 调用方认证（RFC 7662 §2.1）；constant-time secret 比较；cc 响应省略 `refresh_token` + `Cache-Control: no-store` | T-8 rows (b)(c)(e)：query 凭据 → 400、非 form Content-Type → 400、无 refresh_token + no-store；T-9：introspect 无凭据 → 401 | 无（与 1–3 同批） |
| 5 | snaplink | `cmd/` 新 wiring（复用 `audit.Sink`/`BatchSink`/AsyncSink）；`config_audit.go`（audit backend 默认） | 治理连接器（P2 级）：`audit_outbox` 表 + auth.* 行 in-tx 写入（login 失败行 post-commit keyed by request_id，唯一许可的 post-commit 类）+ Go relay（aero-id publisher 模式）+ `auth.token.issue` L1 聚合；stock `audit.backend` 从 memory 改 sqlite/postgres + `hash_chain: true`（F-02/F11） | 重启后 L0 不丢（10 logins → sqlite 10 行）；P2 parity 检查（audit_events vs outbox）；`auth.token.issue` 只出 L1 摘要 | B1, B4-1..4 |

---

## aero-vault（死信状态、Ready 解耦、scope 对齐）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | aero-vault | migration `0039_audit_governance_outbox`（扩展）；`internal/auditgovernance/relay.go` `retryFact` | 死信终态（F3）：outbox 加 status/dead_at 列 + 部分索引；移植 sink outbox 的 `DeliveryError.Permanent` 分类——422/409/tenant mismatch/无效回执 → 终态（≤1 次分类尝试）；5xx/429/网络 → 有界重试 cap 300s；dead 行排除出 claim 与滞后计算 | T-3：注入 422 → 一个投递周期内进入终态；`Ready()` 含 dead 行 = true；批次其余行继续投递 | B1（sink 422/409 语义） |
| 2 | aero-vault | `internal/auditgovernance/runtime.go` `Ready()`；`cmd/server/audit_governance.go:52-59` | Ready 解耦（H1）：移除 maxLag 翻转 readiness；backlog/poison → `degraded` 报告 + **maxLag×0.5（450s）告警**；`OldestPendingAuditGovernance` 排除终态行；读路径降级为超时而非 503 | D1 drill：sink 停 60min → `/readyz` 保持 200；450s 处告警触发；1 条 poison 行不引起重启循环 | B3-1（终态先行） |
| 3 | aero-vault | `internal/auditgovernance/facts.go`（factFromEvent/factFromAudit/factFromGap） | 确定性 fact ID（F4）：`event_id = SHA-256(source \| tenant \| event_type \| origin_kind\|origin_id \| time_bucket)[:32]`（registry v2 §1.9）；gap reconcile/重放复用原 ID | T-4：清除 outbox 行 → reconcile 再生成 → sink `Duplicate` 回执、`QueryEvents` 恰好 1 行 | B1（sink dedup） |
| 4 | aero-vault | `internal/auditgovernance/relay.go`/`runtime.go`（当前 0 处 Observe 调用） | Relay metrics（H6）：attempted/delivered/failed/dead/oldest-age 计数器 | 指标存在并可喂 H2 告警（dead_count>0 15min、lag 增长）；stalled relay 可检测 | B3-1（dead 指标依赖终态） |
| 5 | aero-vault | `internal/auditgovernance/model.go` `RequiredScope`（已验证 = `audit:event:write`）；`token.go` 严格 scope 检查 | scope 对齐（F2 收尾）：**无代码改动**（实现已 enforce write）——保留 `TestTokenSourceRejectsExpandedScope`，补按 scope-matrix-v2 配给的端到端验证 | 矩阵配给（`audit:event:write`）→ 无 403；非 write scope → token-source 拒绝（已有测试保持绿）；grep 型注册表一致性检查绿 | B4-2（IdP scope registry 落地后验证） |
| 6 | aero-vault | `internal/config/config_audit_governance.go`（默认 false）；部署清单（compose/helm） | 连接器显式激活（F-03）：rollout runbook——`AUDIT_GOVERNANCE_ENABLED=true` + `AUDIT_GOVERNANCE_BINDINGS_FILE`；首个事件验证 | 空 bindings + enabled → boot 报错（fail-closed，已验证路径）；启用后首个 `file.delete.admin` 事件到达 sink 并被查询到 | B1, B4-1, B3-1..4 |

---

## aero-id（in-tx 审计记录）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | aero-id | `service/account.go:155,184`、`callback.go:160,221,247`、`audit_hold.go:59,74`、`legacy_cutover.go:88`（8 个 post-commit 调用点） | in-tx 审计（F5）：全部移入业务事务（`AppendInTx`，repository 模式）；无法 in-tx 的路径必须失败可观测（`ObserveAuditIngestFailure` + 告警），**`_ =` 静默丢弃明令禁止**（grep 门禁） | T-5：mock `Record` 失败 → counter 递增或操作失败；全仓无 `_ = s.audit.Record(...)` 调用点 | 无（独立，可先于 B1） |
| 2 | aero-id | `audit_ledger.go` `Record`（inline `publisher.Publish`） | 单一投递路径（F6）：删除 inline publish 或降级 best-effort（独立短超时）；outbox worker 为唯一投递路径；sink dedup 保留为后盾 | 同一事件仅一条投递路径；sink 慢时请求路径无 5s 延迟（F6 drill：p99 不随 sink 延迟漂移）；sink 收到 2× 负载消除 | B2-1 |
| 3 | aero-id | `connector/auditgovernance/oauth.go`、`publisher.go` | 401 → token 失效（F8）：移植 vault 的 401 invalidation；scope-missing → alert（§1.2） | T-10.1：AS revoke 后下一次 publish 立即刷新（token-source 调用计数断言），不等 TTL（≤24h 停滞消除） | B1（sink 401 语义） |
| 4 | aero-id | `service/account.go:155,184`、`audit_hold.go:59`、`legacy_cutover.go:88`（无 EventID 调用点） | 稳定 event_id（F4 部分）：按 registry v2 §1.9 从业务 anchor 派生，重试/再生成复用 | T-4 联合：再生成 fact 在 sink 折叠为 Duplicate、ledger 1 行 | B1 |
| 5 | aero-id | `outbox/repository.go`（dead_letter + TTL purge）；新 replay 工具 | 死信重放（F9）：replay 命令（dead → pending 重投递）；TTL purge 暂停至告警确认 | T-10.2：dead 行 → replay → 重投递成功、事件入账；purge 不再静默吞事件 | B2-1, B2-3 |
| 6 | aero-id | `configs/config.yaml` `audit_governance.enabled`（默认 false）+ bindings | 连接器显式激活（F-03）：激活门（boot fail-closed 为安全门）；首个事件验证 | 空 bindings + enabled → boot 失败（已验证 `validateAuditBindingCoverage`）；启用后首个 `account.create` 事件到达 sink | B1, B4-1, B2-1..5 |

---

## snaplink-audit-governance（sink — 持久账本冲突重试、读/自审计、dev-token 关闭）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | snaplink-audit-governance | `cmd/audit-api/main.go:45`（`AUDIT_ALLOW_DEV_AUTH` 默认 true→**false**）；`internal/auth/auth.go` `ValidateConfiguration`；`deploy/docker-compose.verify.yml` + CI manifest 扫描 | dev-token 默认关闭（S1）：默认 false；dev-only 配置（无 JWT 信任源）拒绝启动；生产 manifest 出现 true → CI 失败 | T-1.1：`AllowDev=false` 时 `dev:platform:platform-admin` → 401（全路由）；dev-only config 启动报错；manifest 扫描规则绿 | COMPOSE-2026-017（B1 首项） |
| 2 | snaplink-audit-governance | `internal/store/store.go` / `postgres.go`（`Store.Update`）；`internal/httpapi/server.go` `statusForError` | 快照存储冲突重试（F-01）：`ErrSnapshotConflict` → 有界 jitter 重试（或显式单写者拓扑声明）；映射为域错误（禁裸 500 逃逸）；file backend fsync（或文档化丢失窗口）；`/readyz` 增加 store probe（今天只探 archive） | T-6（PG CI）：2 并发 ingest 等 digest → 1 行 + 一 202 一 Duplicate、无 500；异 digest → 一 202 一 409；停 PG → `/readyz` 503（今天 200）；Postgres-gated 测试进 CI 不再 SKIP | 无 |
| 3 | snaplink-audit-governance | `migrations/001_control_plane.sql`（`ledger_events` 等，当前死代码）；`internal/store/` | Relational ledger 或显式容量预算（F-01 决策 #7）：migration-001 表接线（per-tenant insert + receipt + segment sealing 同事务，`FOR UPDATE` on `ledger_streams`），**或** `BenchmarkIngest` 记录容量 envelope | 容量 envelope 记录（events/sec + p99 vs ledger 大小曲线）；高容量类（‡/L1）cutover 以本决策为门禁；`QueryEvents` 不再 O(ledger) 全扫（relational 路径） | B1-2 |
| 4 | snaplink-audit-governance | `internal/service/`（`adminAction` 原子模式扩展）+ governance handlers | Self-audit writer（F13，normative P0）：治理操作（legalhold/export/retention/checkpoint/integrity）以注册表事件 same-store-update 写入；append 失败 → 操作中止（fail-closed） | 治理 append 失败 → 操作回滚（无部分 legal hold）；`audit.legalhold.apply` 等事件以完整信封入账；`audit.ledger.checkpoint` 由 checkpoint 任务写入 | 无 |
| 5 | snaplink-audit-governance | `internal/service/service.go` `QueryEvents`/`getEvent`、export download 路径 | Read-path self-audit（F-06）：同一 store update 追加 `audit.event.read` / `audit.event.export`（actor = 调用者、query_filter/result_count/request_id） | T-12：查询后出现调用者的 `audit.event.read` 行；直接 API 调用不可绕过（S9 场景关闭） | B1-4（同模式） |
| 6 | snaplink-audit-governance | `cmd/audit-outbox-relay`、`cmd/audit-kafka-consumer`、`internal/outbox/` | 拓扑收敛（F13）：接线或移除孤儿 outbox relay / Kafka consumer；契约声明"HTTP ingest API + self-audit writer"为唯一受支持入站路径 | 契约声明外无活动入站路径（grep：无生产者 enqueue audit_outbox）；移除或文档化孤儿组件 | 无 |
| 7 | snaplink-audit-governance | `test/e2e/fullstack.sh`（dev token → IdP token fixture）；`scripts/mint-token.sh`（新） | 跨仓 e2e（T-1.2）：token fixture 替换 dev token；`AUDIT_ALLOW_DEV_AUTH=false` 跑通 | T-1.2：`POST /token`（cc, `audit:event:write`）→ ingest 202 `ledgered` → `GET /events` 200（tenant 上下文）→ `POST /exports` 202 | B4-1（IdP claims）、B1-1；与 B4 联合门禁 |

---

## snaplink-console（审计时间线读路径接入）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | snaplink-console | `lib/services/audit_log_service.dart`（localStorage 1000-ring → sink 读 API）；`lib/services/portal_api.dart`（新增 audit 调用）；审计页 UI | 审计时间线读路径接入（F-06）：审计页改调 sink 读 API（携带 tenant_id + trace_id，经 BFF）；localStorage ring 降级为本地调试记录（不再作为证据展示）；展示服务端 `audit.event.read` 记录 | T-12 联合：查询触发 sink self-audit 行；devtools 伪造/清空 localStorage 不再构成证据（S9/O 场景关闭）；查询携带 tenant 上下文 | B1-5（read self-audit 先落地） |
| 2 | snaplink-console | `lib/`（console 原生事件） | console 原生事实走边缘生成（login → snaplink `auth.login.*` with `client_id=console`），无 outbox；随 B4 连接器落地后验证 | 控制台登录在 sink 出现 `auth.login.success`（client_id=console）；无重复事件 | B4-5 |

---

## aero-im（事件消费对齐）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | aero-im | `crates/aero-storage/src/audit.rs` + 新 outbox migration | Outbox + in-tx 写入：`audit_outbox` DDL（status 0/1/2/3 normative）；`message.*`/`room.*`/`admin.*` 行在业务事务内写入；`message.send` ‡/`notification.send` ‡ 走 L1 聚合不进 outbox | 30 个被忽略 audit 测试在 CI（live PG）全绿（37/37）；P2 parity 检查（audit_events vs outbox rows） | B1（sink 就绪） |
| 2 | aero-im | 新 connector crate（参考 aero-id `internal/connector/auditgovernance/` 模式） | Rust relay worker：§1.2 语义（lease/SKIP LOCKED、jittered backoff、422→dead 分类）；client_credentials + claim 契约；scope `audit:event:write` | 首个 `room.create` 事件到达 sink；422 注入 → 终态；relay 存在前 scope 配给被 fail-closed 拒绝（T-11） | B1, B4-2 |
| 3 | aero-im | outbox schema（flag/partition 列或独立表） | Moderation 优先级分区：`admin.content.flag`/`admin.moderation.action`（C2/C1）先于 backlog 排空投递 | moderation 事件在积压排空前到达 sink（注入积压 drill） | B5-1..2 |
| 4 | aero-im | snaplink scope registry（B4-2 配给侧） | Scope 配给：relay 实现完成后授予 `audit:event:write`（scope-matrix-v2 Flow 3 条件） | 按矩阵配给 → 端到端 403-free；无 relay 时配给被拒（fail-closed 消息点名缺失 relay） | B4-2, B5-2 |

---

## 批次门禁汇总

| 门禁 | 内容 | 通过条件 |
|---|---|---|
| G0（契约批准） | v2 三文档（registry/scope-matrix/ingestion-design） | 已随本系列修订完成（本清单不阻塞） |
| G1（信任路径，P0） | B1-1 + B4-1 + B1-7 | T-1.1、T-1.2、T-8(a) 全绿；`AUDIT_ALLOW_DEV_AUTH=false` 生产 manifest 扫描绿 |
| G2（B1 完成） | B1-2..6 | T-6、T-12、治理 fail-closed、拓扑收敛、容量 envelope 记录 |
| G3（B2 完成） | B2-1..6 | T-5、T-10.1/10.2、T-4 联合、首个事件 e2e |
| G4（B3 完成） | B3-1..6 | T-3、T-4、D1 drill、首个事件 e2e |
| G5（B4 完成） | B4-2..5 | T-2、T-8(b–e)、T-9、L0 持久化、P2 parity |
| G6（B5 完成） | B5-1..4 | aero-im 37/37、T-11、moderation 优先级 |
| G7（B6 完成） | B6-1..2 | T-12 联合、console 只显示服务端记录 |
| G8（生产 cutover） | 全部批次 + 迁移骨架 P1–P3 | 各仓 CI 全绿（go test/cargo test）、首个事件验证、DLQ/alert 上线（H2/H6） |
