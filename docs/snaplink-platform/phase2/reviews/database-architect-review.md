# Database Architect Review — Phase 2: store durability, stock wiring, and migration safety

> **Reviewer**: database-architect role (`prompts/README.md` applied).
> **Subject**: `docs/snaplink-platform/phase2/kickoff.md` (正文与已验证
> `/tmp/kickoff-final.md` 一致) vs the **currently deployed persistence topology**
> (evidence 05, DEPLOYMENT, TESTING_GUIDE) and the code at the pinned HEADs.
> **Checks that ran for this revision**: static reads of store/query/write paths at
> snaplink `d19838d8`, sverpweb-stable `abd8801`, snaplink-audit-governance `476d083`,
> aero-vault `c0ce6db`, aero-id `9a4ec42`, aero-im `53b0ced`; `grep` scans for
> config keys, imports, PRAGMAs, dead-schema references; local `.env` key scan.
> No CI was run; **server-side config contents (shared/config.yaml, .env) are not
> inspectable from this workspace** — deployed-backend claims are marked Partial.
> Line numbers cite the reviewed revisions.

## 0. Executive summary

Every business store in the deployed platform is **durable-by-write** (SQLite WAL,
Postgres), with **four hot or gap exposures** that matter for the phase-2 roadmap:

1. **The audit source of truth (governance `state.json`) has no backup, no fsync,
   and no restore drill** — a data-loss path for the very store L2 is built on.
   The kickoff correctly makes it backup-priority #1 (4.3); this review adds the
   fsync detail (the write path itself can revert on power loss) and a
   consistency requirement: governance is the **single-writer, single-node
   ledger** — its "durability" is entirely a copy/restore discipline problem.
2. **"Restart rotates the signing key" is a stock-binary capability gap, not a
   configuration error** — `SigningConfig` has no private-key-file option at all;
   `NewEd25519JWTIssuer` generates a fresh in-process key per boot.
   `rotation.enabled=false` only disables the scheduled rotation loop. 1.2.1
   therefore requires **new code**, and the key file it introduces must be added
   to backup scope and `ReadWritePaths` (4.3 currently lists "snaplink sqlite
   (sso db + 密钥)" — the signing key is **not** in sqlite today; it lives only
   in process memory).
3. **sverpweb audit enqueue is not atomic with the business commit** — the
   outbox is durable *delivery* (at-least-once) but only best-effort *enqueue*
   (at-most-once on crash between the two commits). The L2 promise "审计不丢"
   needs an explicit compensation mechanism, not just the drainer.
4. **Stock defaults remain hot/insecure where the deployment already overrides
   them**: sso-server `config.yaml` ships `identity/oauth/audit.backend: memory`
   (audit ring 10 000), audit-api ships `AUDIT_ALLOW_DEV_AUTH=true`. The
   deployment fixes both (evidence 05), but the **stock binary** is still
   hot-by-default and insecure-by-default — the L2 checklist should gate on
   binary-level fail-closed, not deploy-level.

Verified-good persistence (from round 1/2, re-confirmed at current HEADs):
aero-id hash-chained append-only ledger + same-tx publication outbox (dual
delivery path still present, F-4), aero-vault lease-fenced outbox (relay off by
default), aero-im trigger-enqueued delivery outbox (relay off by default),
sverpweb outbox poison-pill/permanent + lease claim + idempotent append — the ERP
L2 path is the **only fully-wired** governance integration in the platform.

## 1. Store inventory — purpose / durability / implementation / stock wiring / consistency

| # | Store (deployed) | Purpose | Durability (deployed) | Implementation (Verified path) | Stock binary wiring | Consistency requirement |
|---|---|---|---|---|---|---|
| S1 | snaplink-sso (snaplink-sverp :13015/:13016) | Identity/oauth/audit: users, clients, sessions, refresh tokens, auth codes, MFA, audit | **Durable if sqlite configured** (evidence 05 "sqlite+JWT" — **Partial**, server config not inspectable); **stock default is HOT**: `cmd/sso-server/config.yaml:87/114/210` identity/oauth/audit `backend: memory` (audit ring 10000); revocation `backend: ""` = in-process only; **signing key HOT (fresh per boot)** | `serverbuildsign/build_signing_issuers.go:41-59` → `defaultimpl/ed25519_jwt_issuer.go:270` `NewEd25519JWTIssuer` generates `ed25519.GenerateKey(rand.Reader)` when no external signer/private key; `config/config_keys.go:61` `SigningConfig` has **no key-file field** | Stock binary wires per-config backends; **governance connector NOT wired**: `infrastructure/auditgovernance` imported only by `cmd/snaplink-billing/relay.go`, never by `cmd/sso-server` | Refresh-token atomic consume/rotate (Redis Lua, v2-verified); audit chain in-process only; **kid stability across restart = 0 today** |
| S2 | audit-governance (:13018) | Audit ledger (events/receipts/retention/export/legal-hold) — **L2 source of truth** | **Durable-by-write with fsync gap**: `internal/store/store.go:251-254` `os.WriteFile`+`os.Rename`, **no `Sync()`**; default `./data/state.json` (`cmd/audit-api/main.go:33`); single-writer in-process `Store.mu`; **zero backup, zero restore drill** | Whole-snapshot blob rewritten per event; ingest idempotency = O(all events) scan `internal/service/service.go:370`; queries O(ledger) in-memory filter/sort (:544,:842); relational ledger `ledger_events/streams/segments` = **dead schema (zero Go refs)**; no migration runner in binary | Stock `audit-api` file backend wired; **`AUDIT_ALLOW_DEV_AUTH` stock default true** (`main.go:44`) — deployment sets false (Verified, 401 without token); PG single-row backend exists, **unused** (no migration runner → e2e never applies 004) | Single logical writer; per-event receipts (202 Duplicate / 409 conflict branches — M0 `erp_contract_test.go` 5 tests Verified); retention per tenant/class (deployed values Unknown) |
| S3 | aero-vault (:13019) | File platform (presign PUT/GET/DELETE via BFF, tenant-mapped) | **Durable**: sqlite WAL + foreign_keys (`internal/repository/sqlite.go:31`) + local files | Presign: default 300s, **capped 7d** `internal/auth/sigv4.go:27 maxPresignTTL`, tenant-bound HMAC signature (`SignPut(target, tenant, expiry)`, `internal/api/rest/presign.go:102`); BFF replaces internal base URL for browser (routes/integrations.ts:208) | Snaplink JWKS auth wired (evidence 05); **governance outbox relay NOT wired**: `AUDIT_GOVERNANCE_ENABLED` default **false** (`internal/config/config_audit_governance.go` `getEnvBool(..., false)`); nil-panic fix c0ce6db deployed | Tenant isolation via snaplink claims; **no cross-tenant negative test exists** (kickoff 3.2 gap); presign single-writer per key (no versioning used) |
| S4 | aero-id (:13020/:13021) | Account domain (profile/org/membership) + own audit ledger | **Durable** (PG `aero_id`, auto_migrate first boot — DEPLOYMENT §2) | `internal/service/audit_ledger.go:66-84` `Record()` = `repo.Append` (own tx) + **inline `_ = publisher.Publish` (errors swallowed)** — F-4 dual-delivery path **persists at HEAD 9a4ec42**; outbox worker `drainPublications` also wired | Stock cmd wires ledger + publication loop (v2-verified); **zero ERP traffic today** (only gRPC token validation; kickoff 1.2.7 keeps org H2) | Per-partition total order (advisory lock + UNIQUE backstops, v2-verified); at-least-once delivery |
| S5 | aero-im (:13022) | IM/notification (ERP uses "SVERP Integration" channel API only) | **Durable** (PG `aero_im` + pgvector, Redis, NATS JetStream :13023) | Channel messages in PG; BFF notifier `im-notify.ts` = login-token cache in memory + full try/catch **best_effort** (Verified: notify failure never blocks business/audit) | **Snaplink audit relay default OFF** (`snaplink_commercial_runtime.enabled` default false, v2-verified) — no audit delivery from aero-im to governance in this deployment | In-tx trigger enqueue + `UNIQUE(destination, idempotency_key)` (v2-verified, relay latent); message retention policy Unknown |
| S6 | sverpweb-stable (:13014) | **ERP business data + audit outbox + media — the L2 critical path** | **Durable-by-write with power-loss window**: ALL stores `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000` (sqlite-outbox.ts:89-91, auth/sqlite-stores.ts:138-140, quality:240-242, aps:483-485, bs-pricing:394, media:62-65); media files (`media-<uuid>`, `quality-inspection-files/`) on same disk as app.sqlite, written **before** row insert (orphan-file-on-crash only) | `app.sqlite`: business stores + `audit_outbox` + media metadata; `auth.sqlite`: AES-256-GCM sessions. Outbox: `BEGIN IMMEDIATE` batch append with `ON CONFLICT(id) DO NOTHING`; conditional lease claim (status/available_at/locked_at/lock_token); `permanent` poison-pill column (idempotent ADD COLUMN, zero rebuild); `markPublished` guarded by lock_token; indexes (status,available_at,created_at)/(operation_id)/(aggregate) | **Fully wired**: ForwardingAuditSink (append awaited → best-effort single-flight flush, startup flush), governance-transport adapter (http.request.* noise skipped, client.ts:148), OAuth client_credentials auto-refresh + 401 forced retry (client.ts:193-200), permanent classification {400,401,403,404,405,410,422} (client.ts:45); **static AUDIT_APP_TOKEN fallback still present** (env.ts:85, forwarding.ts:68-75) | At-least-once **delivery** (idempotency_key=event.id); enqueue is best-effort **separate tx** from business commit (F-H1 below); single-writer process (Bun), multi-handle WAL on one file |
| S7 | snaplink-console (:13017) | Admin control plane (nginx static Flutter Web) | **HOT, non-durable, forgeable**: audit page = `localStorage` ring `sso_audit_log` (1000 entries, `lib/services/audit_log_service.dart`) — v2 F-12, **still current** (evidence 05 §8) | No server store; local admin-op log only | Not wired to governance read API (kickoff 1.2.3 = M3a) | None (zero evidence value today) |
| S8 | NATS JetStream (:13023) | aero-im bus | Durable by config (JetStream file store) | — | Not in ERP critical path | At-least-once stream delivery (aero-im managed) |

## 2. Findings (severity-ordered)

### Critical

**C1. 审计真源（governance state.json）无备份 + 无 fsync + 无恢复演练（Verified；阻断 L2 宣布）.**
- Evidence: `internal/store/store.go:251-254` — `os.WriteFile(tmp)` → `os.Rename(tmp, path)`, no file `Sync()` and no directory fsync (its own archive does fsync — `internal/archive/archive.go:52`); deployment = single node file ledger (evidence 05); zero backup scripts anywhere (grep of DEPLOYMENT/TESTING_GUIDE/evidence 05: no backup mention); kickoff 4.3 itself states "当前零脚本".
- Impact: power loss between write and rename (or rename without dir fsync) **reverts committed audit events silently**; disk failure = total loss of the platform's source of truth; no restore drill means even a good backup is unvalidated. L2's whole premise ("governance 是审计事实源") has no recovery story.
- Recommendation: before any further milestone, land kickoff 4.3 #1 with fsync included — (a) add `Sync()` + dir fsync in `fileBackend.Save` (small, matches archive.go); (b) nightly copy + sha256 + version retention; (c) **monthly restore drill** asserting query + E2E green after restoring from the copy (test-platform.sh `backup` subcommand, kickoff 3.3 gap 6); (d) document single-writer: never run a second `audit-api` against the same file.
- Validation: kill -9 during an ingest loop → restart → `VerifyIntegrity` passes and event count equals pre-kill count (crash test); restore-from-copy drill green.

**C2. "重启即轮换签名密钥"是 stock 能力缺口——`rotation.enabled=false` 与重启行为无关（Verified；1.2.1 需要新代码）.**
- Evidence: `config/config_keys.go:61` `SigningConfig` fields = Alg/External/RevocationBackend/RevocationDSN only — **no key-file option**; `infrastructure/defaultimpl/ed25519_jwt_issuer.go:270` — fresh `ed25519.GenerateKey(rand.Reader)` per process start when no external signer; `keys.rotation.enabled` only gates the scheduled rotation loop (`serverbuildplatform/build_governance.go:54-69`, `build_app_cluster.go:333-352`). kickoff 1.2.1's root-cause statement is exactly right; the fix ("密钥持久化，仅显式 rotate 才轮换") is **not a config flip — it is a new load-or-generate key-file path** in the issuer build.
- Impact: every restart = new kid → all existing JWT/refresh validation fails at governance (PEM timer 2min) and vault (JWKSCache) until resync; user sessions still work at sso itself (same issuer), but downstream 401 windows occur (mitigated by client retry + idempotency — TESTING_GUIDE §5).
- Recommendation: add `keys.signing.key_file` (0600, in `shared/data`) + load-or-generate in `buildEd25519SigningIssuer`; persist key **before** first token issuance (no signing with unpersisted key); emit a loud `kid` log on generate-vs-load; keep `rotation.enabled` as the only scheduled-rotation switch. **After landing**: add key file to 4.3 backup scope and to the systemd `ReadWritePaths` (deploy/snaplink-sverp.service — currently only `shared/data` is writable, which is correct if the key lives there).
- Validation (kickoff 1.2.1 acceptance): `systemctl restart snaplink-sverp` → kid fingerprint unchanged → `test-platform.sh status/audit/e2e` green **without** manual `sync-sso-key.sh`; new unit test: rotation.enabled=false → fingerprint stable across restart; explicit `rotate` → kid changes + 2min convergence.

### High

**H1. 审计事件入队与业务提交非原子——崩溃窗口丢审计事实且无补偿（Verified；L2 "审计不丢"承诺缺口）.**
- Evidence: `routes/quality-inspections.ts:254-266` — `inspections.create`（独立 `transaction.immediate()`，quality/sqlite-store.ts:455）**提交后**才 `await emitAuditEvent` → `outbox.appendMany`（另一连接、另一事务，sqlite-outbox.ts:150-180）；`http.request.*` 兜底事件被噪音过滤跳过（governance-transport.ts:37-42），不覆盖该窗口。
- Impact: crash between the two commits → 业务事实已提交、审计事件从未入队、无任何补偿记录；请求未返回 201，客户端重试会再造一个业务事实（业务侧幂等性不在本审查范围，但该窗口同样存在）。outbox 保证的是 **delivery** at-least-once，不是 **enqueue** at-least-once。
- Recommendation: 不强行共享事务（SQLite 跨连接无法原子）；落地**补偿扫描器**——周期比对业务行与 outbox 行（按 aggregate_id/occurred_at 窗口），缺失事件重新入队（幂等键保证不重复）；同时把 `emitAuditEvent` 移到业务事务**之前**注册（业务失败则事件标注 failed 语义，需要事件载荷可反查）。L2 验收须把"崩溃注入（kill -9）后账本仍含该业务事件"列为断言（kickoff 3.3 gap 7 扩展）。
- Validation: fault-injection test — kill -9 between commit points → 补偿扫描后 governance 账本含事件；`pending=0 AND permanent=0` 24h 观察（kickoff 3.3 #7）。

**H2. 全库 `synchronous=NORMAL` ——掉电窗口可丢已提交事务（Verified；审计库应 FULL）.**
- Evidence: every sverpweb store sets `synchronous=NORMAL` (S6 rows). WAL+NORMAL: app crash safe, **power loss can lose the most recent committed transactions**.
- Impact: audit outbox append + business commit both sit in this window; the audit chain "无重复" holds but "无缺失" doesn't under power loss.
- Recommendation: audit outbox DB → `synchronous=FULL`（或 FULL 仅对 app.sqlite；auth.sqlite 可 NORMAL——会话丢失仅重登）；文档化掉电接受标准。成本：单进程低频写，FULL 的 fsync 开销可忽略。
- Validation: `PRAGMA synchronous` == FULL after boot; power-cut test (if facility available) or documented acceptance.

**H3. stock 默认仍热/不安全——部署层已覆盖，二进制层未 fail-closed（Verified）.**
- Evidence: sso-server `config.yaml:87/114/210` memory backends（stock 重启全丢身份/会话/审计——v2 F-6 仍在 HEAD）; audit-api `AUDIT_ALLOW_DEV_AUTH` 默认 true（`cmd/audit-api/main.go:44`; test `audit_backend_test.go:17` pins memory default）; deployment overrides both (evidence 05, DEPLOYMENT §3).
- Impact: 任何新部署按 stock 默认启动即热 + dev-auth 可绕过；L2 checklist 若只验部署实例，二进制的默认不安全会随下一次部署回归。
- Recommendation: L2 前置清单（1.2.2）增加两条二进制级门禁：① sso-server 默认 `audit.backend: sqlite`（或 `hash_chain` 开启时拒绝 memory）——memory 仅限 `topology.mode: single` 显式 dev 配置；② audit-api `AUDIT_ALLOW_DEV_AUTH` 默认 false + `ValidateConfiguration` 拒绝 dev-auth 与生产信任材料共存 + CI manifest 扫描（v2 F-2 acceptance 原样）。
- Validation: stock config boot → identity/oauth/audit 后端为 sqlite（重启 10 登录 → 10 行）；默认 env 下 `dev:platform:platform-admin` token → 401。

**H4. 平台侧 governance 接线缺位/双路径（Verified；证据等级修正 + 低优先级）.**
- Evidence: vault relay 默认关（S3）、im relay 默认关（S5）、aero-id 双交付路径仍在（`audit_ledger.go:79` `_ = publisher.Publish` 吞错，S4）；ERP 侧是**唯一全接线**（S6）。
- Impact: 平台"统一审计时间线"（1.2.3 console 页）只能看到 ERP 来源事件 + governance 自身管理操作；SSO 内部策略/RBAC 变更、vault 文件操作、IM 事件均不入账。与 kickoff 3.1 "Snaplink AuditSink 未验证缺口"的处置一致（暂缓、触发条件驱动），但需把"未验证"升级为"**已验证未接线**"写进文档，避免未来误判。
- Recommendation: 维持 kickoff 处置（不接项 + 触发条件：合规要求或 SSO 侧审计事故）；aero-id 在任何业务流量接入前（1.2.7 触发时）先修 F-4 双路径（全部走 outbox，删 inline publish，`_ =` grep 门禁）。

### Medium

**M1. 备份体系零脚本（Verified；4.3 需在 1.2.5 之前落地）.**
- Evidence: 无任何 backup 脚本/文档（C1 同源）；`sso-ctl snapshot` 存在（evidence 02）但未演练；PG 无 pg_dump 任务。
- Recommendation: 按 kickoff 4.3 优先级落地：governance state.json（#1）→ snaplink sqlite（#2，**加 1.2.1 后的密钥文件**）→ sverpweb app.sqlite+auth.sqlite+media（#3，`sqlite3 .backup` 一致性 + 文件 rsync，迁移完成前双份）→ vault → PG pg_dump（7/30/90）；恢复演练进 test-platform.sh `backup` 子命令（kickoff 3.3 gap 6）。**sverpweb 备份必须在 vault 迁移（1.2.5）之前就位**（kickoff 已正确后移 1.2.5 依赖 4.3）。

**M2. 无保留/清理策略落地（Verified 缺口）.**
- Evidence: outbox `published` 行**永不清理**（sqlite-outbox.ts 无 DELETE 路径）；governance retention 默认 hot=30/warm=180/archive=365（migration 001）未对 ERP 租户验证；vault `delivered_origins` 去重表无界（v2 F-16）；IM 频道消息无保留策略。
- Recommendation: ① outbox 清理 job（published + 90d 或 configurable，计数入监控）；② governance 为 sverp 租户显式设置 retention_class 策略并验证 EvaluateRetention 行为；③ vault delivered_origins 加有界保留；④ IM "SVERP Integration" 频道消息保留期随 M5 一并定义。

**M3. 静态 token 回退（AUDIT_APP_TOKEN）（Verified；对齐 kickoff 2.3）.**
- Evidence: `env.ts:85` + `forwarding.ts:68-75` — OAuth 未配置时回退静态 token；无 provider 时 401 → permanent（client.ts:45,193-200）→ 事件被永久隔离（毒丸，有界摘录可查但事件丢失）。
- Recommendation: 按 2.3 删除 `AUDIT_APP_TOKEN`；transport 侧 OAuth 未配置 → **fail-closed 启动即拒**（无静默回退）；保留 401→强制刷新→重试 单测。注意 `sso-admin/sverp-audit` secret 本身归 4.2 密钥管理。

**M4. console localStorage 审计环（Verified；1.2.3 覆盖）.**
- Evidence: S7 — forgeable client-side ring；读侧无授权路径。处置即 kickoff 1.2.3（governance 读 API + sverp-audit 授权 + 契约测试）；补充：console 查询须分页 + source 过滤 + actor 展示，且**先** 3.2 的 scope 收窄（查询走 sverp-audit 而非 sso-admin）再上线页面。

**M5. 迁移体系碎片化（Verified）.**
- Evidence: outbox `permanent` 列是 PRAGMA table_info 隐式迁移（无版本戳，B4 实况显式化，幂等做对了但无 schema 版本）；governance 无 migration runner（F-8，PG 后端在部署中不可用）；audit_outbox 无版本戳。
- Recommendation: 给 sverpweb SQLite schema 加轻量 `schema_version` 表（后续迁移走版本化路径）；governance PG 后端如启用需先补 migrate 命令；1.2.5 vault 迁移的校验/回填脚本须版本化入仓（kickoff 3.3 gap 8 同源）。

### Low / Info

- **L1（Info，比 kickoff 2.2 更干净）**：`DEMO_PASSWORD` 在代码侧**已死**（HEAD abd8801：`grep -rn DEMO_PASSWORD apps/ packages/` 零命中；env.ts 只读 `DEMO_USERNAME`）——剩余工作仅是 .env 清理 + env 未知键 fail-closed 校验 + `grep -rn DEMO_` 零命中门禁，无代码改动。
- **L2（Info）**：vault presign TTL 有上限 7d、默认 300s、tenant 绑定签名（S3）——设计合理；补跨 tenant 负向测试（3.2 已列）。
- **L3（Info）**：governance 409 三分支语义与 202 Duplicate 去重正确（M0 契约测试 Verified）；outbox 幂等键 = event.id 稳定。
- **L4（Info）**：`sso-ctl snapshot` 可作为 snaplink 状态备份工具，纳入 4.3 演练范围（当前零演练）。
- **L5（Info）**：aero-id 链式账本（advisory lock + append-only trigger + UNIQUE 双保险）与 aero-vault/aero-im outbox 设计是平台最佳实践参照系——保留，勿因"未接线"而删。

## 3. Query/index and transaction analysis (demonstrated hot/atomic paths)

**Path 1 — sverpweb outbox claim/publish（ERP 审计热路径，Verified atomic）.**
`appendMany`: `BEGIN IMMEDIATE` + 逐条 `INSERT ... ON CONFLICT(id) DO NOTHING`（批量原子，冲突幂等）→ `claimPending`: 先条件 SELECT（`available_at<=now AND permanent=0 AND (pending|failed|stale-publishing)`，`ORDER BY created_at, id LIMIT n`，索引 `(status, available_at, created_at)` 支撑）→ 逐行条件 `UPDATE ... WHERE id=? AND available_at<=? AND permanent=0 AND (status IN (...)|stale)`（CAS 语义，`changes=0` 即放弃）→ `markPublished` 带 `lock_token` 守卫。lease 默认 30s、批 50。permanent 毒丸从 claim 排除（终态隔离）。单进程多 handle 共享 WAL，busy_timeout 5s。**结论：delivery 路径原子且无双领；enqueue 与业务事务分离（H1）。**

**Path 2 — quality inspection create（业务写，Verified atomic）.**
`transaction.immediate()` 包 header+items（sqlite-store.ts:455-463）——业务原子性正确；audit 事件在其后独立事务（H1）；IM 通知 best_effort 在其后（Verified，不参与事务）。

**Path 3 — governance ingest（Verified，正确但 O(snapshot)）.**
整快照 load → mutate → save（in-process `mu` 串行化）；幂等 = 遍历全部 events（service.go:370）；查询 = 全量内存过滤 + 游标（:544,:842）。文件模式无 CAS（单写者）；PG 模式有 version CAS 但部署未用（且无 migration runner）。关系型 ledger 表索引完备但**不可达**（dead schema）。分页为内存游标，O(ledger)。**ERP 量级（日几十事件）可用；容量包络未测量（F-1 遗留，见 §5）。**

**Path 4 — aero-id chain append（Verified atomic，v2 复核仍成立）.**
重复检查（锁前）→ `pg_advisory_xact_lock(hashtext(partition_key))` → 重复检查（锁后）→ 链头读取（`idx_audit_ledger_partition`）→ append + publication 同行事务；`UNIQUE(source, partition_key, chain_sequence)` + `UNIQUE(source, event_id)` + append-only trigger 兜底。唯一遗留：`Record()` 的 inline publish 双路径（H4）。

**Path 5 — aero-im trigger enqueue（Verified atomic，v2 复核）.**
`audit_events` insert 同事务触发 `aero_enqueue_snaplink_audit` 入 `snaplink_delivery_outbox`；`UNIQUE(destination, idempotency_key)`；CTE claim + `binding_for_claim` 身份围栏。**relay 默认关（S5）——代码路径正确但部署惰性。**

**Path 6 — IdP refresh-token 轮换（v2 Verified；Redis 热存储 by design）.**
Lua 原子 consume+rotate+family 计数；Redis 丢失 = 强制重登（已文档化的热存储），可接受。

## 4. Safe migration sequence (compatibility window / validation / rollback)

定序依据：备份先行 → 根因修复（1.2.1）→ L2 前置（1.2.2）→ 数据风险收敛（1.2.5，依赖备份）→ 可见面（1.2.3）→ 硬化（H1/H2/M2）→ 增值（1.2.6/1.2.7）。每步均为可逆加法。

1. **Step 0 — 备份就位（无代码，4.3 #1/#3）**：governance `state.json` 每日拷贝+sha256；sverpweb `sqlite3 .backup`（一致性）+ media 目录 rsync；PG `pg_dump`（aero_id/aero_im）。兼容窗口：无。回滚：无（纯新增）。验证：`test-platform.sh backup` 全绿 + 一次从拷贝恢复的查询/E2E 演练。**1.2.5 的前置硬门禁。**
2. **Step 1 — 1.2.1 签名密钥持久化（snaplink 仓，新代码）**：加 `keys.signing.key_file` + load-or-generate（生成后先 fsync 再签发）；兼容窗口：首次部署迁移 = kid 变更一次（等价今天每次重启的行为，一次性可接受，需在发布记录注明）；此后重启零变更。回滚：删除配置键 → 回到每次生成（旧行为）。回滚前先备份密钥文件。验证：重启演练 kid 不变 + 三套件绿 + 无需手工 sync；单测（rotation.enabled=false 跨重启指纹不变）。数据完整性：密钥文件 sha256 入 4.3 备份清单；`ReadWritePaths` 确认覆盖。
3. **Step 2 — 1.2.2 L2 前置（sverpweb + audit-api）**：a) `.env` 删 `DEMO_PASSWORD` + env 未知/demo 键 fail-closed（启动即拒，带键名）；b) audit-api `AUDIT_ALLOW_DEV_AUTH` 默认 false + manifest 扫描门禁；c) HTTPS 豁免决策文档化。兼容窗口：a/b 均为配置行为变化，仅影响 dev 形态（生产已 false）。回滚：还原默认值。验证：`grep -rn DEMO_` 生产 .env 零命中；默认 env boot 下 dev token 401；L2 checklist 全勾。
4. **Step 3 — 4.3 自动化 + 月度恢复演练**（含 sso-ctl snapshot 演练）。验证：演练脚本从备份恢复 governance 后查询/E2E 全绿。
5. **Step 4 — 1.2.5 vault 媒体迁移（sverpweb + vault）**：先 `schema_version` 表（M5）→ 新增上传/下载走 vault presign（双读：vault 优先、SQLite 回退，**禁双写**——采纳第一轮裁决）→ 懒迁移 + 回填任务（计数/字节/校验和核对）→ 切流开关 → 全量校验后关 SQLite 读路径。兼容窗口：旧 URL 在关闭回退前持续可用（迁移期 URL 兼容，1.2.5 验收）。回滚：切流开关回拨（双读仍在）；回填可重跑（幂等）。验证查询：`SELECT count(*), sum(size) FROM media` vs vault 清单（每 tenant）；抽样内容哈希比对；presign 上传/下载/删除/越权 403（test-platform.sh `vault` 子命令）。数据完整性：迁移完成前 app.sqlite 保持双份备份（Step 0）。
6. **Step 5 — 1.2.3 console 审计时间线（console 仓，读侧）**：先 3.2 scope 收窄（sverp-audit 运行时最小集 + 负向 403 测试）→ console 审计页改调 governance 读 API（分页/source 过滤/actor）。兼容窗口：governance 读 API 契约已实证（TESTING_GUIDE §4）；governance 预期零改动（跨仓契约走 Change Manifest）。回滚：console 侧回退到本地环（debug 标签）。验证：质检单创建 → drainer 窗口内事件进 console 时间线（source=sverp-web, actor=admin）；console 无直连 DB。
7. **Step 6 — 审计可靠性硬化（H1/H2/M2）**：补偿扫描器（H1）；app.sqlite `synchronous=FULL`（H2）；outbox published 清理 job + governance retention 策略验证（M2）。兼容窗口：FULL 无契约变化，仅性能特征（单进程低频写，可忽略）。回滚：NORMAL 还原。验证：kill -9 注入测试账本完整性；`pending=0 AND permanent=0` 24h；published 行数曲线下降。
8. **Step 7 — 1.2.6 M5 通知扩展 / 1.2.7 aero-id 组织（不排期）**：M5 无 DB 变更（aero-im 通道 API，best_effort 已 Verified；补频道消息保留策略）；1.2.7 触发时先修 aero-id F-4 双路径再接投影，组织数据走契约定义 + 投影（不每请求调 aero-id）。

**总回滚纪律**：所有步骤为加法；回滚只回拨开关/删除新配置，不迁移数据；唯一不可逆点是 Step 4 关闭 SQLite 读路径——该动作前必须双备份 + 全量校验一致。

## 5. Unknown volume, retention, and recovery assumptions

- **Volume（Unknown，需测量）**：无任何工作负载证据——ERP 事件/日、账本增长曲线、媒体字节量、outbox 峰值。governance `BenchmarkIngest` 容量包络（v2 F-1）仍未测量；L2 宣布前应发布包络（10³/10⁴/10⁵ 事件的单事件 ingest p99 与快照字节/事件），任何容量断言（"ERP 量级可接受"）都以它为据。
- **Retention（Unknown）**：ERP 租户 retention_class 策略的实际 hot/warm/archive 值未验证；outbox published 行无保留（M2）；IM 频道消息无保留策略；vault `delivered_origins` 无界（F-16）。合规需求未提供。
- **Recovery（Missing）**：governance 无备份/恢复演练、文件后端无 PITR 故事；snaplink 签名密钥**当前不可备份**（进程内存，C2——1.2.1 落地后密钥文件入备份）；sso-ctl snapshot 未演练；PG 恢复未演练；aero-id append-only 触发器的恢复必须 insert-based（v2 记录）——演练文档未存在。
- **Deployed config（Partial）**：服务器侧 `shared/config.yaml`（snaplink 后端实际值、vault 配置、governance retention）与生产 `.env` 内容本工作区不可见；证据 05 的 "sqlite+JWT" 作为部署事实接受，但二进制默认值（H3）仍以代码为准。
- **Concurrency（Unknown）**：sverpweb 单进程多 handle 共享 app.sqlite 在 ERP 并发下的表现无负载证据（busy_timeout 5s 是唯一护栏）；governance 单写者假设在多 replica 场景未测（F-7 500-on-conflict 仍在）。

## 6. What the roadmap gets right and one correction

定序正确且与代码证据吻合：备份先行后移 vault 迁移、1.2.1 根因（非 timer 表面治理）、1.2.3 读侧零数据风险、4.3 #1 governance 最高优先级、3.3 的 drainer 稳态指标（pending=0/permanent=0 24h）作为 L2 指标——全部与本文发现一致。**两处修正**：① 1.2.1 是 snaplink 仓**新代码能力**（密钥文件），不是配置修复，owner 需按实现项目排期，且密钥文件必须进 4.3 备份范围与 systemd 写路径；② 4.3 表格中 "snaplink sqlite（sso db + 密钥）" 表述不准确——签名密钥今日不在 sqlite 中（进程内存），sqlite 备份救不了密钥轮换，两者是不同资产。

Advisory only：本审查不批准、不阻塞任何发布；它把 C1/C2 设为 L2 宣布的硬前置，并给出 H1 补偿扫描器与 M2 保留策略的可执行验收，供 1.2.x owner 决策。
