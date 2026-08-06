# Database Architect Review — Per-Service Audit Ingestion (Draft vs. Codebase Reality)

> **Reviewer role**: database architect (persistence correctness, production
> readiness, hot/durable store inventory, stock-binary wiring).
> **Input**: `docs/snaplink-platform/audit-ingestion-design-draft.md` (17KB,
> the evidence under review).
> **Checks performed (this revision)**: static code, config, migration, and
> deploy-manifest inspection at the paths cited below. No binaries were
> executed, no load tests were run, and no production metrics were available.
> **Evidence labels**: Verified (read in code/config at the cited path),
> Partial (implemented but diverging from the draft), Missing (absent),
> Proposed (draft-only), Unknown (no evidence in tree).

**Executive summary**: the draft's *verified anchors* hold (aero-id connector
exists, aero-vault relay exists, snaplink/aero-im have no governance
connector, console audit is client-side), but three material facts are
**missing from or mis-stated in the draft**: (1) the sink
(`snaplink-audit-governance`) keeps its **entire ledger in one JSON
snapshot row** — the relational `ledger_events` schema it ships in
`migrations/001` is unused — making every ingest an O(ledger-size)
read-modify-write with no retry on conflict; (2) **both producer connectors
are disabled by default in the stock binaries/configs** (aero-id
`audit_governance.enabled: false`, aero-vault `AUDIT_GOVERNANCE_ENABLED`
default false, and `cmd/sso-server` does not wire the snaplink connector at
all); (3) the console "consumer" claim is wrong — `audit_log_service.dart`
is a local `localStorage` ring, and the sink's query API records **no**
`audit.event.read` self-audit. Backoff/DLQ semantics of the reference
pattern also diverge from draft §1.2/§1.4 (no 422→dead in either relay;
vault has no dead state at all).

---

## 1. Store inventory (purpose, durability, implementation, stock wiring, consistency)

| Service / repo | Store | Purpose | Durability | Implementation | Stock binary wires it? | Consistency requirement |
|---|---|---|---|---|---|---|
| **snaplink (IdP)** `~/workspace/demo/snaplink` | `identity.backend` = memory (stock) \| sqlite \| postgres | users, clients, sessions, codes, consent | **Hot by default** (memory; loses DCR clients + opaque-session tokens on restart — config comment, `cmd/sso-server/config.yaml` L114); sqlite DSN docs `?_journal=WAL&_busy_timeout(5000)`; postgres pool shared (`pgDB`, `cmd/sso-server/main.go`) | **Verified** — stock `bin/config.yaml`/`cmd/sso-server/config.yaml` both `backend: memory` | Strong (session/token state must survive replica failover — sqlite/postgres required; config `server.topology.mode: single` refuses multi with per-pod state) |
| snaplink — audit L0 | `audit.backend` = memory (stock, 10k ring) \| sqlite \| postgres; sinks: webhook (RetryingSink), CEF/OCSF/syslog, Kafka (cold-profile build only), async wrapper, optional hash-chain | local audit log (L0) | **Hot by default** — every event lost on restart (`config_audit.go`, `cmd/sso-server/config.yaml` L127–135); sqlite durable; postgres `audit_events` (hash-chain `prev_hash/hash` columns, `infrastructure/postgres/audit_sink.go` L28–58) | **Verified** — `audit.enabled: true`, backend memory in stock config; `hash_chain: false` default | L0 is a complement, not truth; hash chain optional; webhook fan-out is bounded-retry then **drop** (`auditsink/retrying_sink.go` — returns last error, AsyncSink drop accounting) |
| snaplink — governance connector | — | outbox + relay to sink | — | **Missing** in `cmd/sso-server`; `infrastructure/auditgovernance` is used only by `cmd/snaplink-audit-provisioner` and `cmd/snaplink-billing` | **No** — not imported by `cmd/sso-server` (grep of `cmd/` shows only billing/provisioner) | n/a (draft §snaplink is entirely Proposed) |
| **snaplink-console** | browser `localStorage` (`lib/services/audit_log_service.dart`) | admin-op audit display | **Hot/volatile** — 1000-entry ring, trivially clearable | Flutter; **records local admin ops** (method/path/status), does **not** query any audit API | n/a (client) | None — draft's "consumer only (Verified: reads audit data)" is **inaccurate** (Verified: file reads only `_storageKey='sso_audit_log'`) |
| **snaplink-audit-governance (sink)** `~/workspace/demo/snaplink-audit-governance` | file snapshot (stock verify stack) \| Postgres **single row** `audit_state_snapshot`; ClickHouse projector; S3/file archive; own Postgres `audit_outbox` | tenant-scoped hash-chained ledger, receipts, segments/checkpoints, legal holds, exports, restore | File: atomic tmp+rename, **no fsync** (`internal/store/store.go` L240–258); Postgres: durable row, optimistic version | **Verified** — `cmd/audit-api/main.go` `openStore(statePath, postgresDSN)`; `deploy/docker-compose.verify.yml` uses `AUDIT_STATE_PATH` (file) + S3 archive + Kafka + ClickHouse | Per-tenant append serialization; **entire ledger is one JSON blob** — single effective writer (see F-01) |
| sink — relational ledger schema | Postgres tables `ledger_events`, `event_receipts`, `ledger_streams`, `ledger_segments`, `signed_checkpoints`, `legal_holds`, `export_jobs`, `retention_policies` (+RLS policies) | intended durable ledger | — | **Unused by Go runtime** (Verified: only `audit_state_snapshot` and `audit_outbox` are touched; `grep -rn "ledger_events|event_receipts|..." --include="*.go"` hits nothing outside migrations) | **No** | schema-only today |
| sink — query self-audit | — | `audit.event.read`/`audit.event.export` on query API | — | **Missing** — `QueryEvents` (`internal/service/service.go` L576) records no AdminAction; self-audit exists only for admin *mutations* (`adminAction` at L166, used for tenant/schema/policy/export/hold/restore/checkpoint) | **No** | draft's console §2 `[PROPOSED]` claim "ledger emits read self-audit" — Missing |
| **aero-id** | Postgres (gorm `gorm.io/driver/postgres`) | accounts, messages, files, comments, notifications; `audit_ledger` (append-only, signed, hash-chained, per-partition), `audit_publications` (outbox), `audit_events` (legacy), legal holds, event store | **Durable** (Postgres); ledger protected by append-only trigger (`migrations/000034`), tenant-scoped uniques (`000050`), signing key default is a placeholder (`configs/config.yaml` `signing_key: change-me-please-very-long-audit-key`) | **Verified** — ledger + publication drain loop always wired (`internal/app/new.go` L134–179, `callbackSvc.Start`); **governance publisher gated**: `buildAuditPublishers` returns nil unless `cfg.AuditGovernance.Enabled` (`internal/app/audit.go` L33), stock `configs/config.yaml` `audit_governance.enabled: false` ("disabled until all source bindings exist") | **Partially** — local durable ledger yes; **relay to sink inert in stock config** | Chain append serialized per partition via `pg_advisory_xact_lock` (`internal/repository/audit_ledger.go` `appendInTx`); dedup `(tenant_id, source, event_id)` |
| **aero-vault** | SQLite (`modernc.org/sqlite`, WAL, FK ON, **MaxOpenConns(1)** — `internal/repository/sqlite.go` L26–33) \| Postgres dialect paths | objects, object_events, audit_log, `audit_governance_outbox` (in-tx), bindings/control, legal holds | **Durable** (WAL; default `synchronous=FULL`; no explicit `synchronous`/`busy_timeout` pragmas set) | **Verified** — relay fully implemented (`internal/auditgovernance/{runtime,relay,http,token}.go`, 2.8K LOC) and wired in `cmd/server/main.go` (both server + worker paths, `WrapRepository`) **only when `AUDIT_GOVERNANCE_ENABLED=true`; default false** (`internal/config/config_audit_governance.go` L55); stock `docker-compose.yml` does not set it | **Partially** — outbox capture + relay inert in stock deploy; delete-flow tx (04-composition) implemented (`event_outbox.go` `HardDeleteObjectWithEvent`: legal-hold check + delete + audit_log + both facts in one tx) | Outbox `UNIQUE(origin_kind, origin_id)`; capture only when binding state = active (`governanceCaptureActive`, FOR KEY SHARE in tx); safe-disable guard (`AuditGovernanceCanDisable`) |
| **aero-im** | Postgres (sqlx + pgvector; Redis/NATS hot caches) | messages, rooms, live, notifications; local `audit_events` (workspace-scoped, append-only, ULID keyset) | **Durable** (Postgres) | **Verified** — `migrations/0007_audit.sql`, `crates/aero-storage/src/audit.rs` (keyset pagination, retention sweep with legal-hold exemption, partition sweep); stock `config.toml` `[database] url = postgres://…` | local audit **yes**; governance connector **Missing** (no file references audit-governance; "governance" hits are content moderation, not audit) | Workspace-scoped isolation at query layer; no cross-service contract |
| **snaplink-sso** (legacy, `github.com/snaplink/sso`, `/home/u1/snaplink-sso`) | in-memory ring buffers; `audit-service` ring cap 50k (`audit-service/main.go`) | standalone SSO + audit service | **Hot** | Go stdlib | `cmd/ssoctl` only; gateways are demo binaries | Not part of the 6-repo platform — legacy/Info |

### Hot vs durable summary
- **Hot (volatile)**: snaplink identity + audit (stock default memory), console localStorage, snaplink-sso audit ring, snaplink webhook fan-out (bounded-retry then drop).
- **Durable**: aero-id (Postgres ledger), aero-vault (SQLite WAL), aero-im (Postgres), sink (Postgres single-row; file backend is atomic-rename but no fsync).
- **Stock binaries that actually publish to the sink today**: **none** — aero-id and aero-vault relay code is fully built but flag-off by default; snaplink and aero-im have no relay at all; the sink itself is deployable (Dockerfile + verify compose).

---

## 2. Findings (sorted by severity)

### F-01 — High · Sink stores the whole ledger in one JSON snapshot; relational schema unused
- **Evidence**: `snaplink-audit-governance/internal/store/store.go` (`Snapshot` = maps of *all* events/receipts/streams/segments/legal holds/exports; `fileBackend` tmp+rename) and `internal/store/postgres.go` (single row `audit_state_snapshot`, optimistic `version` check, **no retry** — `Store.Update` L167–181 loads, mutates, saves; `ErrSnapshotConflict` propagates to the caller). `migrations/001_control_plane.sql` defines `ledger_events`/`event_receipts`/`ledger_streams`/`ledger_segments`/`retention_policies` (+RLS) but **no Go code reads or writes them** (grep Verified). `QueryEvents` (service.go L576) scans `data.Events` in full per query.
- **Impact**: every ingest is a full-ledger serialization (load JSON → mutate → save JSON). Single effective writer: with N API replicas sharing one Postgres row, N−1 concurrent ingests fail with `ErrSnapshotConflict` (producers' outbox retries absorb it — at-least-once holds — but steady-state throughput ≈ 1 full-snapshot RMW per event). Query is O(ledger). Snapshot grows unboundedly (no pruning; `retention_policies` unused). This is the draft's central "sink is P0" assumption; the capacity ceiling is far below the draft's `auth.token.issue ‡`/`message.send ‡` ambitions.
- **Recommendation**: before P2/P3 cutover of any high-volume class, either (a) implement the relational ledger backend on the migration-001 schema (per-tenant `ledger_events` insert + receipt + segment sealing in a real tx, `FOR UPDATE` on `ledger_streams` row per stream), or (b) adopt the aero-id pattern (per-partition advisory lock + row-per-event) into the sink. Keep the snapshot only for control-plane state (tenants/sources/schemas/policies) — which is what `audit_state_snapshot` is named for.
- **Validation**: `AUDIT_POSTGRES_DSN` + `pgbench`-style concurrent ingest (e.g., 50 producers × 100 events/s) against the current build; measure conflict rate and per-event latency vs. target. Query `EXPLAIN` on the projector (ClickHouse) vs snapshot scan.

### F-02 — High · Draft's "snaplink: local audit-log concept only" understates it: the stock binary's audit is *volatile by default*, and no relay exists in the stock binary
- **Evidence**: `cmd/sso-server/config.yaml` — `audit.backend: memory` (10k ring), `hash_chain: false`; `identity.backend: memory`; no `auditgovernance` import anywhere in `cmd/sso-server` (Verified). Webhook sink is bounded-retry-then-drop (`auditsink/retrying_sink.go`).
- **Impact**: the draft's P3 "local audit log remains as L0 complement" implies an L0 that survives restarts; today a stock deployment loses the entire local audit trail on restart and has **no** path to the sink. The draft's snaplink section ("Relay: Go worker mirroring the aero-id publisher pattern") has no implementation to mirror onto in this repo.
- **Recommendation**: treat snaplink P1 as *two* deliverables: (1) flip stock config guidance to `audit.backend: sqlite`/`postgres` + `hash_chain: true` (durable L0), (2) build the outbox+relay as a new `cmd` wiring (the SDK seams exist: `audit.Sink` interface, `BatchSink`, AsyncSink). Do not cut over until L0 durability is decided, because P4 backfill then has a source of truth.
- **Validation**: restart an sso-server with stock config after 10 logins; count `audit_events` rows (expect 0) vs. sqlite backend (expect 10).

### F-03 — Medium/High · "Connector exists (Verified)" is true, but neither producer's stock binary publishes — rollout plans must include the flag flip as a migration step
- **Evidence**: aero-id `configs/config.yaml` L174 `audit_governance: enabled: false` (comment: "disabled until all source bindings exist"); `buildAuditPublishers` nil-gated (`internal/app/audit.go` L33–36); vault `getEnvBool("AUDIT_GOVERNANCE_ENABLED", false)` (`internal/config/config_audit_governance.go` L55) and stock compose unset.
- **Impact**: the draft's "fastest rollout (pattern already proven)" claim is code-true but deployment-false: enabling requires per-tenant OAuth bindings *before* boot (aero-id `validateAuditBindingCoverage` fails boot if durable tenants lack bindings — a deliberate fail-closed). This is actually a strength; the draft should state it as the activation gate rather than implying the connector is live.
- **Recommendation**: add an explicit "binding provisioning" step (the `snaplink-audit-provisioner` binary exists for this — `cmd/snaplink-audit-provisioner/run.go` builds `auditgovernance.Provisioner` with `NewPlatformTokenSource`/`NewControlClient`) to P1 of every producer; document the boot-failure behavior.
- **Validation**: `AUDIT_GOVERNANCE_ENABLED=true` with empty bindings file → expect boot error (Verified code path `buildAuditGovernanceRuntime` requires `BindingsFile` when enabled, config L88).

### F-04 — Medium · Reference-pattern backoff/DLQ semantics diverge from draft §1.2/§1.4
- **Evidence** (all Verified):
  - aero-id: batch 50 (draft says 100), lease 5 min, `retryDelay` = 1s base doubling, **cap 1h** (`internal/service/callback.go` L358–370), dead-letter only after `maxAttempts=8` — **no 422/permanent classification** (`publicationOutcome`, `callback_audit.go` L84–97: every error retries; only attempt exhaustion → `dead_letter`); publisher treats any non-202 as generic error (`publisher.go` `validateReceipt`).
  - aero-vault: `ClaimAuditGovernance` batch default 32, lease `claimTTL`; `retryFact` → `boundedBackoff` single class, **no dead state at all** (outbox has no status column; `delivered_at_ns=0` rows retry forever); `classifyRelayError` is logging-only (`relay.go` L148).
  - Governance sink's own relay *does* implement permanent-error dead-lettering (`internal/outbox/relay.go` — 400/403/409 → `StatusFailed` immediately).
- **Impact**: draft §1.4's "permanent failures → dead" and §1.2's "rate-class 30–60s scatter, cap 300s" are **not the behavior of the anchor pattern** the draft cites. A schema-rejecting producer event (422) will be retried up to 8× over ~8 minutes in aero-id and **forever** in aero-vault — with no alertable DLQ state in vault (poison events won't block batches — claim skips are fine — but they are invisible and unbounded in `last_error` churn).
- **Recommendation**: amend the contract to match reality or harden the code: minimal change is (a) classify non-2xx ≥400 with a schema/validation status in both relays → dead/immediate-DLQ, (b) add a `status`/`dead_at` column + partial index to the vault outbox (migration 0039 extension), (c) document the actual backoff constants in §1.2 instead of proposing new ones.
- **Validation**: unit-test a 422 response through aero-id `finishPublicationAttempt` (expect dead on first attempt after fix; today expect `pending`+retry) and through vault `deliverFact` (today: `retryFact` logs "delivery deferred").

### F-05 — Medium · aero-vault SQLite single-connection serialization + relay-in-process contention
- **Evidence**: `internal/repository/sqlite.go` L26 `db.SetMaxOpenConns(1)` "serialize writes to avoid SQLITE_BUSY"; relay runs in the same process and claims/publishes on the same pool (`cmd/server/main.go` L81/210); `deliverFact` publishes synchronously per fact (HTTP timeout `AUDIT_GOVERNANCE_HTTP_TIMEOUT_SECONDS`), while claimed rows are leased.
- **Impact**: a slow sink (5s timeouts × 32 concurrent deliver goroutines) can hold the single SQLite connection for long stretches (claim + retry/complete are quick, but every business write queues behind them). Download/notification-heavy tenants will serialize behind the relay. `synchronous` pragma is left at default FULL (durable — good); no `busy_timeout` set.
- **Recommendation**: (a) set `PRAGMA busy_timeout` (belt-and-braces for the Postgres-dialect path is irrelevant; for SQLite consider a second connection pool for the relay with `SetMaxOpenConns(1)` still per-pool), or better (b) run the relay as a separate process/worker with its own DB file handle (the code already supports `-worker` mode: `cmd/server` has a worker path that wires the same runtime). Measure write latency under relay load (F-05 validation: `AUDIT_GOVERNANCE_ENABLED=true` + concurrent 100 writes/s, compare p99 vs disabled).
- **Validation**: `sqlite3` `PRAGMA journal_mode; PRAGMA synchronous;` on the runtime DB (expect `wal`, `full`).

### F-06 — Medium · Console "consumer only" claim is wrong; read self-audit does not exist
- **Evidence**: `lib/services/audit_log_service.dart` — `_storageKey='sso_audit_log'`, ring cap 1000, `record()` on admin mutations, no HTTP; `audit_log_tab.dart` reads only `_logService.entries`. Sink `QueryEvents` records no AdminAction. (Both Verified.)
- **Impact**: the draft's console section rests on two false/absent premises: console is not a consumer of the ledger today (it shows a local, clearable log), and the "ledger emits `audit.event.read` self-audit rows on the query API" has no implementation. The "cannot be bypassed by direct API calls" guarantee is therefore **unimplemented**.
- **Recommendation**: keep the console design (edge facts + sink-side read self-audit) but mark the read self-audit as a sink P0 deliverable (it is the one piece that closes the "who queried the ledger" question), and re-label the console section as "no connector; local-only today (Verified)".

### F-07 — Low/Medium · Idempotency-key construction differs from the draft's SHA-256 contract (acceptable, but the contract text should match)
- **Evidence**: aero-id uses `fact.ID` (UUID, minted once per business op; retries reuse the `audit_publications` row — deterministic per op) and `idempotency_key = fact.ID` (`publisher.go` `governanceEvent`); vault uses `UNIQUE(origin_kind, origin_id)` with `event_id = fact.ID` UUID (retries reuse the row); neither implements `SHA-256(service|event_type|tenant_id|trigger_key)[:32]`. Sink dedups on `(tenant_id, event_id)` + tenant-scoped `idempotency_key` with content-conflict detection (`service.go` Ingest L389–450).
- **Impact**: the draft's §1.3 contract is un-implementable as written for the existing code; the *property* (stable key per occurrence, reused on retry, sink dedup) holds in both. `file_id|request_id` distinct-download semantics: vault's `object_events` rows are per-occurrence and each maps to its own outbox row (`UNIQUE(origin_kind, origin_id)` per row) — Verified OK.
- **Recommendation**: rewrite §1.3 as "deterministic per-occurrence key assigned at first write, reused on retry; sink dedups on (source, event_id) and rejects idempotency-key reuse" — no code change needed.

### F-08 — Low · aero-id ledger signing key placeholder in stock config
- **Evidence**: `configs/config.yaml` `audit_ledger.signing_key: change-me-please-very-long-audit-key` (comment says override via `AERO_ID_AUDIT_LEDGER_SIGNING_KEY`).
- **Impact**: chain signatures are not meaningful until operators override; the draft's "不可抵赖" (non-repudiation) property is config-dependent.
- **Recommendation**: add a boot-time check that the placeholder is rejected in production profile (like the governance sink's `AUDIT_SIGNING_SECRET` handling).

### F-09 — Info · Snaplink product: `audit_events` Postgres/SQLite parity is maintained (rebind `?`→`$n`, migrations v1/v2) — good migration hygiene to copy
- **Evidence**: `infrastructure/postgres/audit_sink.go` (`rebind`, `auditMigrations`), `platform/audit/sqlite` (migrations, WAL DSN docs L127).

### F-10 — Info · Legacy `snaplink-sso` audit-service is a volatile in-memory ring (cap 50k) — not part of the 6-repo platform; do not treat as a sink candidate
- **Evidence**: `audit-service/main.go` `newAuditStore(50000)`, HTTP-only.

---

## 3. Query/index and transaction analysis (demonstrated hot/atomic paths)

### 3.1 aero-id — ledger append + publication drain (the draft's reference pattern)
- **Write tx** (`internal/repository/audit_ledger.go` `AppendInTx`): business tx → `checkAuditDuplicate` → `SELECT pg_advisory_xact_lock(hashtext(partition_key))` → read chain head → insert ledger row + `audit_publications` row (same tx, Verified L190–200). Advisory lock serializes per-partition chain appends; dedup checked inside the lock (double-check). Microsecond truncation before hashing so chain verification round-trips (P4-03). Append-only trigger + `UNIQUE(tenant_id, source, event_id)` (000034/000040/000050).
- **Drain** (`callback_audit.go` `drainPublications`): `ClaimPublication` — `FOR UPDATE SKIP LOCKED`, `status IN (pending, delivering) AND next_attempt_at<=now OR lease_until<now`, order `next_attempt_at, id`, batch 50, lease 5 min; per-fact publish goroutines under a bulkhead (concurrency 4, `worker_concurrency`); `FinishPublication` after outcome. Partial index `idx_audit_publications_due (status, next_attempt_at)` serves the claim. Sound lease/recovery semantics; crash-between-send-and-mark absorbed by sink dedup (draft §1.5 row 9 — Verified pattern).
- **Hot-path cost**: one synchronous HTTP POST per fact to the sink (`wait_for=ledgered`), receipt validated (202 + JSON receipt, event_id/tenant match, status ∈ ledgered/indexed/archived). If the sink is slow, the worker's 4-way bulkhead bounds impact; rows stay leased and retry.
- **Query paths**: `idx_audit_ledger_query (account_id, occurred_at DESC, id DESC)`, actor, operation, partition indexes — tenant-scoped ledger queries are index-served.

### 3.2 aero-vault — delete-flow tx + relay claim
- **Atomic delete** (`internal/repository/event_outbox.go` `HardDeleteObjectWithEvent`): legal-hold EXISTS check → access-state cleanup → `DELETE FROM objects` → `audit_log` row → 2 outbox facts (`vault.file.deleted@1.1`, `vault.file.notify@1.1`, payload ≤1 MiB validated, schema_version 1.1) — one tx; zero-row delete rolls back (no phantom facts, GAP-4). Matches 04-composition C13 (vault-side tombstone; no governance owner query).
- **Relay claim** (`audit_governance_claim.go`): Postgres `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE OF o SKIP LOCKED)` joining `audit_governance_bindings` on `revision` (a config change invalidates outstanding leases — clean); SQLite variant: tx + select + per-id conditional UPDATE (single conn makes it safe). Index `(delivered_at_ns, available_at_ns, lease_expires_at_ns, created_at_ns)` serves the claim predicate.
- **Gap reconciliation** (`runtime.go` `reconcile`): scans `object_events LEFT JOIN outbox` for rows captured before a binding went active (`audit_governance_write.go` L248) — closes the enable-after-capture hole. Good design, worth copying.
- **Hot-path cost**: every business write that touches the single SQLite connection serializes with the relay (F-05). Downloads: each `object_events` insert (per request_id) → distinct outbox row → relay POST. **Volume unknown** — `file.download ‡` L1 decision (registry open question #3) has no data yet.

### 3.3 Sink — ingest hot path (the bottleneck)
- `postEvent` (httpapi) → JWT scope `audit:event:write` → `Ingest` (service): validate → tenant resolution via source `allowed_client_ids` (migration 002, `resolveIngestTenant`) → quota check → dedup by `event_id` (digest compare; same-key-different-content → conflict error) → idempotency-key uniqueness → per-stream sequence + `prev_hash`/`event_hash` → receipt (status=ledgered) → segment seal at `SegmentSize` → archive (O_EXCL idempotent) → receipt status archived/indexed.
- **All inside one `Store.Update` = full-snapshot RMW** (F-01). There is no per-tenant index, no partial write, no retry on `ErrSnapshotConflict`. `QueryEvents` = full scan + in-memory filter + sort. The ClickHouse projector (`internal/projection/projection.go`: `ReplacingMergeTree`, `PARTITION BY toYYYYMM`, `ORDER BY (tenant_id, occurred_at, event_id)`) is the only scalable query path and is fed by Kafka, not by the API.
- **Consistency**: strong within one snapshot update (dedup + chain + receipt atomic); across replicas, optimistic-lock conflict = request failure (producer retries — at-least-once preserved).

### 3.4 aero-im / snaplink hot paths
- aero-im `audit_events`: keyset pagination on ULID `id DESC` (index `(workspace_id, id DESC)` — serves tenant filter + order), retention sweep respects legal holds, monthly partition sweep (migration 0154 refs). Postgres-backed, no outbox — every `member.add`-class op is a direct insert in the op's tx (no relay). Verified local only.
- snaplink: recorder → MemorySink O(1); sqlite sink single insert (+hash-chain head read); webhook fan-out bounded-retry-drop. No outbox; the `auth.token.issue ‡` L1 aggregation has no implementation (Proposed).

---

## 4. Safe migration sequence (draft P0–P5, adjusted to verified reality)

Compatibility window principle: every step is flag-gated (`enabled: false` defaults) and **additive**; nothing existing changes until cutover.

| Phase | Action | Compatibility window | Validation queries | Rollback / roll-forward |
|---|---|---|---|---|
| **P0a — sink hardening (new, required by F-01/F-06)** | Relational ledger backend on migration-001 tables (or explicit capacity decision + measurement); read self-audit (`audit.event.read`) on `QueryEvents`/export | API stays snapshot-backed behind a `LEDGER_BACKEND=snapshot\|relational` flag; receipts identical | Concurrent-ingest benchmark (F-01); `EXPLAIN` on `ledger_events (tenant_id, occurred_at)`; assert `audit.event.read` rows appear for a query | Relational backend is new code — snapshot backend remains default until gate; roll-forward = flip flag per replica; rollback = flag off (no data loss; relational rows remain for later backfill) |
| **P0b — sink ops** | Archive destination (S3) readiness gate (`readyz` already probes it), signing/encryption secrets out of stock config | — | `readyz` shows `archive_unavailable` when S3 down (Verified behavior) | n/a |
| **P1 — producer flags + bindings** | aero-id: fill `audit_governance.bindings` (per-tenant client_credentials), set `enabled: true`; vault: `AUDIT_GOVERNANCE_ENABLED=true` + `AUDIT_GOVERNANCE_BINDINGS_FILE`; snaplink/aero-im: **build relay first** (F-02; aero-im scope grant conditional on relay — draft Flow 3) | Boot fail-closed if bindings missing/incomplete (aero-id coverage check; vault requires bindings file) — this *is* the safety gate | `SELECT tenant_id FROM accounts … UNION SELECT ledger.tenant_id FROM audit_publications …` (aero-id coverage — mirrors `validateAuditBindingCoverage`); vault `SELECT state, revision FROM audit_governance_bindings` (expect active) | Flip flag off; vault `AuditGovernanceCanDisable` blocks disable until outbox drained + bindings removed (Verified — rollback is *safe by construction*); aero-id: flag off = publications complete locally, no loss |
| **P2 — dual write** | Vault/id: capture already in-tx — verify outbox rows appear for the chosen non-sensitive classes; snaplink/aero-im: add outbox DDL + in-tx write behind feature flag | New tables only; no consumer changes | `SELECT count(*) FROM audit_governance_outbox WHERE delivered_at_ns=0` grows; per-class: `SELECT action, count(*) FROM audit_log GROUP BY action` vs outbox rows (parity check); vault gap scan: `ListAuditGovernanceGaps` returns 0 | DDL is additive (Vault 0039/0040, id 000034/000040/000050/000057 already applied; snaplink/aero-im new). Drop columns/tables only after P3 gate |
| **P3 — cutover (relay L2)** | Enable relay for full classes; keep local L0 append | Producers send to sink with existing receipts; sink dedup absorbs any double-send from P2 | Ingest parity: `event_id` dedup counters (`audit_ingest_duplicates_total`) stable; per-service lag: outbox `next_attempt_at` vs now; DLQ alerts (sink relay `StatusFailed`) | Disable relay flag; outbox rows persist (never dropped) — roll-forward/backward both safe |
| **P4 — backfill** | Draft open decision #1 — scope per service; **default: none** (vault tombstone C13; snaplink skip unless fields exist) | Idempotent by construction (sink dedup); backfill must reuse the same event_id derivation as live path or it will conflict | `audit_ingest_conflicts_total == 0` during backfill; integrity: `VerifyIntegrity` per tenant after backfill (sink has `VerifyIntegrity` API — Verified) | Backfill is additive; abort = stop job (partial rows dedup-harmless) |
| **P5 — gate** | Registry gate: new write/admin op without event type fails review; DLQ/alert monitoring live | — | `signed_checkpoints` sealed per segment; `integrity_verify` valid; alert thresholds (draft §1.4) | n/a |

**Data-integrity checks to run at every phase** (all Verified as existing capabilities):
- Sink: `VerifyIntegrity(tenant, stream)` API + `signed_checkpoints`/`merkle_root` (`internal/service/governance.go` L149); segment seal at `SegmentSize`; `audit_state_snapshot.version` monotonic.
- aero-id: `audit_ledger` append-only trigger (attempt an UPDATE → expect exception); `prevent_audit_ledger_mutation` (000034); chain verify (P4-03 test suite).
- vault: outbox fact payload validation inside delete tx (invalid payload rolls back the delete); `UNIQUE(origin_kind, origin_id)` prevents double-fact.
- aero-im: legal-hold-exempt retention sweep (`NOT EXISTS legal_holds`).

---

## 5. Unknown volume, retention, and recovery assumptions

1. **Volume per event class (Unknown, no workload evidence anywhere)**: counts for `auth.token.issue` ‡, `message.send` ‡, `notification.send` ‡, `file.download` ‡, `file.rag.query` ‡ — the L1 aggregation thresholds (draft open decision #2) and the sink's snapshot-store ceiling (F-01) cannot be sized without these. **Required measurement**: per-service daily event counts + peak-per-second from existing logs/metrics before P3 of any high-volume class.
2. **Sink ledger growth (Unknown)**: current snapshot size / row count in any deployed instance; the O(N) ingest cost curve is unmeasured.
3. **Retention (Unknown)**: `retention_policies` (hot 30 / warm 180 / archive 365 defaults in migration 001) are schema-only — no enforcement in the runtime; snaplink retention default `max_age: 720h` (30d, disabled); aero-im `AERO__SERVER__AUDIT_RETENTION_DAYS` env default; aero-id audit TTL cleaner exists (`new.go` L332, `audit_events` legacy table) with no configured default documented. No compliance input exists in-tree (draft open decision #1 defers to compliance).
4. **Backup/restore (Partial/Unknown)**:
   - Sink: restore workflow exists (approval-gated `restore_runs` — Verified) and S3 WORM archive; **file-backend backups and the Postgres snapshot row backup schedule are undocumented (Unknown)**; file backend has no fsync (F-01).
   - snaplink: snapshot + DR subsystem exists (`snapshot.enabled: false` default; encryption none/passphrase/aes-gcm; `restore_from`) — capability Verified, operator schedule Unknown.
   - aero-id: replication cursors (000055), region directory (000054), legacy cutover registry (000056); **ledger-specific backup/restore procedure Unknown**.
   - aero-vault: **no backup mechanism found** (SQLite file on a compose volume); restore path Unknown.
   - aero-im: retention sweeps only; backup Unknown.
5. **Tenant counts / binding matrix (Unknown)**: number of tenants × OAuth bindings the `validateAuditBindingCoverage` startup query must cover; sink `tenants` table is control-plane (seeded via API — `AUDIT_BOOTSTRAP_TENANT` in verify compose).
6. **Multi-replica topology (Unknown)**: sink replicas sharing the Postgres snapshot row (conflict rate); vault relay in-worker vs in-server process (F-05); snaplink `topology.mode: multi` + sqlite audit DSN sharing.

---

## Bottom line

- **Draft claims that Verified**: connector existence (aero-id), vault delete-flow tx integration (04-composition), snaplink/aero-im gaps, outbox-in-business-tx pattern, sink dedup/idempotency/tenant-consistency design.
- **Draft claims that need correction**: console "consumer only" (F-06), §1.2/§1.4 backoff/DLQ as description of the anchor pattern (F-04), §1.3 SHA-256 idempotency key (F-07), and the implicit assumption that the sink is ready for P2/P3 volume (F-01).
- **Before any producer cutover**: P0 must include the sink's relational-ledger backend (or an explicit, measured capacity decision), the read self-audit, and the flag/binding activation gate (F-03). No producer relay should be declared "proven" until a 422-classified dead-letter path exists in both aero relays (F-04).
