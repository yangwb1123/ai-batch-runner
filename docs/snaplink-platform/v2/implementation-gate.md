实施分派完成。完整批次清单已写入 **`docs/snaplink-platform/v2/implementation-batches.md`**（13KB），以下为全量内容（可人工核对）。

**批次模型**：COMPOSE-2026-017 为基线 → B1（sink P0）→ B2（aero-id）/ B3（aero-vault）→ B4（IdP）→ B5（aero-im）→ B6（console）。**两处归属校正**（按已验证代码）：① "dev-token 关闭"（S1）在 sink 仓库（`cmd/audit-api/main.go`）；② "discovery 真值化"（F-3）在 IdP 仓库（`cmd/server/routes.go`）。**信任路径联合门禁**：B1-1 + B4-1 + B1-7 合并为"真实流量"P0 门禁（T-1.1/T-1.2/T-8a）。

---

## snaplink（IdP — token claims、scope 注册；dev-token 关闭的铸币侧）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | snaplink | `infrastructure/defaultimpl/issue_payload.go` `buildAccessPayload`（三 issuer 共用）；`token/jwt/generator.go:52-55`；`sso.go:894-960` | claim 契约落地（S2）：`buildAccessPayload` 加 **tenant_id**（client 绑定解析）+ **roles**；header 补 **kid**；`iss` 配置化（allowlist，禁 Host 派生）；`/token` 校验 `scope`（RFC 6749 §3.3） | T-8(a)：`POST /token` → 200 + header kid + claims {iss/aud/scope/client_id/tenant_id/roles}；T-1.2 联合 | B1（联合验收） |
| 2 | snaplink | `cmd/server/routes.go` token handler + scope 注册存储 | Scope registry（P0-3）：注册 scope-matrix-v2 全表；未注册 scope → 400 `invalid_scope` | T-8(d)；矩阵配给后 vault/aero-id 端到端无 403 | B4-1 同批 |
| 3 | snaplink | `cmd/server/routes.go:63-77,663-684`；`routes_test.go:41` 重写 | Discovery 真值化（F-3）：`token_endpoint=/token`；无 `/authorize` 不广告 code 流；`/revoke` 实现或移除；认证方法收敛 `client_secret_post`；device grant 广告含 `device_authorization_endpoint`；issuer allowlist | T-2：sweep 全绿（广告端点绝不 404）；`metadata.token_endpoint == "/token"` | 无（可并行） |
| 4 | snaplink | `cmd/server/routes.go:247-300`；`sso.go:909`；`/introspect` | 端点加固（F-4/5/6/7/13）：仅 body 解析；Content-Type 强制；introspection 调用方认证；constant-time 比较；cc 响应省略 refresh_token + no-store | T-8(b)(c)(e)；T-9（introspect 无凭据 → 401） | 无（同批） |
| 5 | snaplink | `cmd/` 新 wiring（`audit.Sink`/`BatchSink` 可复用）；`config_audit.go` | 治理连接器（P2 级）：outbox + auth.* in-tx（login 失败 post-commit 为唯一许可类）+ Go relay + `auth.token.issue` L1 聚合；stock audit.backend memory→sqlite/postgres + hash_chain（F-02/F11） | 重启 L0 不丢（10 logins → 10 行）；P2 parity；L1 摘要生效 | B1, B4-1..4 |

## aero-vault（死信状态、Ready 解耦、scope 对齐）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | aero-vault | migration `0039_audit_governance_outbox` 扩展；`internal/auditgovernance/relay.go` `retryFact` | 死信终态（F3）：status/dead_at 列 + 部分索引；移植 sink `DeliveryError.Permanent` 分类（422/409/tenant mismatch/无效回执 → 终态 ≤1 次尝试）；瞬态有界重试 cap 300s；dead 行排除出 claim/lag | T-3：422 → 一个周期内终态；`Ready()` 含 dead 行 = true；批次继续 | B1 |
| 2 | aero-vault | `internal/auditgovernance/runtime.go` `Ready()`；`cmd/server/audit_governance.go:52-59` | Ready 解耦（H1）：maxLag 翻转移除 → `degraded` + **maxLag×0.5（450s）告警**；终态行排除出 `OldestPending`；读路径超时降级非 503 | D1 drill：sink 停 60min → `/readyz` 200 + 450s 告警；无重启循环 | B3-1 |
| 3 | aero-vault | `internal/auditgovernance/facts.go`（三构造点） | 确定性 fact ID（F4）：`SHA-256(source\|tenant\|event_type\|origin_kind\|origin_id\|time_bucket)[:32]`；gap reconcile 复用 | T-4：再生成 → sink Duplicate、`QueryEvents` 1 行 | B1 |
| 4 | aero-vault | `internal/auditgovernance/relay.go`/`runtime.go`（0 Observe） | Relay metrics（H6）：attempted/delivered/failed/dead/oldest-age | 指标可喂 H2 告警；stalled relay 可检测 | B3-1 |
| 5 | aero-vault | `internal/auditgovernance/model.go` `RequiredScope`；`token.go` | scope 对齐（F2 收尾）：**无代码改动**（已 enforce `audit:event:write`）——保留 scope 拒绝测试 + 矩阵配给端到端验证 | 矩阵配给无 403；非 write scope 拒绝；grep 一致性检查绿 | B4-2 |
| 6 | aero-vault | `internal/config/config_audit_governance.go`；部署清单 | 激活门（F-03）：`AUDIT_GOVERNANCE_ENABLED=true` + bindings 文件 + 首个事件验证 | 空 bindings + enabled → boot 失败；首个 `file.delete.admin` 事件到达 sink | B1, B4-1, B3-1..4 |

## aero-id（in-tx 审计记录）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | aero-id | `service/account.go:155,184`、`callback.go:160,221,247`、`audit_hold.go:59,74`、`legacy_cutover.go:88`（8 点） | in-tx（F5）：全部移入 `AppendInTx`；不可 in-tx → 失败可观测（`ObserveAuditIngestFailure` + 告警）；**`_ =` 禁止**（grep 门禁） | T-5：mock 失败 → counter 递增或操作失败；无静默调用点 | 无（可先于 B1） |
| 2 | aero-id | `audit_ledger.go` `Record`（inline publish） | 单一投递路径（F6）：删 inline publish 或 best-effort；outbox worker 唯一 | 单路径；sink 慢时请求 p99 不漂移（F6 drill）；2× 负载消除 | B2-1 |
| 3 | aero-id | `connector/auditgovernance/oauth.go`、`publisher.go` | 401 → 失效（F8）：移植 vault 模式；scope-missing → alert | T-10.1：revoke 后立即刷新（调用计数断言），≤24h 停滞消除 | B1 |
| 4 | aero-id | `service/account.go:155,184`、`audit_hold.go:59`、`legacy_cutover.go:88` | 稳定 event_id（F4）：按 §1.9 从 anchor 派生 | T-4 联合：再生成折叠 Duplicate | B1 |
| 5 | aero-id | `outbox/repository.go`（dead_letter/purge）；新 replay 工具 | 死信重放（F9）：replay（dead→pending）；TTL purge 暂停至告警确认 | T-10.2：replay 后重投递成功；purge 不静默吞事件 | B2-1, B2-3 |
| 6 | aero-id | `configs/config.yaml` `audit_governance.enabled` + bindings | 激活门（F-03）：boot fail-closed 为安全门；首个事件验证 | 空 bindings + enabled → boot 失败；首个 `account.create` 到 sink | B1, B4-1, B2-1..5 |

## snaplink-audit-governance（sink — 冲突重试、读/自审计、dev-token 关闭）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | sink | `cmd/audit-api/main.go:45`；`internal/auth/auth.go` `ValidateConfiguration`；`deploy/docker-compose.verify.yml` + CI manifest 扫描 | dev-token 关闭（S1）：默认 **false**；dev-only 配置拒绝启动；manifest true → CI 失败 | T-1.1：`dev:platform:platform-admin` → 401 全路由；dev-only 启动报错 | 基线（B1 首项） |
| 2 | sink | `internal/store/store.go`/`postgres.go`；`internal/httpapi/server.go` `statusForError` | 冲突重试（F-01）：`ErrSnapshotConflict` → 有界 jitter 重试（或显式单写者）；域错误映射（禁裸 500）；fsync；readyz store probe | T-6（PG CI 不 SKIP）：等 digest → 1 行 + 202/Duplicate 无 500；停 PG → readyz 503 | 无 |
| 3 | sink | `migrations/001_control_plane.sql`（死代码）；`internal/store/` | Relational ledger 或容量预算（#7 决策）：migration-001 接线（per-tenant insert + receipt + segment 同事务）或 `BenchmarkIngest` envelope | 容量 envelope 记录；高容量类 cutover 门禁；`QueryEvents` 不再 O(ledger) | B1-2 |
| 4 | sink | `internal/service/`（`adminAction` 扩展）+ governance handlers | Self-audit writer（F13）：治理操作 same-store-update 注册表事件；append 失败 → 中止 | 治理失败 → 回滚（无部分 legal hold）；事件完整信封入账 | 无 |
| 5 | sink | `internal/service/service.go` `QueryEvents`/`getEvent`、export download | Read self-audit（F-06）：same-update 追加 `audit.event.read`/`audit.event.export` | T-12：查询后出现调用者 self-audit 行；直接 API 不可绕过 | B1-4 |
| 6 | sink | `cmd/audit-outbox-relay`、`cmd/audit-kafka-consumer`、`internal/outbox/` | 拓扑收敛（F13）：接线或移除孤儿组件；唯一受支持入站路径声明 | 无活动入站路径（grep 无 enqueue） | 无 |
| 7 | sink | `test/e2e/fullstack.sh`；`scripts/mint-token.sh`（新） | 跨仓 e2e（T-1.2）：IdP token fixture 替换 dev token；`AUDIT_ALLOW_DEV_AUTH=false` | T-1.2：ingest 202 `ledgered` → read 200 → export 202 | B4-1, B1-1（联合门禁） |

## snaplink-console（审计时间线读路径接入）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | console | `lib/services/audit_log_service.dart`；`lib/services/portal_api.dart`；审计页 UI | 读路径接入（F-06）：审计页调 sink 读 API（tenant_id + trace_id 经 BFF）；localStorage ring 降级为调试记录；展示服务端记录 | T-12 联合：查询触发 self-audit 行；devtools 伪造不再构成证据 | B1-5 |
| 2 | console | `lib/` 原生事件 | 边缘生成验证：login → `auth.login.success`（client_id=console） | sink 出现 console login 事件；无重复 | B4-5 |

## aero-im（事件消费对齐）

| # | 仓库 | 文件或模块 | 改动 | 验收断言 | 依赖批次 |
|---|---|---|---|---|---|
| 1 | aero-im | `crates/aero-storage/src/audit.rs` + outbox migration | Outbox + in-tx：DDL（status 0/1/2/3 normative）；`message.*`/`room.*`/`admin.*` 同事务写入；‡ 类走 L1 | 30 个忽略测试 CI 全绿（37/37）；P2 parity | B1 |
| 2 | aero-im | 新 connector crate（aero-id 模式） | Rust relay：§1.2 语义（lease/backoff/422→dead）；cc + claim 契约；scope `audit:event:write` | 首个 `room.create` 到 sink；422 → 终态；T-11（无 relay 配给被拒） | B1, B4-2 |
| 3 | aero-im | outbox schema（flag/partition） | Moderation 优先级：`admin.content.flag`/`admin.moderation.action` 先于积压 | 注入积压 drill：moderation 先达 sink | B5-1..2 |
| 4 | aero-im | snaplink scope registry（B4-2 配给侧） | Scope 配给：relay 完成后授予 `audit:event:write` | 矩阵配给端到端 403-free；无 relay 时 fail-closed 拒绝 | B4-2, B5-2 |

## 批次门禁汇总

| 门禁 | 内容 | 通过条件 |
|---|---|---|
| G0 | v2 三文档（契约批准） | 已随本系列完成（本清单不阻塞） |
| G1（P0 信任路径） | B1-1 + B4-1 + B1-7 | T-1.1/T-1.2/T-8(a) 绿 + manifest 扫描绿 |
| G2（B1） | B1-2..6 | T-6、T-12、治理 fail-closed、拓扑收敛、容量 envelope |
| G3（B2） | B2-1..6 | T-5、T-10.1/10.2、T-4 联合、首个事件 e2e |
| G4（B3） | B3-1..6 | T-3、T-4、D1 drill、首个事件 e2e |
| G5（B4） | B4-2..5 | T-2、T-8(b–e)、T-9、L0 持久化、P2 parity |
| G6（B5） | B5-1..4 | 37/37、T-11、moderation 优先级 |
| G7（B6） | B6-1..2 | T-12 联合、console 服务端记录 |
| G8（生产 cutover） | 全部批次 + 迁移骨架 P1–P3 | 各仓 CI 全绿、首个事件验证、DLQ/alert 上线（H2/H6） |

**要点**：① 批次间严格按 v2 rollout 顺序（sink → aero-id → aero-vault → IdP → aero-im → console），仅 B4-3/B4-4 与 B2/B3 可并行；② 所有验收断言均映射审查测试编号（T-1.1 至 T-12）与 drill（D1），可逐条回归；③ aero-vault B3-5 与 aero-im B5-2 的"scope 对齐/配给"依赖 B4-2（IdP scope registry），这是 F2 契约修订后的唯一剩余依赖链。
