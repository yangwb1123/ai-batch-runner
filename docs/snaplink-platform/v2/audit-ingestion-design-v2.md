# Snaplink Platform — Per-Service Audit Ingestion Design (v2 Revision)

> **Status**: DRAFT v2 — revision of `audit-ingestion-design-draft.md`
> (v1) incorporating gate findings **F3/H1, F5, F-01, S1** (mandated) plus
> cross-doc alignment with `v2/audit-event-registry-v2.md` (§1.9
> idempotency, S2 claims, F-06/F13 self-audit) and
> `v2/scope-matrix-v2.md` (F2 scope name). Companion docs:
> `audit-event-registry-v2.md` (121 event types), `scope-matrix-v2.md`
> (scopes/grants), `revision-plan.md` (gate unblock plan).
> **Evidence standard**: connector existence is Verified where cited;
> all schema/behavior details are `[PROPOSED]` until validated against
> each codebase. Revisions vs v1 are marked `[v2]` (log in §0.1).
> **Every service section ends with an implementation-prerequisite
> task list (repo / change / acceptance assertion)** `[v2]` — these are
> recorded prerequisites that gate rollout (P1–P3 cutover), not blockers
> of contract approval (approve-as-target-state per revision-plan.md).

## 0. Verified anchors

| Anchor | Source |
|---|---|
| aero-id connector: `internal/connector/auditgovernance/` (publisher/oauth) — the Outbox+publisher reference pattern | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| aero-vault: `internal/auditgovernance/token.go` (Snaplink browser-flow token source) | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| snaplink: local audit-log concept only, no governance connector wired in `cmd/sso-server` | `SCENARIO_ANALYSIS.md` §0; database F-02 (Verified gap) |
| aero-im: no audit connector | `SCENARIO_ANALYSIS.md` §0 (Verified gap) |
| Outbox in the business transaction; audit never blocks business flow; L0/L1/L2 tiers; sync minimal audit ack only for extreme-risk ops (tenant delete / master-key reset) | `COMBINATION_MODEL.md` §3 (Proposed, cited) |
| 04-composition: audit + notify events generated in the same transaction as the file delete; outbox sits inside vault's transaction | `decisions/04-composition.md` (Verified decision) |
| Event envelope: id/type/version/source/tenant_id/time/trace_id/correlation_id/data (CloudEvents style); ingest idempotent by `event_id`; tenant consistency check | `COMBINATION_MODEL.md` §4; `scope-matrix-v2.md` Flow 3 (Proposed, cited) |
| **Ingest scope is `audit:event:write`** in every implementation (sink `require()`, aero-id `validOAuthBinding`, vault `RequiredScope`) `[v2]` | Verified (F2) |
| Access-token claim contract: `kid`/`iss`/`aud`/`scope`/`client_id`/`azp`/`tenant_id`/`roles` — sink `parseJWT` consumes these; IdP does not yet mint them `[v2]` | Verified (partial) — security S2, protocol F-2 |
| Sink dev-token auth defaults ON (`AUDIT_ALLOW_DEV_AUTH` true); only exercised trust path `[v2]` | Verified (S1) |
| aero-vault relay: no dead state, retries forever; `Ready()` fails on backlog > 900s or poison row → 503 `[v2]` | Verified (F3/H1) |
| aero-id: 8 post-commit `_ = Record(...)` discard sites (silent loss); inline publish double-delivers `[v2]` | Verified (F5/F6) |
| Sink store: whole-snapshot RMW, `ErrSnapshotConflict` → bare 500, no retry; file backend no fsync; relational `ledger_events` schema unused `[v2]` | Verified (F-01/H5) |
| Deterministic `event_id` per registry v2 §1.9: `SHA-256(source_service \| tenant_id \| event_type \| resource_id \| time_bucket)[:32]` `[v2]` | Proposed (F4) |
| Self-audit writer + read-path self-audit (`audit.event.read`/`audit.event.export`) — Missing in implementation; sink P0 `[v2]` | Verified gap (F13/F-06) |
| OIDC discovery metadata mis-advertises endpoints (no `/authorize`, `/revoke` 404, `token_endpoint=/authenticate`) `[v2]` | Verified defect (F-3) |

### 0.1 v2 revision log

| Blocking item | Change | Location |
|---|---|---|
| S1 | New §1.7 production trust posture: dev-token auth default OFF, dev-only config refused, manifest scan; trust path = IdP-issued JWT (JWKS) with claim contract | §1.7, sink § |
| S2 | Relay/ingest auth references access-token claim contract (`tenant_id`/`roles` REQUIRED); tenant consistency via token `tenant_id` claim | §1.2, sink § |
| F2 | Scope name `audit:event:ingest` → `audit:event:write` (all occurrences) | §1.2, sink §, aero-im § |
| F3/H1 | §1.4 normative dead-letter: poison rows MUST reach dead state with retry cap; readiness decoupling clause (degraded + alert at maxLag×0.5, never 503); vault task list | §1.4, vault § |
| F4 | §1.3 rewritten to reference registry v2 §1.9 (deterministic ID + dedup keys + regeneration rule + anti-squatting) | §1.3 |
| F5 | aero-id path inventory (in-tx vs 8 post-commit sites); mandate: move into business tx or observable failure; inline publish removed | aero-id § |
| F-01 | Sink §: optimistic-lock conflict retry (bounded jitter), `statusForError` maps conflict, fsync or documented loss window, `/readyz` store probe, relational-ledger-or-budget decision | sink § |
| F-06/F13 | Console re-labeled local-only; sink self-audit writer + read self-audit normative P0; supported topology declared | console §, sink § |
| F-3 | snaplink §: discovery fix task (endpoints truthfulness + conformance test) | snaplink § |
| F12 | §1.2 batch/backoff stated as per-service operational parameters, not contract constants | §1.2 |
| — | Implementation-prerequisite task lists per service (repo/change/acceptance) | each service § |

## 1. Common design (applies to all producer services)

### 1.1 Outbox table schema `[PROPOSED]` (contract = semantics, not DDL)

```sql
CREATE TABLE audit_outbox (
  id               BIGSERIAL PRIMARY KEY,          -- local order (FIFO per service)
  event_id         VARCHAR(128) NOT NULL,          -- deterministic idempotency key (registry v2 §1.9)
  event_type       VARCHAR(128) NOT NULL,          -- registry id (e.g. file.delete.admin)
  event_version    INT NOT NULL DEFAULT 1,         -- registry schema version
  tenant_id        VARCHAR(64)  NOT NULL,
  payload          JSONB        NOT NULL,          -- envelope + event-specific fields
  status           SMALLINT     NOT NULL DEFAULT 0,-- 0=pending 1=sending 2=sent 3=dead (normative `[v2]`)
  attempt_count    INT          NOT NULL DEFAULT 0,
  next_attempt_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  last_error       TEXT,
  sent_at          TIMESTAMPTZ,
  lease_until      TIMESTAMPTZ,                    -- worker lease (crash-safe)
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
  UNIQUE (service, event_id)                       -- service column fixed per DB
);
CREATE INDEX idx_outbox_due ON audit_outbox (status, next_attempt_at) WHERE status IN (0,1);
```

- Written **in the same transaction** as the business operation (per
  `COMBINATION_MODEL.md` §3). No business flow awaits the relay.
- **Status semantics are the contract `[v2]`**: `3=dead` is terminal —
  every outbox MUST reach a dead state for permanent failures and MUST
  have a retry cap for transient ones (never retry forever). Per-repo DDL
  differs (Verified): vault `audit_governance_outbox` currently has
  **no status column** (`delivered_at_ns` only) → migration 0039
  extension required; aero-id `audit_publications` has status +
  `dead_letter` after maxAttempts=8 (all classes — needs permanent-class
  immediate death).
- **Uniqueness contract `[v2]` (F4/F12)**: one row per occurrence;
  regeneration anchors — vault `UNIQUE (origin_kind, origin_id)`,
  aero-id `UNIQUE (tenant_id, source, event_id)` / `UNIQUE (fact_id)`.
  `event_id` derived per registry v2 §1.9; regeneration MUST reuse it.
- `payload` is metadata-only (registry §1.7): no file/message content.
- Table name/local column conventions follow each service's DB style.

### 1.2 Relay/worker behavior `[PROPOSED]`

- Single-writer per service: one worker (or N workers partitioned by
  `hash(event_id) % N` with per-partition FIFO). Claim via
  `SELECT ... WHERE status IN (0,1) AND next_attempt_at <= now()
  ORDER BY id LIMIT batch FOR UPDATE SKIP LOCKED` (or lease_until
  stamping).
- **Batch size, poll interval, lease TTL, backoff caps are per-service
  operational parameters, not contract constants `[v2]` (F12)** —
  verified defaults: aero-id batch 50 / lease 5min / backoff 1s→1h cap;
  vault batch 32 / claim 30s / backoff 1s→300s cap. The contract is the
  *semantics*: lease-based claiming, jittered exponential backoff,
  rate/network class scattered 30–60s, **never drop** (with the
  observability carve-out in §1.4), L1 aggregation for ‡ events.
- Auth: `client_credentials` via the connector's oauth token source
  (aero-id `publisher/oauth`, aero-vault `token.go` pattern); token
  refresh before expiry; single token shared across batches. Tokens MUST
  carry the claim contract (Flow 2 of scope-matrix-v2): `kid`, `iss`,
  `aud`, `scope`, `client_id`/`azp`, `tenant_id`, `roles` `[v2]` (S2).
- Delivery: `POST <audit-governance>/v1/events` (or gRPC) with the
  **`audit:event:write`** scope (scope-matrix-v2 Flow 3) `[v2]` (F2).
- **401/403 handling `[v2]` (F8)**: relay MUST invalidate its cached
  token on 401 and refetch (vault does; aero-id does not — required);
  scope-missing → alert, not infinite retry.
- Ordering: FIFO by `id` per partition; per-tenant ordering is best-effort
  (sink assigns `ledger_seq`).
- High-volume classes (`auth.token.issue` ‡, `message.send` ‡,
  `notification.send` ‡): L1 aggregation — relay sends summaries, details
  stay in local logs (registry §1.8).
- Extreme-risk ops (tenant delete, master-key reset): synchronous minimal
  audit ack + async full event (per `COMBINATION_MODEL.md` §3).

### 1.3 Idempotency keys `[v2]` (F4 — normative, per registry v2 §1.9)

`event_id = SHA-256(source_service | tenant_id | event_type | resource_id | time_bucket)[:32]`

- `resource_id` — stable business anchor: `session_id`/`request_id` for
  auth flows, `file_id|version` for file ops, `account_id` for account
  ops, `message_id|edit_seq` for IM, `request_id` for admin ops.
- **Distinct-occurrence rule**: two distinct occurrences of the same
  (source, tenant, action, resource) must never share a bucket — extend
  `time_bucket` with a per-occurrence component (`request_id` /
  counter) where needed (e.g., downloads: `file_id|request_id`).
- Retries **reuse** the same `event_id` (derived, never regenerated per
  attempt). **Regeneration** (gap-reconcile/replay/row-clear) MUST reuse
  the original `event_id`; a new ID for the same occurrence is a contract
  violation (vault `(origin_kind, origin_id)` / aero-id
  `(tenant_id, source, event_id)` are the anchors).
- Anti-squatting: `time_bucket` is not attacker-predictable; sink
  enforces claim-then-verify (same `event_id` + different digest → 409).
- **Transition `[v2]`**: while producers mint random UUIDs, sink dedup is
  vacuous on regeneration — deterministic IDs are a **P2 dual-write
  cutover prerequisite**. Sink dedup key: `(source_service, tenant_id,
  event_id)` + tenant-scoped `idempotency_key`.

### 1.4 Dead-letter handling `[v2]` (normative — F3)

- **Permanent failures** (schema validation / unknown event_type / tenant
  mismatch / payload too large / unparseable / sink 422 / sink 409
  content-conflict) → **`status=3 (dead)` immediately (or ≤1
  classification attempt)**, `last_error` recorded; batch continues with
  remaining rows (poison event must not block the queue).
- **Transient failures** (5xx / 429 / network / timeout) → bounded retry
  with jittered exponential backoff, cap 300s (or per-service verified
  cap). **Every outbox MUST have a retry cap; retry-forever is a contract
  violation** (vault today).
- **Readiness decoupling clause (H1)**: audit-pipeline health (backlog
  lag, poison rows, dead-letter count) MUST NOT gate any core service
  readiness. Backlog/poison state reports `degraded` + **alert at
  maxLag×0.5**, never a `/readyz` 503 flip; read-path degradation is a
  timeout/slowdown, not an outage signal. Dead rows are excluded from
  lag/readiness calculations.
- Dead rows are visible to operators; replay tool flips `3→0` after fix
  (aero-id has no replay tool today — required; vault none — required).
- Relay emits `audit.outbox.failure` (permanent failure) and
  `audit.relay.dlq.replay` (replay) events per registry — **but only if
  the sink is reachable**; otherwise the failure is local-logged.
- DLQ alerting threshold: `dead_count > 0` for >15min or `attempt_count`
  stuck >24h → alert channel (aero-im notification is a delivery channel
  only; the fact is the ledger/local log).
- **Observability carve-out (F5)**: any audit-record path that is not
  in-tx MUST make failure observable (metric + alert) and **never
  discard the error** (`_ =` is forbidden).

### 1.5 Common failure modes

| Failure | Detection | Handling |
|---|---|---|
| Ingest down / 5xx / network | non-2xx, timeout | backoff, retry, never drop; business flow unaffected (outbox) |
| 429 rate limit | header/status | jittered backoff 30–60s (rate class) |
| Auth failure (token/scope) | 401/403 | **invalidate token + refetch (F8)**; if scope missing → alert (scope grant problem, not retryable forever) `[v2]` |
| Duplicate delivery | sink dedup ack | treat as success |
| Schema/validation 4xx | 422 | **→ dead immediately** (normative §1.4) `[v2]` |
| Tenant mismatch | 422/403 at sink | → dead + alert (possible misrouting/compromise) |
| Poison event | repeated failure of one row | → dead, batch continues (normative §1.4) `[v2]` |
| Crash between send and mark-sent | lease expiry, row still pending | at-least-once redelivery; sink dedup absorbs |
| Sink clock skew | occurred_at vs sink time | sink rejects/annotates skew >5min `[PROPOSED]` |
| **Audit backlog/poison flags core readiness (vault)** | `/readyz` 503 | **Forbidden — degraded + alert at maxLag×0.5 (H1)** `[v2]` |
| Sink store optimistic-lock conflict | `ErrSnapshotConflict` → 500 today | **sink retries with bounded jitter; no bare 500 escape (F-01)** `[v2]` |
| Queue growth | row count / lag metric | alert; throttle business writes only as last resort |
| Local DB loss | — | outbox rows lost with business tx (unavoidable; ledger gap detected by integrity spot-checks) `[PROPOSED]` |

### 1.6 Migration phases (common skeleton) `[v2]`

1. **P0 — sink readiness (expanded `[v2]`)**: ingest API + dedup +
   ADR-0002 schema validation + tenant check live; **dev-token auth OFF
   (S1)**; **conflict retry / single-writer topology (F-01)**;
   **self-audit writer + read-path self-audit (F13/F-06)**; readyz store
   probe.
2. **P1 — schema + relay behind flag**: outbox DDL (**vault: add
   status/dead columns**) ; relay in log-only/dry-run mode; **deterministic
   event_id implemented (F4)**; **readiness decoupling (H1)**.
3. **P2 — dual write**: business transactions begin writing outbox rows
   (same tx); relay enabled (L2) for non-critical event classes first.
4. **P3 — cutover**: all event classes; local audit log remains as L0
   complement/fallback, not a second truth.
5. **P4 — backfill** `[PROPOSED]` (scope decision needed): bounded,
   idempotent import of legacy local audit data; rows lacking required
   fields (tenant_id, actor) are skipped or marked `[PROPOSED]` in
   payload, never fabricated.
6. **P5 — gate**: DLQ/alert monitoring live; new write/admin op without
   event type fails review (registry gate).

### 1.7 Production trust posture `[v2]` (S1 — normative)

- The platform's **only** production trust path is snaplink-issued JWT
  (Ed25519/ES/RS/PS, verified via JWKS with the hardened verifier).
- Dev-token auth (`AUDIT_ALLOW_DEV_AUTH`) is a **test fixture**:
  default **false**; `dev:` tokens rejected on every route when off;
  startup **refused** when dev-auth is the only configured trust source;
  deployment manifests must set it false and are CI-scanned.
- Access tokens must carry the claim contract (kid/iss/aud/scope/
  client_id/azp/tenant_id/roles); tokens lacking `tenant_id`/`roles`
  for tenant-scoped principals are rejected (S2).
- Conformance gates: T-1.1 (dev token → 401 when off), T-1.2 (cross-repo
  e2e: `/token` client_credentials → ingest 202 `ledgered` → read 200 →
  export 202, all with `AUDIT_ALLOW_DEV_AUTH=false`).

---

## snaplink (IdP — Go)

**Connector status**: **none today** — local audit-log concept only
(Verified gap); `infrastructure/auditgovernance` used only by
provisioner/billing, not `cmd/sso-server`. This section is entirely
`[PROPOSED]` except the trust-root obligations (S2/S1) which are
**normative** `[v2]`.

- **Trust-root obligations `[v2]` (S2/S1)**: mint access tokens per the
  claim contract (kid/iss/aud/scope/client_id/azp/tenant_id/roles) —
  `buildAccessPayload` (all three issuers) + `generator.go` header;
  register scopes (scope registry, P0-3) and reject unregistered scope
  requests; fix OIDC discovery metadata truthfulness (F-3).
- **Outbox**: `audit_outbox` table in the snaplink DB. Auth-flow rows
  (`auth.*`) written in the auth transaction where feasible; login
  success/failure rows written post-commit keyed by `request_id`
  (session may not exist on failure) — the **only sanctioned post-commit
  class**; failure must be observable (never `_ =`) `[v2]`.
- **Relay**: Go worker mirroring the aero-id publisher pattern; token via
  its own `client_credentials` client (`snaplink` → audit-governance)
  with the claim contract.
- **Idempotency**: per registry v2 §1.9 — `resource_id = request_id` for
  login/flow events; `session_id` for session/token events;
  `role_id|subject_id` for grants/revokes.
- **DLQ/failures**: common §1.4–1.5; special case — `auth.token.issue` ‡
  never enters the outbox (L1 aggregation only), `auth.login.failure` is
  C2 ledgered (security monitoring).
- **Sync-minimal-audit**: `tenant.delete` and platform client-secret
  rotation require sync minimal ack before commit (`COMBINATION_MODEL.md` §3).
- **Migration**: P0 sink-ready → P1 table+flag → P2 dual-write
  non-sensitive classes (`auth.logout`, `auth.device.*`) → P3 full
  cutover → P4 backfill `[PROPOSED]` (risk: historical entries lack
  tenant/actor fields; default: skip) → P5 gate.

### 实现前置任务清单（snaplink）`[v2]`

| 任务 | 仓库/位置 | 验收断言 |
|---|---|---|
| Access-token claim 契约落地（tenant_id + roles + kid + iss） | snaplink `infrastructure/defaultimpl/issue_payload.go` `buildAccessPayload`；`token/jwt/generator.go:52-55` header | T-8 row (a)：`POST /token` 响应 header 含 kid、claims 含 iss/scope/tenant_id/roles/client_id；T-1.2 e2e 全绿 |
| Scope registry（P0-3） | snaplink token endpoint scope 校验 | T-8 row (d)：未注册 scope → 400 |
| OIDC discovery 真值化 | snaplink `cmd/server/routes.go:63-77,663-684`；`routes_test.go:41` 重写 | T-2：discovery sweep 全绿；`token_endpoint == "/token"`；无广告端点 404 |
| 治理连接器（outbox+relay，新 cmd wiring） | snaplink `cmd/`（现有 `audit.Sink`/`BatchSink` 接口可复用） | P2 双写 parity 检查；`auth.token.issue` L1 聚合生效 |

---

## snaplink-console (Flutter admin console)

**Connector status `[v2]` (F-06)**: **local-only today (Verified)** —
`audit_log_service.dart` is a browser `localStorage` 1000-ring recording
local ops; **reads no audit API**. v1's "consumer only (Verified)" was
inaccurate. **No outbox needed**; the consumer role (reading the ledger
via the sink read API) is the **target state**.

- **Design**: console-native facts are generated at the edge, not in the
  console:
  - console logins → snaplink `auth.login.*` events with
    `client_id=console` (console adds no duplicate event).
  - console audit queries → **audit-governance emits `audit.event.read` /
    `audit.event.export` self-audit rows on the query API** (ledger
    records who read what — no console-side table, cannot be bypassed by
    direct API calls). **This is a sink P0 deliverable (F-06/F13)**;
    console displays server-side records only. `[v2]`
  - `console.session.revoke` → snaplink `auth.session.revoke` via the
    BFF's user session.
- **Failure modes**: query failures are console-side UX concerns; the
  ledger record of the query attempt is written by audit-governance
  itself (fail-closed on read events only if the read succeeded).
- **Migration**: none (no schema); verify the audit page queries carry
  tenant + trace_id; no backfill.

### 实现前置任务清单（snaplink-console）`[v2]`

| 任务 | 仓库/位置 | 验收断言 |
|---|---|---|
| 审计页接 sink 读 API（替换 localStorage ring 展示） | snaplink-console `lib/services/audit_log_service.dart` + `portal_api.dart` | T-12：查询后 sink 出现调用者的 `audit.event.read` 行；console 只显示服务端记录（localStorage 不再作为证据） |
| 依赖：sink read self-audit 先落地 | sink（P0，见下） | 依赖门禁：sink T-12 先绿 |

---

## snaplink-audit-governance (Go — ledger)

**Connector status**: it **is** the sink — no outbox/relay for its own
ops; uses a **self-audit writer** (normative P0, `[v2]` F13).

- **Self-audit writer `[v2]` (F13, normative)**: governance operations
  (legal hold apply/release, export request/complete/failed, retention
  update, checkpoint, integrity verify) append their self-audit events
  (`audit.legalhold.*`, `audit.export.*`, `audit.retention.*`,
  `audit.ledger.checkpoint`, `audit.integrity.*`) **in the same store
  update** as the operation; append failure → operation **aborts**
  (fail-closed — governance ops are the one place sync audit is
  correct). Today only internal `adminAction` mutation records exist
  (Verified); registry-conformant events + read-path self-audit are
  Missing. Checkpoint sealing is a `system:<audit-governance>` event
  written by the checkpoint job.
- **Read-path self-audit `[v2]` (F-06)**: `QueryEvents`/`getEvent`/
  `downloadExport` append `audit.event.read` / `audit.event.export` in
  the same store update as the served query (actor = caller).
- **Supported ingest topology `[v2]`**: HTTP ingest API + self-audit
  writer. The sink's outbox relay / Kafka consumer are **unwired** and
  are NOT a supported ingest path in this contract (wire or remove).
- **Trust `[v2]` (S1)**: `AUDIT_ALLOW_DEV_AUTH` default **false**;
  dev-only config refused at startup; manifests scanned in CI; production
  verification path = IdP JWT per claim contract.
- **Store `[v2]` (F-01)**: snapshot store must gain bounded jittered
  retry on `ErrSnapshotConflict` and `statusForError` must map the
  conflict to a domain error (no bare 500); file backend fsync (or
  documented loss window); `/readyz` MUST probe the store (today only
  archive). Production target: relational ledger on migration-001 tables
  (per-tenant `ledger_events` insert + receipt + segment sealing in a
  real tx, `FOR UPDATE` on `ledger_streams`), **or** an explicit measured
  capacity budget (`BenchmarkIngest` envelope); high-volume classes'
  cutover is gated on this decision.
- **Idempotency**: per registry v2 §1.9 — `resource_id = operation_id`
  (hold_id, export_id, policy_id, checkpoint_id); sink dedup
  `(source_service, tenant_id, event_id)` + `idempotency_key`
  (same digest → 202 Duplicate; different digest → 409).
- **Ingest API contract** (the sink side, consumed by all producers):
  - Auth: publisher `client_credentials` JWT per claim contract with
    **`audit:event:write`** scope `[v2]` (F2); client allowlist; **dev
    tokens rejected (S1)** `[v2]`.
  - Dedup: `(source_service, tenant_id, event_id)` — duplicate → success
    ack; conflict → 409.
  - Validation: ADR-0002 schema registry (event_type known, version
    supported, required fields incl. `tenant_id`/`actor.roles` (S2),
    metadata-only payloads); failure → 422 with reason (producer marks
    row dead).
  - Tenant consistency: envelope `tenant_id` must match publisher token
    `tenant_id` claim (S2) `[v2]`; mismatch → 422 (scope-matrix Flow 3).
  - Skew: `occurred_at` >5min in future → reject; >5min past → annotate.
- **DLQ**: sink-side rejections are the producer's DLQ trigger; ledger
  itself has no DLQ.
- **Failure modes**: append failure → governance op aborts (no partial
  legal hold); ingest surge → 429 + bounded queue; hash-chain checkpoint
  failure → alert, ledger keeps accepting (integrity risk surfaced by
  `audit.integrity.verify.failure`).
- **Migration**: P0 first (it is P0); implement self-audit writer +
  read self-audit + trust posture + store hardening before any producer
  cutover.

### 实现前置任务清单（snaplink-audit-governance）`[v2]`

| 任务 | 仓库/位置 | 验收断言 |
|---|---|---|
| Dev-auth 默认关闭 + config 拒绝 + manifest 扫描 | sink `cmd/audit-api/main.go:45`；`internal/auth/auth.go` `ValidateConfiguration`；`deploy/docker-compose.verify.yml` | T-1.1：`AllowDev=false` 时 `dev:platform:platform-admin` → 401；dev-only 配置启动报错；扫描在任一生产 manifest 出现 true 时失败 |
| 快照存储冲突重试 + 错误映射 + fsync + readyz store probe | sink `internal/store/store.go` / `postgres.go`；`internal/httpapi/server.go` `statusForError` | T-6：2 并发 ingest（等 digest）→ 一行 + 一 202 一 Duplicate，无 500 逃逸；停 PG → `/readyz` 503（今天 200） |
| Relational ledger（migration-001）或显式容量预算 | sink `internal/store/` | `BenchmarkIngest` 记录 envelope（events/sec + p99）；高容量类 cutover 门禁 |
| Self-audit writer（治理操作 same-tx，fail-closed） | sink `internal/service/`（复用 `adminAction` 原子模式） | 治理 append 失败 → 操作回滚；`audit.legalhold.apply` 等事件入账 |
| Read-path self-audit（`audit.event.read`/`audit.event.export`） | sink `internal/service/service.go` `QueryEvents`/export 路径 | T-12：查询后出现调用者 self-audit 行 |
| 拓扑收敛：接线或移除 outbox relay / Kafka consumer | sink `cmd/audit-outbox-relay`、`cmd/audit-kafka-consumer`、`internal/outbox/` | 契约声明外无活动入站路径 |
| 跨仓 e2e（生产 token 路径） | sink `test/e2e/fullstack.sh`（替换 dev token） | T-1.2：`AUDIT_ALLOW_DEV_AUTH=false` + IdP 签发 token → ingest 202 ledgered / read 200 / export 202 |

---

## aero-id (Go — account domain)

**Connector status**: **exists (Verified)** —
`internal/connector/auditgovernance/` (publisher/oauth). Details below
`[PROPOSED]` until validated against the actual connector.

- **In-tx recording `[v2]` (F5, normative)**: ALL account-domain audit
  records MUST be written in the business transaction (`AppendInTx`).
  Verified path inventory:

  | Path | Status (Verified) | v2 requirement |
  |---|---|---|
  | repository paths: `account_profile.go`, `operation.go`, `sync_job.go`, `event_receipt.go`, `operation_audit.go` | in-tx (`AppendInTx`) | keep |
  | `service/account.go:155,184` | post-commit `_ = Record(...)` — silent loss | move in-tx or observable failure (metric + alert; `_ =` forbidden) |
  | `callback.go:160,221,247` | post-commit discard | same |
  | `audit_hold.go:59,74` | post-commit discard | same |
  | `legacy_cutover.go:88` | post-commit discard | same |

- **Single delivery path `[v2]` (F6)**: the outbox worker is the ONLY
  delivery path; remove the inline `publisher.Publish` in `Record` (or
  degrade to best-effort async with its own short timeout). Today every
  event delivers twice (inline + durable worker) and requests pay up to
  5s sink latency.
- **Token lifecycle `[v2]` (F8)**: invalidate cached token on 401 and
  refetch (vault pattern); scope-missing → alert.
- **Outbox**: `audit_outbox` (existing `audit_publications`); account
  rows (`account.*`, `admin.account.*`) written in the account
  transaction.
- **Relay**: the existing publisher worker; token via `publisher/oauth`
  (Snaplink client_credentials) with the claim contract; verify
  lease/batch/backoff semantics per §1.2 (batch 50, lease 5min,
  backoff 1s→1h cap — all acceptable operational parameters `[v2]`).
- **Idempotency**: per registry v2 §1.9 — `resource_id = account_id`
  for updates, `account_id|consent_id` for consent, `request_id` for
  admin ops; 3 call sites currently mint random IDs without an
  `EventID` (`account.go:155,184`, `audit_hold.go:59`,
  `legacy_cutover.go:88`) → derive from anchor `[v2]` (F4).
- **DLQ/failures**: common §1.4–1.5 — 422/409 → dead **immediately**
  (today: 8 attempts for every class); 5xx/429 keep bounded retry;
  dead rows need a **replay tool** (today: TTL cleaner purges them
  silently).
- **Migration**: P0 sink-ready → P1 verify table matches connector
  expectations (or add DDL) + dry-run → P2 enable non-sensitive
  (`account.update`) → P3 full cutover → P4 backfill `[PROPOSED]`
  (bounded; skip rows without tenant/actor) → P5 gate. **Activation gate
  (F-03)**: `audit_governance.enabled: true` only after per-tenant
  bindings exist (boot fail-closed is the safety gate); "fastest
  rollout" claim is code-true, deployment-false until the flag flips.

### 实现前置任务清单（aero-id）`[v2]`

| 任务 | 仓库/位置 | 验收断言 |
|---|---|---|
| 8 个 post-commit 调用点移入业务事务（`AppendInTx`）或可观测失败 | aero-id `service/account.go:155,184`、`callback.go:160,221,247`、`audit_hold.go:59,74`、`legacy_cutover.go:88` | T-5：mock `Record` 失败 → `ObserveAuditIngestFailure` 递增或操作失败；无 `_ =` 调用点 |
| 单一投递路径（删 inline publish） | aero-id `audit_ledger.go` `Record` | 同一事件仅一条投递路径；请求路径无 sink 延迟（F6 drill） |
| 401 → token 失效 | aero-id `connector/auditgovernance/oauth.go` | T-10.1：revoke 后下一次 publish 立即刷新（不等到 TTL） |
| 稳定 event_id（3 个无 EventID 调用点） | aero-id `service/account.go:155,184`、`audit_hold.go:59`、`legacy_cutover.go:88` | T-4（与 vault 联合）：再生成 fact 在 sink 折叠为 Duplicate |
| 连接器显式激活（bindings + flag） | aero-id `configs/config.yaml` `audit_governance.enabled` | F-03：空 bindings + enabled → boot 报错（fail-closed）；首个事件 e2e 验证 |

---

## aero-vault (Go — file platform)

**Connector status**: **scaffolding exists (Verified)** —
`internal/auditgovernance/token.go` (tokenSource). Details `[PROPOSED]`.

- **Dead-letter state `[v2]` (F3, normative)**: `audit_governance_outbox`
  MUST gain a terminal dead state — migration 0039 extension: status /
  `dead_at` column + partial index; `relay.go` `retryFact` MUST classify
  permanent failures (sink 422 / 409 / tenant mismatch / invalid receipt)
  → terminal state immediately, transient (5xx/429/network) → bounded
  jittered retry with cap 300s; **retry-forever is a contract
  violation**. Port the sink outbox package's `DeliveryError.Permanent`
  classification. Dead rows excluded from
  `OldestPendingAuditGovernance`.
- **Readiness decoupling `[v2]` (H1, normative)**: audit backlog /
  poison rows MUST NOT flip `Ready()` / `/readyz` 503. Replace the
  maxLag gate with `degraded` reporting + **alert at maxLag×0.5**
  (450s at default 900s); one poison row must never take the file
  platform down (read-timeout degradation, not outage).
- **Relay metrics `[v2]` (H6)**: delivered/failed/dead/oldest-age
  counters (today: zero Observe calls in the relay).
- **Deterministic fact IDs `[v2]` (F4)**: `facts.go`
  (`factFromEvent`/`factFromAudit`/`factFromGap`) derive `event_id` per
  registry v2 §1.9 from the origin anchor (`origin_kind|origin_id` +
  time bucket); regeneration (gap reconcile) must reuse it — sink dedup
  then collapses regenerated facts.
- **Outbox**: per `decisions/04-composition.md` the outbox write is
  **inside the business transaction** — for `file.delete.admin` the
  delete, the `file.delete.admin` audit row and the `file.deleted.notify`
  row are generated in the same tx (audit and notify are same-source, no
  second truth). `UNIQUE (origin_kind, origin_id)` is the regeneration
  anchor.
- **Relay**: Go worker using the `token.go` tokenSource; HTTP ingest;
  scope `audit:event:write` (Verified enforcement) `[v2]` (F2).
- **Idempotency**: per registry v2 §1.9 — `resource_id = file_id|version`
  for file content events, `file_id|request_id` for downloads (distinct
  occurrences!), `file_id|share_id` for share ops, `request_id` for
  admin ops.
- **DLQ/failures**: common §1.4–1.5; special case — `file.download` ‡
  volume: L1 aggregation decision pending (registry open question #3).
- **Migration**: P0 sink-ready → P1 DDL (**status/dead columns**) +
  relay dry-run → P2 dual-write (`file.metadata.update`,
  `file.share.create`) → P3 full cutover (incl. delete-flow tx
  integration) → P4 backfill `[PROPOSED]` (default: none initially —
  tombstone/history is vault-side per 04-composition C13) → P5 gate.
  **Activation gate (F-03)**: `AUDIT_GOVERNANCE_ENABLED=true` +
  `AUDIT_GOVERNANCE_BINDINGS_FILE` before boot; rollback safe by
  construction (`AuditGovernanceCanDisable`).

### 实现前置任务清单（aero-vault）`[v2]`

| 任务 | 仓库/位置 | 验收断言 |
|---|---|---|
| Outbox 终态（status/dead 列 + 永久错误分类 + 重试上限） | vault migration `0039_audit_governance_outbox`；`internal/auditgovernance/relay.go` | T-3：422 → 一个投递周期内进入终态；`Ready()` 含 dead 行时为 true |
| Ready() 与 audit backlog 解耦（degraded + maxLag×0.5 告警，绝不 503） | vault `internal/auditgovernance/runtime.go` `Ready()`；`cmd/server/audit_governance.go:52-59` | D1 drill：sink 停 60min → vault `/readyz` 保持 200；450s 处告警触发 |
| 确定性 fact ID | vault `internal/auditgovernance/facts.go` | T-4：清除行 → reconcile 再生成 → sink Duplicate 回执、`QueryEvents` 恰好 1 行 |
| Relay metrics（delivered/failed/dead/lag） | vault `internal/auditgovernance/relay.go`/`runtime.go` | 指标存在且可喂 H2 告警（今日 0） |
| 连接器显式激活（bindings + flag） | vault `internal/config/config_audit_governance.go`；部署清单 | F-03：启用以 bindings 文件为前置；首个事件 e2e 验证 |

---

## aero-im (Rust — messaging/live/notifications)

**Connector status**: **none today (Verified gap)** — no audit connector;
only local content-governance logic. Entire section `[PROPOSED]`.

- **Outbox**: `audit_outbox` table in the IM store (SQL if present, else
  the message store's durable table); rows for `message.*`, `room.*`,
  `live.*`, `notification.*`, `admin.*` written in the business
  transaction. `message.send` ‡ and `notification.send` ‡ never enter
  the outbox (L1 aggregation).
- **Relay**: new Rust worker (first connector in this stack) implementing
  §1.2; client_credentials via snaplink with the claim contract; scope
  **`audit:event:write`** `[v2]` (F2) — grant conditional on relay
  implementation (scope-matrix Flow 3).
- **Idempotency**: per registry v2 §1.9 — `resource_id = message_id|edit_seq`
  for edit/delete/retract, `room_id|member_uid` for membership,
  `stream_id` for live, `recording_id` for recordings, `request_id` for
  admin/moderation ops.
- **DLQ/failures**: common §1.4–1.5; special case — moderation events
  (`admin.content.flag`, `admin.moderation.action`) are C2/C1 and must be
  delivered before the outbox backlog drains: priority partition (flag
  column or separate table) `[PROPOSED]`.
- **Migration**: P0 sink-ready → P1 DDL + relay dry-run → P2 dual-write
  (`room.create`, `notification.template.update`) → P3 full cutover →
  P4 backfill of content-moderation history `[PROPOSED]` (bounded,
  metadata-only) → P5 gate.

### 实现前置任务清单（aero-im）`[v2]`

| 任务 | 仓库/位置 | 验收断言 |
|---|---|---|
| Outbox DDL + in-tx 写入 + Rust relay worker | aero-im `crates/aero-storage` + 新 connector crate | 30 个被忽略的 audit 测试在 CI（live PG）全绿；`cargo test` audit 组 37/37 |
| Moderation 优先级分区 | aero-im outbox schema（flag/partition） | moderation 事件先于 backlog 排空投递 |
| Scope grant（relay 存在后） | snaplink scope registry（P0-3） | T-11：无 relay 时配给被 fail-closed 拒绝（错误信息点名缺失 relay） |

---

## 2. Rollout order & dependencies `[v2]`

| Phase | Service | Depends on |
|---|---|---|
| 1 | snaplink-audit-governance (sink: trust posture + store hardening + self-audit + read self-audit) | ADR-0002 schema registry, scope registry; **S1/F-01/F13/F-06 tasks green** |
| 2 | aero-id (in-tx recording + single delivery + 401 invalidation) | sink live; **F5/F6/F8 tasks green** |
| 3 | aero-vault (dead state + readiness decoupling + deterministic IDs) | sink live, 04-composition tx design; **F3/H1/F4 tasks green** |
| 4 | snaplink (IdP — claim contract + discovery fix + largest surface, L1 aggregation for token.issue) | sink live, scope registry (P0-3); **S2/F-3 tasks green** |
| 5 | aero-im (new Rust connector) | sink live, `audit:event:write` grant after relay exists |
| 6 | snaplink-console (consumer verification; sink read self-audit) | sink self-audit read events (phase 1) |

## 3. Open decisions

1. **Backfill policy** `[PROPOSED]`: which services backfill legacy local
   logs (default: vault none, id/vault bounded, snaplink skipped unless
   fields exist). Needs compliance input.
2. **L1 aggregation thresholds** for `auth.token.issue`, `message.send`,
   `notification.send`, `file.download`, `file.rag.query` — summary
   granularity and whether summaries satisfy legal-hold needs; coupled
   with `time_bucket` granularity (registry v2 open question #8).
3. **aero-im priority partition** for moderation events — flag column vs
   separate table.
4. **Sink sync-ack scope**: confirm the exact extreme-risk op list
   (tenant delete, master-key reset, +?) per `COMBINATION_MODEL.md` §3.
5. **Tenant propagation** — **RESOLVED in v2**: access-token `tenant_id`
   claim per claim contract (scope-matrix-v2 Flow 2) `[v2]`.
6. **DLQ visibility**: whether `audit.outbox.failure`/`audit.relay.dlq.replay`
   become ledgered events (they must not be generated by a relay whose
   sink is down — local-log fallback specified).
7. **Sink capacity** (new, F-01): relational ledger (migration-001) vs
   explicit measured budget — decision gates high-volume class cutover
   `[v2]`.
8. **Connector hardening verification** (new): every task list row above
   must be verified against code (evidence standard) before its
   production cutover; `audit:event:write` enforcement is the only
   pre-verifiable item today `[v2]`.
