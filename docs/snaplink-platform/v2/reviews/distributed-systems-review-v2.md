# Distributed-Systems Review v2 — Per-Service Audit Ingestion (v2 design vs current code)

> **Scope**: `docs/snaplink-platform/v2/audit-ingestion-design-v2.md` (+
> `scope-matrix-v2.md`, `audit-event-registry-v2.md`, `implementation-batches.md`)
> vs the **current code** of the 6 repos, reviewed with a distributed-systems
> lens: consistency, ordering, atomicity, idempotency, ownership,
> conflict-resolution, OAuth/session/JTI/revocation state, retry/backoff,
> failover, clock, and the documented fail-open/fail-closed boundary.
> **Status**: advisory. Claims labeled **Verified** (read from code this
> review), **Partial**, **Missing**, **Proposed** (design-only), **Unknown**.
> **What actually ran**: static reads + targeted greps at each repo's current
> working tree; no test suites or live integration executed. Repo roots:
> `/home/u1/snaplink` (IdP), `/home/u1/aero-id`, `/home/u1/aero-vault`,
> `/home/u1/aero-im`, `/home/u1/workspace/demo/snaplink-audit-governance`
> (sink), `/home/u1/workspace/demo/snaplink-console` (console).
> Revision: implementation batches COMPOSE-2026-017 B0 (baseline, pre-B1..B6).

## 1. State map — owner, store, durability, consistency, replication, failover

| State | Owner | Store / table | Durability | Consistency | Replication | Failover |
|---|---|---|---|---|---|---|
| **Sink ledger** | sink `audit-api` | whole-snapshot: file (atomic rename, **no fsync**) or PG single row `audit_state_snapshot` (jsonb + optimistic `version`) — `internal/store/store.go`, `postgres.go` (Verified) | ledgered = snapshot Save; file backend: rename without fsync → **unbounded loss window on power failure** (Verified); PG: versioned UPDATE | serialized per process (`Store.mu`); cross-process optimistic CAS → `ErrSnapshotConflict` → **bare 500, no retry** (Verified) | none — effective single writer; 2+ replicas share one row, losers 500 | standby takes over; in-flight ingest during failover → 500 → producer retry (absorbed by dedup) |
| Sink streams/segments | sink | in snapshot: per-(tenant,stream) `NextSequence`, hash chain, segments, checkpoints (Verified) | same as snapshot | per-stream sequence + PrevHash chain; sealed segments; order = **ingest arrival order (ledger_seq), not occurred_at** (Verified) | none | via snapshot |
| Sink ingest dedup | sink | snapshot `Events` + `Receipts` keyed `tenant\x1f event_id`; idempotency-key scan is **O(ledger) per ingest** (Verified) | same | duplicate content → `Duplicate` 202; different content → 409; idempotency_key reuse → 409 (Verified) | none | — |
| Sink quota | sink | in-memory per-instance (`quotaMu`, `quotas`) (Verified) | volatile | **per-replica, not global** (Verified) | none | reset on restart (burst forgiveness) |
| Sink self-audit | sink | `AdminActions` appended **inside the same Store.Update** for export/legal-hold/restore ops (Verified, F13 partial) | same | governance op + admin row atomic (fail-closed on append failure) (Verified) | none | — |
| IdP sessions/refresh tokens | snaplink | session store (memory/redis/postgres), refresh keys `refresh:<token>` (Verified) | per backend (PG/Redis durable; memory volatile) | single-store; rotation = Get→Delete(best-effort)→Create, **non-atomic → concurrent-replay family split** (Verified) | multi-replica via shared Redis/PG | replica shares store; JWT access tokens **stateless, no jti, not revocable** (Verified) |
| aero-id ledger + outbox | aero-id | `audit_ledger` (HMAC chain) + `audit_publications`, **same tx** in repo paths (`AppendInTx`: account_profile/operation/sync_job/event_receipt/operation_audit — Verified); **post-commit `_ =` in 8 service sites** (Verified) | DB tx | per-(source,partition) serialized via `pg_advisory_xact_lock` + in-tx dup check (Verified); dedup `(tenant, source, event_id)` | DB-level | lease expiry → redelivery; dead_letter has **no replay tool, no purge, no alert** (Verified) |
| aero-id relay | callback worker (`drainPublications`, batch 50, lease 5min, bulkhead 4) | in-process | — | sequential oldest-first drain; retry all classes ×8 (`callback.max_attempts: 8`, backoff 1s base) → dead_letter; **no permanent-class immediate death** (Verified) | multi-worker via SKIP LOCKED + lease | lease_owner-guarded finish; crash → redelivery |
| aero-vault outbox | vault | `audit_governance_outbox`, `UNIQUE (origin_kind, origin_id)` + `audit_governance_delivered_origins` tombstone (migration 0039 — **no status/dead column**) (Verified) | same tx as `audit_log`/`object_events` insert when binding active (`auditedRepository` wrap, `RecordAuditWithGovernance`/`InsertEventWithGovernance` — Verified); `HardDeleteObjectWithEvent` = delete + audit + outbox facts in **one tx** (Verified) | claim `UPDATE … FOR UPDATE SKIP LOCKED`, revision-filtered, lease TTL 30s (> 2×HTTP timeout enforced by config validation), batch 32; **per-fact goroutines → no per-tenant ordering to sink** (Verified) | multi-worker safe | lease/claim_token guarded; complete requires live lease (fencing) (Verified) |
| aero-vault relay | `Runtime` (reconcile→deliver→cleanup loop) | in-process | — | **retry forever, no dead state** (`retryFact` → `RetryAuditGovernance` unconditionally; backoff 1s→300s ±25% jitter) (Verified); 401 → token invalidated (Verified, F8 vault side OK) | N workers | lease expiry; gap reconcile rescan (Verified) |
| aero-vault readiness | `Runtime.Ready()` | — | — | **fails /readyz** on binding drain or oldest pending > 900s maxLag (Verified — H1 gap) | — | — |
| OAuth token caches | per producer | in-memory, refresh skew 30s, TTL cap 24h (aero-id singleflight; vault serialized mutex) (Verified) | volatile | vault invalidates on 401 (Verified); **aero-id has no Invalidate** (Verified — F8 gap); **vault accepts empty scope list** (`validTokenScopes`: len 0 OK) (Verified) | — | refetch on expiry |
| aero-im audit | aero-im | local `audit_events` trail (`crates/aero-storage/src/audit.rs`, tests `#[ignore = "requires live Postgres"]`) — **no governance connector** (Verified) | local DB | local only | — | — |
| console audit | console | browser `localStorage` 1000-ring (`lib/services/audit_log_service.dart`) — **not evidence** (Verified) | volatile | local only | — | — |

**Delta vs v1 review**: sink dev-token posture (S1) and readyz store probe, vault Ready() decoupling (H1), dead-letter terminal state (F3), deterministic event IDs (F4), aero-id in-tx/`_ =` inventory (F5), token invalidation (F8), IdP claim contract (S2) + discovery truthfulness (F-3), self-audit (F13/F-06) — all verified against current code below.

## 2. Findings (by severity)

### DS-01 — Critical: sink dev-token auth defaults ON and dev-only config is accepted (S1 violated in code)
- **Evidence** (Verified): `cmd/audit-api/main.go` `flag.Bool("allow-dev-auth", boolEnv("AUDIT_ALLOW_DEV_AUTH", true), …)` — default **true**; `internal/auth/verifier.go:53-55` `ValidateConfiguration` passes with `AllowDev=true` and **no JWKS/public-key/secret** (dev-only config is not refused); `internal/auth/auth.go` `parseDevToken` + `Claims.Allows` → `Permissions[p] || Platform` — `dev:platform:platform-admin` bypasses every permission including `audit:event:write`; `deploy/docker-compose.verify.yml:12` sets `AUDIT_ALLOW_DEV_AUTH: "true"`; no manifest scan in CI (Missing).
- **Triggering failure**: operator deploys with default env (or copies verify compose); any caller with `dev:` prefix is authenticated as platform.
- **User impact**: unauthenticated write/read/export/legal-hold/delete access to the entire audit ledger; audit records can be forged or suppressed — the one system whose integrity is the product.
- **Recovery**: set `AUDIT_ALLOW_DEV_AUTH=false` (or configure a real trust source, which then makes `dev:` tokens 401 by the `AllowDev` gate) and restart; audit for exposure; key rotation if secrets leaked.
- **Corrective pattern**: default false + refuse dev-only trust config at startup + CI scan of manifests (implementation batch B1-1, T-1.1). Fail-closed boundary must be **code-default**, not operator-discipline.

### DS-02 — Critical (availability): vault outbox retries forever and one poison row flips vault /readyz to 503 (F3 + H1 combined)
- **Evidence** (Verified): `internal/auditgovernance/relay.go` `retryFact` → `store.RetryAuditGovernance` with **no attempt cap and no terminal state**; migration 0039 has no `status`/`dead_at` column; `internal/repository/audit_governance_claim.go` `RetryAuditGovernance` only stamps `available_at_ns`; `runtime.go` `Ready()` returns error when `OldestPendingAuditGovernance` > `maxLag` (default 900s), wired into `/readyz` via `cmd/server/audit_governance.go:51-65` + `cmd/server/http.go:39,59`; zero `Observe` calls in the relay (H6, Verified).
- **Triggering failure**: sink rejects one event with a permanent class (422 schema, 409 content conflict, tenant mismatch) — e.g. after sink schema registry updates — or sink outage > 900s. The row retries forever; backlog age crosses 900s → `/readyz` 503 → LB drains the **entire file platform**.
- **User impact**: file platform outage caused by an audit side-channel; audit events silently stuck (or lost at sink if they eventually succeed after retention cleanup).
- **Recovery**: manual SQL to clear/fix the row; restart.
- **Corrective pattern**: permanent-class → terminal dead state (≤1 attempt), transient bounded retry cap 300s, dead rows excluded from lag/readiness, `degraded` + alert at maxLag×0.5, never 503 (B3-1/B3-2, T-3, D1). Also fixes the design's own §1.4 "retry-forever is a contract violation" — currently the code is the violation.

### DS-03 — High: sink snapshot store — optimistic-lock conflicts surface as bare 500s; no retry; file backend has no fsync (F-01)
- **Evidence** (Verified): `internal/httpapi/server.go` `statusForError` — `store.ErrSnapshotConflict` falls to `default → 500`; `internal/service/service.go` `Ingest` has no retry around `Store.Update`; `internal/store/store.go` `fileBackend.Save` = write tmp + `os.Rename`, **no `File.Sync()`/dir sync**; `postgres.go` single-row CAS (`UPDATE … WHERE version = $2` → `ErrSnapshotConflict`).
- **Triggering failure**: two `audit-api` replicas (or one replica + a burst) sharing the PG row: every lost CAS → 500 → producers retry (vault forever, aero-id ≤8) → self-inflicted 500 storm under load; power loss on file backend → last events lost (window unbounded — no fsync).
- **User impact**: ingest errors (T-6 fails today: "2 concurrent ingest → no 500"); audit gaps after crash with file backend.
- **Recovery**: single-writer deployment today; restart.
- **Corrective pattern**: bounded jittered retry on `ErrSnapshotConflict` + `statusForError` mapping to 409/503 domain error; fsync or documented loss window; `/readyz` store probe (B1-2, T-6). Until the relational ledger decision (#7), the design must declare single-writer as the only supported topology.

### DS-04 — High: deterministic `event_id` (F4) is not implemented anywhere; regeneration and gap-reconcile mint new IDs → sink dedup is vacuous on redelivery paths
- **Evidence** (Verified): vault `internal/auditgovernance/facts.go` — `factFromAudit`/`factFromEvent`/`factFromGap` all use `ID: uuid.NewString()`; `reconcile()` re-derives facts for gaps via `factFromGap` → **new UUID per regeneration**; aero-id `internal/service/audit_ledger.go` `buildAuditFact` `ID: idgen.NewID()`; explicit `EventID: idgen.NewID()` at `service/audit_hold.go:59,74` and `service/legacy_cutover.go:88` (3 sites without an anchor, per design F4).
- **Triggering failure**: sink outage → vault gap-reconcile re-enqueues regenerated facts → **new event_id** → sink treats as new events → duplicate ledger rows (audit trail says two events where one occurred); aero-id row-clears/replays have the same property. The design's §1.3 "regeneration MUST reuse the original event_id" is unimplemented.
- **User impact**: ledger inflation and false audit records; legal-hold/export windows distorted; integrity-verify still passes (chains are self-consistent) so the corruption is silent.
- **Recovery**: manual dedup by (origin_kind, origin_id)/(tenant, source, event_id).
- **Corrective pattern**: derive `event_id` per registry v2 §1.9 with the per-occurrence component (request_id/counter) so regeneration reuses it (B2-4, B3-3, T-4); sink-side `Duplicate` then absorbs replays. Note the vault's local `UNIQUE (origin_kind, origin_id)` + `delivered_origins` tombstone already prevents **local** re-enqueue — the gap is cross-service, at the sink.

### DS-05 — High: aero-id 8 post-commit `_ = Record(...)` sites silently discard even the local durable append (F5)
- **Evidence** (Verified): `service/account.go:155,184`, `service/callback.go:160,221,247`, `service/audit_hold.go:59,74`, `service/legacy_cutover.go:88` — all `_ = s.audit.Record(...)`; `Record` (`service/audit_ledger.go:66-81`) returns the append error, but callers discard it — so the business op proceeds and the caller cannot retry, roll back, or log. The metric `ObserveAuditIngestFailure` does fire inside `Record` on append error (partial observability, wired at `app/new.go:148`), but there is **no alert coupling, no caller-visible failure, and no replay**; silent loss remains the effective behavior for the 8 sites. In-tx paths (repo `AppendInTx`, 6 sites) are correct (Verified).
- **Triggering failure**: append fails (dup-check race, chain lock error, DB outage at that instant); business op succeeds; audit record missing with **zero observability**; gap reconcile does not exist at aero-id (unlike vault).
- **User impact**: compliance-relevant actions (legal hold set/removed, cutover decisions, admin account views/lists, callback registration/deletion) unrecorded.
- **Recovery**: none automatic.
- **Corrective pattern**: move into business tx (`AppendInTx`) or make failure observable (counter + alert, `_ =` forbidden by grep gate) (B2-1, T-5).

### DS-06 — High: IdP refresh-token rotation is a non-atomic Get→Delete→Create; concurrent replays split the family; JWT access tokens have no jti and are not revocable
- **Evidence** (Verified): `sso.go` `RefreshWithRotation` (970-1059) — `sessionStore.Get` → `Delete` (best-effort, error ignored) → `Create` new refresh; three separate store calls, no compare-and-delete, no reuse detection, no family revocation; JWT claims (see DS-07) have no `jti`; revocation is only consulted in `Validate` (cookie path) via `tokenStore.IsRevoked` (`sso.go:811`); `LogoutAll` revokes refresh tokens only when the session store is listable (`DeleteByUser`), else `Revoke("user:"+userID)`.
- **Triggering failure**: attacker replays a stolen refresh token concurrently with (or after) the legitimate refresh; both Get succeed before either Delete → two live refresh tokens; no reuse alert. Also: revoking a client whose access tokens are JWTs does nothing until `exp`.
- **User impact**: prolonged session hijack; "sign out everywhere" does not kill JWT client credentials (audit publisher tokens are client_credentials JWTs, TTL-bounded — exposure window = TokenExpiry).
- **Recovery**: `Revoke`/`DeleteByUser` (works for cookie sessions and refresh tokens); JWT expiry.
- **Corrective pattern**: single atomic rotate (store-level CAS or tx), reuse → revoke whole family + alert (RFC 6749 §10.4/6819 §4.1.2); add `jti` + short-lived access tokens for service clients (relevant to B4 claim-contract work; T-10.1 exercises the 401-invalidation half on the consumer side).

### DS-07 — High: IdP does not mint the claim contract (S2): no kid/iss/scope/client_id/tenant_id/roles; sink then cannot do client/tenant binding from real tokens
- **Evidence** (Verified): `token/jwt/generator.go` — header is `{alg, typ}` only (no `kid`); `Claims{Subject, Audience, IssuedAt, ExpiresAt, Issuer, UserID, AppID, AuthMode, Permissions}`; `sso.go` `ClientCredentials`/`RefreshWithRotation` fill sub/aud/iat/exp/uid — **no iss (Issuer omitted when empty), no scope, no client_id, no tenant_id, no roles**. Sink `internal/auth/auth.go` `clientIdentity` tolerates missing `client_id`/`azp` → `ClientID = ""`; `tenant_id` missing → `claims.TenantID = ""`; `require()` then depends on source allowlist with empty client. Discovery (`cmd/server/routes.go` `oidcDiscovery`) advertises `token_endpoint=/authenticate` (real token route is `/token`), `revocation_endpoint=/revoke` (**no /revoke route exists → 404**), `response_types_supported: code` with `authorization_endpoint=/login` (no `/authorize`), and `token_endpoint_auth_methods_supported` includes `client_secret_basic`/`private_key_jwt` while the handler only reads form fields (`client_secret_post`). No scope validation/registry in `h.token` (F-3/P0-3, Verified).
- **Triggering failure**: T-1.2 e2e with a real IdP token: sink gets `client_id=""`, `tenant_id=""` → `resolveIngestTenantFromData` (sources.go:101-131) requires a source whose `AllowedClientIDs` contains "" — so **IdP-token ingest fails 403 today**; only dev tokens (and empty-allowlist sources) work. This is exactly why the implementation batches order B4 (IdP) before B1-7 (e2e) — but it means the entire trust path is currently **unexercised in production code**.
- **User impact**: cannot cut over producers to real tokens; discovery-driven clients break (`/revoke` 404, wrong token_endpoint); scope authorization is effectively absent.
- **Recovery**: keep dev tokens off; no production cutover until B4.
- **Corrective pattern**: mint claim contract in `buildAccessPayload`/generator (kid header, iss configurable with allowlist — not Host-derived, `routes.go` discovery uses `r.Host` when issuer unset, Verified), scope registry with `invalid_scope` 400, discovery truthfulness + conformance sweep (B4-1..4, T-2/T-8).

### DS-08 — Medium: sink silently overwrites envelope `tenant_id` with the token tenant instead of rejecting mismatch (S2 tenant consistency)
- **Evidence** (Verified): `internal/service/service.go` `Ingest` — after `resolveIngestTenant`, `event.TenantID = tenantID` (envelope tenant clobbered); no comparison, no 422. Design §Sink requires "envelope tenant_id must match publisher token tenant_id claim; mismatch → 422".
- **Triggering failure**: a buggy/compromised producer sends tenant A events with tenant B credentials — the ledger records them under B with no error; operators cannot tell the producer envelope was wrong.
- **User impact**: silent cross-tenant misattribution in the audit ledger (the one record that must be trustworthy).
- **Recovery**: none (records already attributed).
- **Corrective pattern**: fail-closed compare (envelope vs token `tenant_id`) → 422 + alert (B1-2/B4 scope; add T-8 assertion).

### DS-09 — Medium: sink `/readyz` does not probe the store; gRPC ingest is an active but undeclared topology; outbox-relay/Kafka-consumer are in the verify compose (topology not converged)
- **Evidence** (Verified): `internal/httpapi/server.go` `readyz` checks only the archive (S3/FileStore); store outage → still 200 (T-6 "停 PG → readyz 503" fails today); `cmd/audit-api/main.go` registers `grpcapi.Register` when `--grpc-listen` set; `internal/grpcapi/server.go` `WriteStream` ingests with the same `Service.Ingest`; `deploy/docker-compose.verify.yml` runs `audit-outbox-relay` + `audit-kafka-consumer` services. Design §Sink declares "HTTP ingest API + self-audit writer" as the only supported topology.
- **Triggering failure**: PG down with LB health-checking `/readyz` → traffic continues → 500s; a gRPC client or outbox relay enqueues events through a path the contract doesn't declare, then the contract's guarantees (self-audit, tenant consistency) are silently bypassed.
- **User impact**: availability confusion; ungoverned ingest paths.
- **Recovery**: none today.
- **Corrective pattern**: readyz store probe; declare or remove gRPC; wire-or-remove relay/Kafka with a grep gate (B1-2/B1-6, T-6).

### DS-10 — Medium: no read-path self-audit (F-06/F13): queries/exports are unrecorded; console target state blocked
- **Evidence** (Verified): `QueryEvents`/`GetEvent`/`downloadExport` in `internal/service/service.go` append nothing; only internal `AdminAction` rows exist for governance mutations (export/legal-hold/restore, same-update — Verified partial). Design mandates `audit.event.read`/`audit.event.export` in the same store update; console `audit_log_service.dart` is a localStorage ring (Verified).
- **Triggering failure**: an auditor reads the ledger — no record of the read; direct API reads bypass console-only logging entirely.
- **User impact**: can't answer "who read this event"; console shows non-evidence.
- **Recovery**: none.
- **Corrective pattern**: same-update self-audit rows on read/export paths, fail-closed on read success (B1-4/B1-5, B6-1, T-12).

### DS-11 — Medium: per-tenant delivery ordering is not guaranteed to the sink (vault) and ledger order ≠ occurred_at order everywhere
- **Evidence** (Verified): vault `deliverBatch` spawns **one goroutine per fact** (batch 32 concurrent HTTP POSTs) — claim ORDER BY gives batch order, delivery interleaving destroys it; aero-id drains sequentially oldest-first (good); sink assigns `Sequence` per (tenant, stream) at **arrival** and queries sort by `OccurredAt` (service.go eventsFor sorts with tie-break) — with concurrent delivery, `ledger_seq` can invert relative to `occurred_at`; no clock-skew check at the sink (design's ">5min future reject" is Missing).
- **Triggering failure**: producer clock rollback/freeze (or vault concurrency) → events with `occurred_at` inverted relative to ledger sequence; export/legal-hold windows by occurred_at silently exclude/duplicate.
- **User impact**: replay/export of a stream yields a different order than ingestion; forensic reconstruction diverges from the ledger.
- **Recovery**: none.
- **Corrective pattern**: either serialize per-partition delivery (single worker or hash-partitioned FIFO, per design §1.2) or declare arrival order as the only ordering guarantee; add sink skew annotation/rejection (B1-2/B3-1 scope, T-8 assertion).

### DS-12 — Medium: aero-id publication dead-letter is a black hole; all failure classes retry ×8 with no classification
- **Evidence** (Verified): `internal/service/callback_audit.go` `publicationOutcome` — only `errAuditTenantUnbound` is special-cased (pending retry); everything else retries to `MaxAttempts` (config `callback.max_attempts: 8`) then `dead_letter`; no replay tool, no purge, no alert on `dead_letter` count (grep found only the status transition); `internal/outbox/repository.go` TTL purge exists only for the **notification** outbox, not `audit_publications`.
- **Triggering failure**: a 422-class event (schema drift) burns 8 attempts then dies silently; operator has no tool to requeue after the schema is fixed.
- **User impact**: permanent audit loss invisible to ops.
- **Corrective pattern**: permanent-class immediate dead + replay tool + alert threshold (B2-5, T-10.2).

### DS-13 — Low: vault token source accepts an empty scope list; sink-side scope comes from roles/scope claims, not a registry
- **Evidence** (Verified): `internal/auditgovernance/token.go` `validTokenScopes` — `len(scopes)==0` accepted; sink `auth.go` `parseJWT` merges `roles` and `scope` claims into permissions — the sink trusts whatever the IdP puts in the token; with no scope registry at the IdP (DS-07) there is no enforcement point that a client's granted scopes match the token.
- **Triggering failure**: IdP misconfiguration mints tokens without scope → vault proceeds (fails at sink with 403 → retries forever per DS-02); no registry means a client can be granted `audit:event:write` without any provisioning record.
- **Corrective pattern**: require exactly `audit:event:write` in token source; IdP scope registry (P0-3) + grant revocation (B4-2, T-8(d), T-11).

### DS-14 — Info: positive anchors verified (the parts that are already distributed-systems-correct)
- Vault delete flow: `HardDeleteObjectWithEvent` — legal-hold check + delete + L0 audit + outbox facts in **one tx**, double-delete race → ErrNotFound rollback (Verified, `internal/repository/event_outbox.go:96-141`).
- Vault claim fencing: `CompleteAuditGovernance`/`RetryAuditGovernance` require live lease + owner/token match ("claim lost" otherwise) — stale deliveries cannot complete after lease expiry (Verified).
- Vault regeneration anchor: `UNIQUE (origin_kind, origin_id)` + `delivered_origins` tombstone inserted **in the same statement** as the outbox-row delete (cleanup) — no re-enqueue window (Verified, `audit_governance_cleanup.go`).
- aero-id: lease-fenced `FinishPublication` (`status='delivering' AND lease_owner`), `pg_advisory_xact_lock` per partition → strictly ordered chain; activation gate fail-closed (`enabled: false` default; empty bindings + enabled → `NewOAuthTokenSource` ErrInvalidConfig at boot; `validateAuditBindingCoverage` requires bindings for all tenants with pending publications) (Verified).
- Sink dedup semantics (same digest → 202 Duplicate; different → 409; idempotency-key reuse → 409) with **reconstruction-based digest compare** (fallback to stored digest on schema/key changes — preserves idempotency across schema deregistration) (Verified).
- Sink receipt contract honored by both producers: 202 + JSON receipt, event_id/tenant match, `conflict=false`, status ledgered/indexed/archived (Verified, vault `http.go` `validateReceipt`, aero-id `publisher.go`).

## 3. Scenario table

| Scenario | Behavior today (Verified) | Guarantee violated / risk | Outcome & recovery |
|---|---|---|---|
| Sink partition/outage (relay reachability) | vault: 5xx/network → backoff retry (forever); aero-id: ≤8 retries → dead_letter | vault §1.4 retry cap; DLQ visibility | backlog grows → vault /readyz 503 at 900s (DS-02) → LB drains file platform; recovery: sink back, relay drains; aero-id events past 8 attempts lost |
| Producer crash between send and mark-sent | vault: lease TTL 30s → re-claim → redelivery; aero-id: lease 5min → redelivery | at-least-once, dedup absorbs — **only if event_id stable** (DS-04: stable within a row, unstable on regeneration) | duplicate delivery → sink 202 Duplicate (same fact row); regenerated facts → new ledger rows (DS-04) |
| Replica crash of sink (PG row shared) | second replica CAS conflict → 500 (no retry) | F-01 | ingest errors; producers retry; recovery: single-writer ops or B1-2 retry |
| Sink file backend power loss | rename without fsync | F-01 | last N events lost (unbounded window); snapshot may be stale; recovery: restore from archive/backup; integrity verify detects chain break at last segment only |
| Clock rollback at producer | occurred_at trusted; no skew check (Missing) | ordering/export windows | ledger_seq vs occurred_at inversion (DS-11); no detection |
| Clock rollback at sink | `ReceivedAt = s.Now()` (injectable clock); receipt `AcceptedAt` | — | bounded by config clock; same as above |
| Stale token cache (revoked publisher credential) | vault: 401 → Invalidate → refetch (F8 OK); aero-id: **no invalidation** — continues with cached token until refreshAt | F8 (aero-id) | aero-id stalls up to refresh-skew 30s…TTL (T-10.1); vault recovers next publish |
| Token with empty scope claim | vault accepts (DS-13); sink 403s → vault retries forever | fail-open at token source | stuck rows (DS-02) |
| Dependency outage: IdP down | token refresh fails → `ErrTokenUnavailable` → retry (vault forever / aero-id ≤8) | no dependency on IdP for ingest itself | same as sink outage |
| Dependency outage: PG down (vault) | claim/complete fail → warn; no health flip except Ready() store errors → 503 "backlog lookup failed" | H1 | vault readyz 503 while PG down (arguably correct — core DB down); differs from design's audit-only decoupling |
| Split-brain (2 relays, same DB) | lease + claim_token fencing; both deliver → sink dedup | at-least-once, dedup absorbs | duplicates collapse at sink (stable IDs); ordering interleaves (DS-11) |
| Stale reads (query during ingest) | snapshot Read = Load (file: in-memory; PG: row read) — single-process mutex serializes | linearizable per process; multi-replica: read-your-writes not guaranteed across replicas | console may briefly miss the event it just wrote (B6-1 read path) |
| Governance op mid-failure | adminAction appended same Store.Update — atomic (Verified) | F13 registry-conformant events missing | partial state impossible for legal hold/export; but events not registry-conformant (DS-10) |
| Refresh token replay (IdP) | non-atomic rotate; no reuse detection (DS-06) | RFC 6819 | two live tokens; recovery: manual revoke |
| Quota under multi-replica | per-instance counters (DS: quota) | burst = EPS+Burst × replicas | rate limit not global |

## 4. Stated guarantees, unsupported topologies, validation, residual risks

### Stated guarantees (design §1, vs code)
| Guarantee | Design | Code today |
|---|---|---|
| Outbox row written in business tx; business flow never awaits relay | §1.1, 04-composition | **Partial**: vault delete/event paths same-tx (Verified); aero-id 6 repo paths in-tx, **8 service sites post-commit with discarded errors** (Verified); snaplink/aero-im/console: no outbox at all (Verified) |
| At-least-once delivery, sink dedup absorbs duplicates | §1.2/1.5 | **Partial**: dedup works for stable IDs; regeneration mints new IDs (DS-04); sink conflict → 500 no retry (DS-03) |
| Dead-letter terminal state, retry cap, 422→dead | §1.4 | **Missing**: vault retries forever (DS-02); aero-id 8 attempts for all classes, no replay (DS-12) |
| Readiness never gated on audit backlog | §1.4 (H1) | **Violated**: vault `/readyz` 503 at maxLag 900s (DS-02) |
| 401 → token invalidation | §1.5 (F8) | **Partial**: vault yes; aero-id no (DS-12/DS-05 note) |
| Deterministic event_id, regeneration reuse | §1.3 (F4) | **Missing everywhere** (DS-04) |
| Dev-token default OFF, dev-only refused, manifest scan | §1.7 (S1) | **Violated**: default ON, dev-only accepted, no scan (DS-01) |
| Claim contract minted by IdP | S2 | **Missing** (DS-07) |
| Tenant consistency 422 on mismatch | sink § | **Violated**: silent overwrite (DS-08) |
| Read-path self-audit, governance self-audit fail-closed | F-06/F13 | **Missing** (DS-10); governance mutation admin rows exist same-update (Partial) |
| Sink conflict retry + no bare 500 | F-01 | **Violated** (DS-03) |
| readyz store probe | F-01 | **Missing** (DS-09) |
| Supported ingest topology = HTTP + self-audit | sink § | **Violated**: gRPC active, relay/Kafka in verify compose (DS-09) |
| Activation gate fail-closed (bindings + flag) | F-03 | **Partial**: aero-id + vault boot checks exist (Verified, DS-14); snaplink/aero-im have no connector to gate |

### Unsupported topologies (must be declared, not accidental)
1. **Multi-replica sink ingest** — two+ `audit-api` replicas sharing the PG snapshot row: CAS conflicts → 500s (DS-03). Supported today: single active writer; standby must not serve ingest.
2. **Multi-replica IdP with per-replica session stores** (memory backend) — refresh/session state is store-local; only shared Redis/PG stores give cross-replica sessions/revocation.
3. **Parallel ingestion of one tenant stream from multiple relays** — ordering collapses (vault's per-fact goroutines already interleave within one relay); per-tenant FIFO requires one worker per partition.
4. **Dev-token trust in any non-test deployment** — DS-01; compose `verify` file must not be copied to prod.
5. **Kafka/outbox-relay ingest** — functional binaries in the compose manifest, outside the declared contract (DS-09); any contract enforcement (self-audit, tenant consistency) is bypassable through them.

### Validation tests (mapped from implementation batches; status today)
| Test | Assertion | Today |
|---|---|---|
| T-1.1 | dev token → 401 on all routes with `AUDIT_ALLOW_DEV_AUTH=false` | passes only if env set; **default is true** (DS-01) |
| T-1.2 | IdP `/token` → ingest 202 ledgered → read 200 → export 202, dev-auth off | **blocked**: IdP claims missing → sink 403 (DS-07) |
| T-2 | discovery sweep; `token_endpoint == "/token"`; no advertised 404 | **fails**: `/authenticate`, `/revoke` 404 (DS-07) |
| T-3 | 422 → terminal state ≤1 period; `Ready()` true with dead rows | **fails**: no terminal state (DS-02) |
| T-4 | regeneration → sink Duplicate, exactly 1 ledger row | **fails**: new UUID per regeneration (DS-04) |
| T-5 | mock Record failure → counter increments or op fails; no `_ =` | **fails**: 8 `_ =` sites, no counter (DS-05) |
| T-6 | concurrent ingest → 1 row, 202+Duplicate, no 500; PG down → readyz 503 | **fails**: 500 on CAS; readyz 200 on store outage (DS-03/DS-09) |
| T-8(a–e) | claim contract + endpoint hardening | **fails** (DS-07) |
| T-9 | introspect without creds → 401 | unverified this pass (endpoint exists; hardening in B4-4) |
| T-10.1 | revoke → next publish refreshes immediately | **fails**: aero-id no Invalidate (DS-12 note) |
| T-10.2 | replay dead → redelivered; purge not silent | **fails**: no replay tool (DS-12) |
| T-11 | grant without relay → fail-closed rejection | unverifiable: no scope registry yet (DS-07) |
| T-12 | query → caller self-audit row; console server-side only | **fails**: no read self-audit (DS-10) |
| D1 | sink down 60min → vault readyz 200 + 450s alert | **fails**: 503 at 900s, no alert (DS-02) |

### Residual risks (accepted / to be decided)
1. **Local DB loss** (design §1.5): outbox rows die with the business tx; vault gap-reconcile + sink dedup mitigate only when the source row survives. No cross-service reconciliation between producer local ledger and sink ledger exists (integrity spot-checks are the only detector).
2. **Sink capacity envelope (#7)**: whole-snapshot RMW is O(ledger) per ingest and the idempotency-key scan is O(events) — `BenchmarkIngest` envelope unmeasured; high-volume classes (`auth.token.issue`, `message.send`) must not cut over before the relational-ledger-or-budget decision.
3. **L1 aggregation (#2)** and **backfill (#1)** open decisions block P3/P4 for token.issue/message.send and legacy data; until then the ‡ classes are either unrecorded at the sink or full-volume.
4. **Producer local clocks** are trusted end-to-end (no sink skew policy implemented — DS-11); any legal timestamping requirement needs the sink-side skew gate.
5. **aero-im**: no connector, no outbox DDL, 30 ignored PG tests — entire B5 is unbuilt; the design's "first Rust connector" has no existing in-tree reference (aero-id Go pattern only).
6. **Sink gRPC + self-audit**: gRPC WriteStream bypasses any future HTTP-only middleware guarantees; must be declared supported (with self-audit) or removed before cutover.
7. **IdP JWT revocation**: no jti → revocation requires TokenExpiry wait for service tokens; accept for TTL ≤ token expiry, or add jti + deny-list at the sink for audit-scope clients (sink currently accepts any non-expired signed token from the JWKS, including user tokens with an `audit:event:write` scope — client allowlist is the only gate).

## 5. Bottom line

The v2 design correctly identifies every distributed-systems defect that this review independently re-verified (S1, F3/H1, F-01, F5, F4, F8, F-3, F-06/F13) — the findings above confirm each one is **still present in code** at the B0 baseline, plus three defects the v2 docs under-weight: the sink's **envelope-tenant overwrite** (DS-08), the **IdP refresh-rotation race + missing jti** (DS-06), and the **active-but-undeclared gRPC ingest topology** (DS-09). The batch plan's ordering (sink P0 → aero-id → vault → IdP → im → console) is sound: nothing above blocks B1; T-1.2 cannot pass before B4; vault availability (DS-02) and sink dev-auth (DS-01) are the two release-blocking items and should be gated first within B1/B3. No code was modified by this review.
