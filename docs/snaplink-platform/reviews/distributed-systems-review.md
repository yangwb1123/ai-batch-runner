# Distributed-Systems Review — Per-Service Audit Ingestion Design

> **Scope**: `docs/snaplink-platform/audit-ingestion-design-draft.md` (draft) vs the
> current code of the 6 repos, reviewed with a distributed-systems lens: consistency,
> ordering, atomicity, idempotency, ownership, conflict resolution, failover, clock,
> retry/backoff, and the documented fail-open/fail-closed boundary.
> **Status**: advisory. All claims labeled **Verified** (read from code this review),
> **Partial**, **Missing**, **Proposed** (draft-only), or **Unknown** (no evidence found).
> **What actually ran**: static reads + targeted greps of each repo at the current
> working tree; no test suites or live integration were executed. Code-level citations
> use repo-relative paths; each repo was reviewed at its local checkout
> (`/home/u1/{aero-id,aero-vault,aero-im,snaplink,snaplink-sso}` and
> `/home/u1/workspace/demo/snaplink-audit-governance`).

## 1. State map

| Component | Owner | Store / table | Durability | Consistency | Replication | Failover |
|---|---|---|---|---|---|---|
| **Sink ledger** (snaplink-audit-governance) | sink service (single writer) | whole-snapshot: file (atomic rename) or Postgres single row `audit_state_snapshot` (jsonb + optimistic `version`) — `internal/store/store.go` (Verified) | ledgered = committed snapshot write (rename / version-checked UPDATE); crash-safe, no WAL | serialized per process (`Store.mu`); cross-process optimistic version → `ErrSnapshotConflict` (Verified) | **none** — one active writer; replicas share the row, losers conflict | standby replica loads row and resumes; lost updates rejected (Verified) |
| Sink ledger data (events) | sink | `ledger_events` PK `(tenant_id, event_id)`, `UNIQUE (tenant_id, idempotency_key)`, `UNIQUE (tenant_id, stream_id, sequence)` — `migrations/001_control_plane.sql` (Verified) | same as snapshot | per-stream hash chain, segments, signed checkpoints (Verified) | none | via snapshot row |
| Sink ingest receipts | sink | `event_receipts` (Verified) | same | dedup: duplicate content → `Duplicate` receipt + 2xx; different content → `Conflict` 409; idempotency_key reuse → 409 (Verified) | none | — |
| **aero-id local ledger** | aero-id | `audit_ledger` hash chain, `UNIQUE (tenant_id, source, event_id)` (migration 000050 Verified) | local DB tx | append serialized per (source, partition) via `pg_advisory_xact_lock` + in-tx dup check (Verified) | DB-level only | local DB |
| **aero-id outbox** | aero-id | `audit_publications` (`uq_audit_publication_fact UNIQUE (fact_id)`) — migration 000045 (Verified) | **same tx as ledger append** in repo paths (`AppendInTx`: `account_profile.go`, `operation.go`, `sync_job.go`, `event_receipt.go`, `operation_audit.go`); **post-commit, own tx, error discarded** in service paths (`service/account.go:155,184`, `callback.go` Register/Delete/Replay, `audit_hold.go:59`, `legacy_cutover.go:88`) (Verified) | lease claim `FOR UPDATE SKIP LOCKED`, lease 5 min, batch 50, `lease_owner`-guarded finish (Verified) | multi-worker safe (SKIP LOCKED + lease) | lease expiry → redelivery |
| **aero-id relay** | callback worker (`drainPublications`, bulkhead 4, 1s tick) | in-process | — | MaxAttempts 8 → `dead_letter`; backoff 1s→1h cap ±25% jitter (Verified) | N workers claim | lease expiry; **no automatic dead-row replay (Missing)** |
| **aero-vault outbox** | vault | `audit_governance_outbox` PK `id` (random UUID fact ID), `UNIQUE (origin_kind, origin_id)` (migration 0039 Verified) | **same tx** as `audit_log`/`object_events` insert when binding active (`RecordAuditWithGovernance`/`InsertEventWithGovernance`, `auditedRepository` wrap) (Verified) | claim `UPDATE … FOR UPDATE SKIP LOCKED`, revision-filtered by binding, claim TTL 30s, batch 32 (Verified) | multi-worker safe; SQLite path serialized | lease/claim_token guarded |
| **aero-vault relay** | `Runtime` (reconcile → deliver → cleanup loop) | in-process | — | **no dead state**: all failures → `RetryAuditGovernance`, backoff 1s→300s cap ±25% jitter hashed by fact ID (Verified) | N workers | lease expiry; gap reconcile rescan (Verified) |
| **aero-vault readiness** | `Runtime.Ready` (health) | — | — | fails if binding draining or oldest pending > 900s maxLag (Verified) | — | — |
| OAuth token sources | per producer | in-memory cache, refresh skew 30s, TTL cap 24h (aero-id singleflight; vault serialized) (Verified) | volatile | vault invalidates on 401; **aero-id does not** (Verified) | — | refetch on expiry |

**Verified anchor deltas vs draft §0**:
- Draft calls aero-id connector and vault `token.go` the "reference pattern" — the pattern is
  real (both exist and are wired) but it is a **synchronous `wait_for=ledgered` publisher +
  leased outbox**, not the draft's §1.2 shape (batch 100, cap 300s, 422→dead). Batch/lease
  constants differ everywhere (see F12).
- Draft §1.2 / sink-contract say scope `audit:event:ingest`; **all code uses
  `audit:event:write`** (sink `server.go` `require(r, "audit:event:write")`; aero-id
  `oauth.go` `auditScope`; vault `model.go` `RequiredScope`) (Verified) — see F2.
- Draft §1.3 deterministic `event_id = SHA-256(…)` is **not implemented anywhere**;
  aero-id uses UUID event IDs (business-key-derived at some call sites, random at others),
  vault uses random UUID fact IDs (Verified) — see F4.
- Draft §1.4 "422 → dead" is implemented **only partially in aero-id** (all failures
  dead-letter after 8 attempts) and **not at all in vault** (retry forever) (Verified) — see F3.
- Draft's "outbox inside vault's delete transaction" (04-composition C13/C14): **Verified**
  — `auditedRepository.RecordAudit/InsertEvent` write audit + outbox rows in the same tx.

## 2. Findings (by severity)

### F1 — High (availability/capacity): the sink is a whole-snapshot single-writer store; ingest cost is O(ledger size), and the design's "never drop" makes producer backlogs the pressure valve
- **Evidence** (Verified): `internal/store/store.go` — file backend rewrites the full JSON
  snapshot per `Save` (tmp + rename); Postgres backend keeps the **entire ledger in one
  jsonb row** with a version counter; `Store.Update` = clone whole snapshot → mutate → save.
  Every ingest, query, and integrity check operates on that snapshot.
- **Triggering failure**: sustained ingest rate exceeds snapshot-rewrite capacity (rewrite
  cost grows with every event, schema, segment, and receipt retained). Producers never drop
  (vault retry forever; aero-id up to 8 attempts), so backlogs grow; vault `Ready()` then
  fails on maxLag > 900s (Verified `runtime.go`), red-flagging health.
- **User impact**: audit lag and ledger gaps at scale; vault health flapping; DLQ alerts;
  eventually dead_letter (aero-id) for events during the outage.
- **Recovery**: reduce rate (L1 aggregation is a survival requirement, not an optimization);
  purge/retention of old events (retention job exists); single active writer.
- **Corrective pattern**: the ledger must be an **append-only partitioned store** (per
  tenant/stream shards, WAL), with the snapshot demoted to a projection. Before that: put a
  hard ingest-rate budget (per tenant quota exists — `checkTenantAndQuota` 429, Verified) and
  size the snapshot growth; document the capacity envelope in the design.

### F2 — High (contract): the draft's scope name `audit:event:ingest` contradicts every implementation's `audit:event:write`
- **Evidence** (Verified): draft §1.2 and the sink-contract section both name
  `audit:event:ingest`; sink requires `audit:event:write` (`server.go` `postEvent`),
  aero-id `oauth.go` `validOAuthBinding` **rejects** any scope other than
  `audit:event:write`, vault `model.go` `RequiredScope = "audit:event:write"`. The scope
  matrix draft (`scope-matrix-draft.md` Flow 3) is the odd one out.
- **Triggering failure**: operators provision tokens per the scope matrix → 401/403 at the
  sink → aero-id retries 8× then dead_letter; vault retries forever and its `Ready()` gate
  fails.
- **User impact**: total audit pipeline outage at rollout, misdiagnosed as credentials.
- **Recovery**: re-provision scope; manual dead-row fix (tool Missing, see F9).
- **Corrective pattern**: freeze the contract from the sink's `require()` (the executable
  truth), fix the drafts; add a startup validation that asserts the publisher's configured
  scope against the sink's required scope (aero-id already validates scope shape at config
  time — extend to the sink side).

### F3 — High (availability): vault outbox has no dead-letter state; any permanent failure (422/409/poison) retries forever and permanently red-flags readiness
- **Evidence** (Verified): `audit_governance_outbox` has no status column — only
  `delivered_at_ns` (migration 0039); `relay.go` `retryFact` is called for **every** error
  class (it classifies only for logging); `Runtime.Ready` fails when any undelivered row is
  older than maxLag (900s) and `OldestPendingAuditGovernance` counts dead-to-be rows.
- **Triggering failure**: one schema-invalid or content-conflicting event (e.g., sink 409
  from an idempotency_key collision) → the row is retried with 300s cap forever → health
  endpoint permanently failing → orchestrator restart loops (restart does not clear the row).
- **User impact**: vault marked unhealthy indefinitely; the outbox drains but the poison row
  keeps the gate red; alert fatigue.
- **Recovery**: manual SQL flip of the row (no tool).
- **Corrective pattern**: implement the draft's §1.4 — classify permanent (4xx/422/409,
  invalid receipt) → terminal state + alert; exclude terminal rows from
  `OldestPendingAuditGovernance`/`Ready`. `DeliveryError.Permanent` already exists in the
  **sink's** outbox package — port that classification to the vault relay.

### F4 — High (idempotency): vault fact IDs are random UUIDs per creation; the sink's `event_id` dedup and `idempotency_key` uniqueness are vacuous for vault events
- **Evidence** (Verified): `facts.go` `factFromEvent/factFromAudit/factFromGap` all set
  `ID: uuid.NewString()`; `http.go` `governanceWire` sends `EventID: fact.ID` and
  `IdempotencyKey: fact.ID`. Sink dedup is by `(tenant_id, event_id)` (`service.go`
  `store.EventKey(tenantID, event.EventID)`) plus `UNIQUE (tenant_id, idempotency_key)`.
  Draft §1.3's deterministic derivation is absent.
- **Triggering failure**: any path that regenerates a fact for the same origin (gap
  reconcile after an outbox row is lost/manually cleared, replay tooling, dual relay
  instances after lease loss are safe — row persists) produces a **different event_id** for
  the same business occurrence → sink accepts two ledger events.
- **User impact**: duplicate audit facts, wrong event counts, legal-hold/export duplication;
  integrity spot-checks (aero-id `VerifyChain`) do not cross-check the sink.
- **Recovery**: detect via `audit.integrity.verify` + manual de-dup at sink (write API only).
- **Corrective pattern**: derive the fact ID deterministically from the origin anchor
  (`SHA-256(service|tenant|origin_kind|origin_id)`) so any regeneration collapses at the
  sink; or add the origin reference to the envelope payload and have the sink dedup on it
  (schema registry change). Same for aero-id call sites that pass no `EventID`
  (`account.go:155,184`, `audit_hold.go:59`, `legacy_cutover.go:88` → random ID).

### F5 — High (loss): aero-id service-layer audit calls run post-commit with errors discarded — silent audit gaps
- **Evidence** (Verified): `service/account.go:155,184` and `callback.go`
  Register/Delete/ReplayDelivery call `_ = s.audit.Record(...)`; `Record` opens its **own**
  transaction (`audit_ledger.go` `Append` → `db.Transaction`), i.e., not in the business tx.
  In-tx `AppendInTx` is used only in repository paths (F-state map).
- **Triggering failure**: crash or DB error between business commit and `Record`; deadlock
  on the partition advisory lock; connection pool exhaustion.
- **User impact**: account/callback operations missing from both the local ledger and the
  sink, with no alert (error discarded).
- **Recovery**: none automatic; integrity spot-checks surface gaps only if someone runs them.
- **Corrective pattern**: move these calls into the business transaction (`AppendInTx`) or,
  if post-commit is deliberate (draft: "auth rows post-commit keyed by request_id"), make
  failure observable: metric + alert (metrics hook `ObserveAuditIngestFailure` exists) and
  **never discard** the error. This contradicts the draft's claim that account rows are
  "written in the account transaction" — the draft must say which paths are in-tx and which
  are at-least-once-with-alert.

### F6 — Medium (latency/throughput): aero-id delivers every event twice (inline publish in `Record` + durable publication worker), and the inline publish blocks the request up to the 5s HTTP timeout
- **Evidence** (Verified): `audit_ledger.go` `Record` appends the publication row **and**
  synchronously calls `publisher.Publish(ctx, fact)` with the request context
  (`_ =`); `drainPublications` then re-delivers the same fact via the row. Publisher timeout
  5s (`publisher.go` `defaultConfig`). Sink dedup absorbs the duplicate.
- **Triggering failure**: sink slow/unreachable → every audited request suffers up to 5s
  added latency (ctx-bound, but requests typically complete before the timeout) while the
  durable path re-sends anyway; sink receives 2× load.
- **User impact**: request tail latency; doubled ingest volume (matters at the F1 capacity
  envelope); wasted token/bandwidth.
- **Recovery**: none needed (dedup), but capacity is wasted.
- **Corrective pattern**: drop the inline publish (the outbox worker is the single delivery
  path) or make it best-effort async with its own short timeout; keep sink dedup as backstop
  either way. This restores the draft's "audit never blocks business flow".

### F7 — Medium (forensics): clock-skew policy is unimplemented; sink accepts arbitrary `occurred_at`
- **Evidence** (Verified): no future/past skew check exists in `Ingest`/`validateEvent`
  (grep of `service.go`, `models.go`, `canonical.go`); `occurred_at` drives retention
  cutoffs and query ordering. Draft's ">5min future reject / >5min past annotate" is
  `[PROPOSED]` only.
- **Triggering failure**: producer clock rollback (or a tenant VM with wrong clock) →
  `occurred_at` older/newer than stream siblings; sink `ledger_seq` order ≠ wall-clock order.
- **User impact**: misordered timelines, retention windows computed on wrong cutoffs, legal
  hold evidence with unreliable timestamps.
- **Recovery**: correct clocks; events already ledgered cannot be reordered (hash chain).
- **Corrective pattern**: implement the proposal at the sink (reject >5min future at
  ingest, annotate >5min past); producers run NTP and the relay should stamp a
  `received_at`-style observation time on the envelope (sink already sets
  `event.ReceivedAt` at ingest — Verified — use it for ordering/retention decisions).

### F8 — Medium (availability): aero-id's token source never invalidates on 401; credential rotation can stall delivery for up to ~24h
- **Evidence** (Verified): aero-id `publisher.go` `Publish` → `client.Do` →
  `validateReceipt` (no status handling); `oauth.go` caches tokens with TTL cap 24h and
  refreshes only at `refreshAt`. Vault `http.go` explicitly calls `Invalidate` on 401.
- **Triggering failure**: client secret rotated or token revoked at the AS → every publish
  fails with 401 for up to the cached TTL (worst case ~24h minus 30s skew).
- **User impact**: audit pipeline down; aero-id dead-letters recoverable events after 8
  attempts (F9); vault recovers on next refresh.
- **Corrective pattern**: port vault's 401 → invalidate; add a scope-missing alert path
  (draft §1.5 says "if scope missing → alert", unimplemented).

### F9 — Medium (loss): aero-id dead-letters after 8 attempts (~2–3 min at 1s→1h backoff) with no automatic or tooled replay
- **Evidence** (Verified): `callback_audit.go` `publicationOutcome` → `dead_letter` at
  `attempt >= maxAttempts` (default 8); `retryDelay` 1s base doubling, cap 1h. No
  publication replay tool exists (grep of `cmd/`, `internal/app` — `ReplayDelivery` is
  callback-only). Draft §1.4 "replay tool flips 3→0" is Missing for aero-id.
- **Triggering failure**: sink outage (5xx) longer than ~2–3 min, or a 5xx storm → events
  permanently dead even though the sink is fine again.
- **User impact**: ledger gaps for events that were recoverable; manual SQL fix required.
- **Corrective pattern**: classify by status class — 5xx/429/network retry with a long
  horizon (vault-style, capped), 4xx/422/409 dead immediately with alert; build the replay
  tool (or a `flip` subcommand) before rollout.

### F10 — Medium (topology): sink multi-replica operation is optimistic-single-writer; conflicts surface as ingest 5xx → producer retry churn
- **Evidence** (Verified): `store.go` `OpenPostgres` comment: "Multiple replicas can share
  the row; the optimistic version check surfaces lost-update conflicts"; `Store.mu`
  serializes only within one process. `ErrSnapshotConflict` → `Ingest` error → 5xx.
- **Triggering failure**: two sink replicas serving traffic (misconfig, failover overlap,
  rolling deploy) → version conflicts → 5xx → producers back off (jittered — good) → lag.
- **User impact**: transient ingest failures and lag; vault readiness flaps at maxLag.
- **Recovery**: stop one writer; conflicts resolve on retry.
- **Corrective pattern**: document "exactly one active writer; standby replicas are
  cold-warm" as the only supported sink topology; optionally add leader election or a
  `LEADER` flag in the audit API deployment.

### F11 — Medium (durability gap): the IdP's "local audit log" L0 is volatile — the reference audit-service is an in-memory ring buffer
- **Evidence** (Verified): `/home/u1/snaplink/audit/client.go` is an HTTP client to a remote
  Audit Service; `/home/u1/snaplink-sso/audit-service/main.go` implements `auditStore` as an
  in-memory `ring.Ring` with a capacity cap (no persistence). Draft §1.6 P4 backfill of
  "legacy local audit data" assumes a durable local log.
- **Triggering failure**: restart of audit-service loses all buffered history; capacity cap
  drops old entries.
- **User impact**: the pre-cutover audit history the backfill plan expects does not exist;
  any "L0 complement" story built on it is false.
- **Recovery**: none.
- **Corrective pattern**: decide explicitly: (a) accept and document the loss window, (b)
  make snaplink's local log durable (it already has `stdlog` and `memory` variants — pick a
  durable one for production), or (c) drop the backfill claim. P4 must not promise
  reconstructing what was never durable.

### F12 — Low (contract drift): draft constants disagree with all implementations
- **Evidence** (Verified): draft batch 100 vs aero-id 50 (`callback_audit.go`) vs vault 32
  (`config_audit_governance.go` default); draft backoff cap 300s vs aero-id 1h (`callback.go`
  `retryDelay`) vs vault 300s; draft `UNIQUE(service, event_id)` vs actual
  `UNIQUE (origin_kind, origin_id)` (vault), `UNIQUE (tenant_id, source, event_id)`
  (aero-id), `UNIQUE (fact_id)` (aero-id publications).
- **Impact**: the P5 "registry gate" will fail against real schemas; tuning surprises.
- **Corrective pattern**: the draft should be rewritten to state the **contract** (unique
  keys, dead-letter classes, at-least-once semantics) and leave constants (batch, backoff,
  lease) as per-service operational parameters with verified defaults.

### F13 — Info (P0 dependency missing): the sink's self-audit writer does not exist; its outbox relay and Kafka consumer are unwired
- **Evidence** (Verified): `internal/outbox/` + `cmd/audit-outbox-relay` +
  `cmd/audit-kafka-consumer` exist; no in-repo producer enqueues to `audit_outbox` (grep of
  `internal/service`, `internal/store`, `cmd/*` shows only the relay consuming).
  Governance ops (legal hold/export/retention/checkpoint) append nothing to their own
  ledger.
- **Impact**: the draft's own P0 ("sink: ingest API + self-audit writer") is not met;
  governance actions are un-audited today.
- **Corrective pattern**: implement the self-audit writer (same-tx append of `system:` /
  governance events into the sink's own store, or enqueue to its outbox) before any producer
  cutover; wire or remove the orphaned outbox/Kafka components.

### F14 — Info (contract fragility): `wait_for` semantics are cosmetic, and the default (`""`) yields receipts that fail publisher-side validation
- **Evidence** (Verified): sink `Ingest` always writes the ledger synchronously; `wait_for`
  only shapes the returned receipt status. With `wait_for` empty the receipt status is forced
  to `accepted` (`service.go`), which both publishers' `validateReceipt` reject (they require
  `ledgered/indexed/archived`). Both producers pass `wait_for=ledgered` today, so this is
  latent; the sink's own outbox relay HTTP deliverer does not (Unknown — it only checks
  2xx, `internal/outbox/http.go` not read in full).
- **Corrective pattern**: freeze `wait_for=ledgered` as the platform ingest contract for all
  publishers, and document that the sync ack is bounded by the sink's snapshot write, not a
  long-poll.

## 3. Scenario table

| Scenario | Behavior today (Verified unless noted) | Result | Gap / corrective |
|---|---|---|---|
| **Partition** (producer ↔ sink network cut) | aero-id: inline publish fails (discarded), publication rows accumulate, retry 1s→1h; vault: retry 1s→300s, rows accumulate; sink keeps serving reads | No business impact; delivery resumes after partition heals | F5/F6 (aero-id request-path latency + discarded errors); F9 (dead_letter if outage > ~2–3 min in aero-id) |
| **Sink crash / restart** | snapshot is atomic (rename / single-row UPDATE); standby loads and resumes; mid-ingest requests see 5xx → producer retry | No ledger corruption; brief ingest outage | F1 (single-writer topology), F10 (dual-writer conflicts) |
| **Producer crash between sink commit and ack** | aero-id: publication row still pending → worker re-claims (lease) → redeliver same `event_id` → sink `Duplicate` receipt → success. Vault: same via claim row; `CompleteAuditGovernance` guarded by claim token | At-least-once satisfied | F4 (only if fact regenerated with new ID — not in this path) |
| **Producer crash between business commit and outbox write** | aero-id service paths: `Record` post-commit, error discarded → **event lost silently** | Audit gap | F5 — move in-tx or alert |
| **Duplicate delivery** | sink dedup by `(tenant_id, event_id)` with content-digest compare; duplicate → 2xx; both publishers validate `!Conflict` | Absorbed | F4 — only safe while event_id is stable; vault regenerated IDs break it |
| **Clock rollback (producer)** | sink accepts arbitrary `occurred_at`; no skew check; hash chain freezes order | Misordered/duplicate-timestamp timeline; retention cutoffs wrong | F7 |
| **Stale token cache / credential rotation** | vault: 401 → invalidate → refetch; aero-id: keeps failing until TTL (~24h worst case) | aero-id delivery outage + dead_letter | F8 |
| **Sink 409 (event_id content conflict / idempotency_key reuse)** | aero-id: retry 8× → dead_letter; vault: retry forever → `Ready()` red permanently | aero-id: manual fix; vault: health gate stuck | F3, F9 |
| **Sink 422 (schema unknown / payload invalid)** | same as 409 path; draft's "→ dead" not implemented (vault) / delayed (aero-id) | Poison rows block readiness / waste attempts | F3 |
| **Dependency outage (scope/AS down)** | token fetch fails → `ErrTokenUnavailable` → retry; sink 401/403 → same class | Delivery stalls until AS recovers | F2 (wrong scope = permanent), F8 |
| **Recovery sequencing (sink back up after long outage)** | vault: backlog drains with 300s-cap jittered backoff; aero-id: rows ≤8 attempts survive, older rows already dead; `Ready()` clears once oldest pending < 900s | Partial loss in aero-id unless replayed | F9 (replay tool Missing), F3 (poison rows keep gate red) |
| **Vault binding flip (draining → active)** | `ApplyAuditGovernanceBindings` revision-gated; unbound backlog **blocks startup** (fail closed); reconcile enqueues gap origins with new random fact IDs | Safe, but re-enqueued facts get new IDs (F4); draining rows block `Ready()` | F4 |
| **Sink ingest surge** | per-tenant quota (EventsPerSecond) → 429; producers jittered-backoff | Backpressure works; whole-snapshot rewrite is the ceiling | F1 |

## 4. Stated guarantees vs reality, unsupported topologies, tests, residual risks

### Stated guarantees
| Guarantee (draft) | Status | Notes |
|---|---|---|
| Outbox written in the same tx as the business op | **Partial** | Verified in aero-id repository paths + vault (`auditedRepository`); **not** in aero-id service-layer `Record` call sites (F5) |
| Never drop; business never blocked | **Partial** | Vault: yes (but never dead = F3). Aero-id: drops silently (F5) and blocks request path up to 5s (F6) |
| Sink dedup by `(source_service, event_id)` | **Partial** | Sink side Verified; producers don't guarantee stable event_ids (F4: vault random, aero-id random at several call sites) |
| FIFO per partition | **Not implemented** | Vault delivers concurrently; aero-id claims ordered by `next_attempt_at` (retries jump the queue). Sink assigns `ledger_seq` at arrival — best-effort ordering is the real guarantee |
| Per-tenant ordering best-effort | **Verified** | Sink sequence assignment serialized per ingest |
| Skew >5min reject/annotate | **Proposed only** | Unimplemented (F7) |
| Permanent failures → dead + alert | **Partial/Missing** | aero-id: dead after 8 attempts (all classes); vault: never dead (F3); alert thresholds exist only as draft |
| Sync minimal ack for extreme-risk ops (tenant.delete, master-key reset) | **Unknown** | No code path found in this review (snaplink IdP side not implemented); listed as open decision #4 in the draft |
| Sink self-audit fail-closed (governance op aborts if its own append fails) | **Missing** | No self-audit writer exists (F13) |
| Fail-open boundary (audit never blocks business) vs fail-closed (governance ops) | **Partial** | Business path fail-open holds except aero-id inline publish latency (F6); the governance fail-closed half is not implemented |

### Unsupported topologies (explicitly out of scope of the current code)
- **Multi-writer sink**: the ledger snapshot admits exactly one active writer; concurrent
  writers lose updates (F10).
- **Horizontal ledger partitioning / sharded ingest**: all events share one snapshot;
  capacity ceiling (F1).
- **Cross-region sink replication**: no WAL shipping, no secondary readers; standby =
  same-row optimistic takeover.
- **Kafka as a primary ingest path**: Kafka producer/consumer scaffolding exists in the
  sink (`cmd/audit-kafka-consumer`, outbox kafka delivery) but no producer wiring is
  verified; treat as Unknown, not a supported topology.
- **Multi-tenant fan-out via shared outbox**: the draft's `UNIQUE(service, event_id)`
  contract doesn't exist in any implementation's DDL (F12).

### Validation tests (must-run before promotion; none executed in this review)
1. **Sink concurrency**: two processes ingesting the same `(tenant, event_id)` with equal
   digest → one ledger row + Duplicate receipt; with different digest → 409 + Conflict
   receipt (extend existing `TestIngestIdempotencyConflictAndIntegrity`).
2. **Vault poison row**: ingest a fact that will 422 (unknown action) → assert it reaches a
   terminal state (test will **fail today** — F3) and `Ready()` stays green.
3. **Vault crash-between-send-and-ack**: kill the relay after publish, before
   `CompleteAuditGovernance` → redelivery with the **same fact ID** → sink Duplicate
   (idempotency holds); then clear the outbox row and re-run the reconcile → assert the new
   fact **does not** create a second sink event (fails today — F4).
4. **Aero-id sink outage > 8 attempts**: assert dead_letter rows and verify the replay tool
   exists (fails today — F9).
5. **Aero-id token revocation**: revoke at the AS → assert next publish refreshes (fails
   today — F8).
6. **Clock skew**: ingest with `occurred_at` +10 min → expect rejection (fails today —
   F7); decide the policy before cutover.
7. **Cross-repo E2E** (suite compose per `MULTI_REPO_OPERATIONS.md` §1): login → file
   delete → `file.delete.admin` + notify in the delete tx → query ledger; assert exactly
   one ledger event per origin and matching `event_id` across redelivery.
8. **Capacity**: measure snapshot rewrite cost at N events (the sink's ingest benchmark
   exists: `internal/service/bench_test.go` `BenchmarkIngest`) and derive the per-tenant
   quota budget for F1.

### Residual risks
- The sink's O(ledger) snapshot write is the platform-wide throughput ceiling; no design
  change or budget exists yet (F1).
- Aero-id silent-loss window (F5) and vault permanent-poison/readiness coupling (F3) are
  the two most likely first incidents in production.
- Idempotency contracts (F4) mean "at-least-once" is only as strong as the producer's
  event_id stability — currently weak for vault and for several aero-id call sites.
- Scope contract drift (F2) will fail at deployment, not at review time — fix drafts and
  scope matrix before rollout.
- `snaplink-console` status was taken from the draft's citations (repo not present in this
  workspace) — re-verify `audit_log_service.dart` behavior before relying on the
  edge-generated read-event design.
- L0 durability of the IdP's local audit log is a ring buffer; backfill (P4) plans built on
  it are vacuous (F11).

---

*Advisory only. No files were modified. All code claims are Verified against the local
checkouts at review time; re-verify after any schema/relay change.*
