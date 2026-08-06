# Persistence Review v2 — Store inventory, durability, wiring, and migration safety

> **Reviewer**: database-architect role (`prompts/README.md` applied).
> **Subject**: the v2 contract (`audit-ingestion-design-v2.md`, `scope-matrix-v2.md`,
> `implementation-batches.md`, `implementation-gate.md`) vs the **current code** of
> every persistence-bearing repository in the platform, one review round after the
> v2 design revision.
> **Evidence standard**: Verified (code read at cited symbol/path), Partial, Missing,
> Proposed (plan-only). Checks that ran for this revision: static reads of the
> store/query/write paths listed below in each repository at the pinned HEADs
> (sink `fb4fd97`, aero-vault `35ff4ce`, aero-id `756b65b`, aero-im `8658d02`,
> IdP `b3c839bb`, console `5dd1ab9`); `grep` scans for table/symbol usage across Go
> and Rust trees; no full CI was run. Line numbers cite the reviewed revision.

---

## 0. Executive summary

Every store in the platform is **durable-by-design** (Postgres/SQLite rows, WAL
archives) **except two explicitly-hot exceptions** — the IdP stock audit sink
(`backend: memory` ring buffer) and the console `localStorage` audit ring — plus
one durability gap on the sink's default file backend (no fsync).

The central problem is **wiring, not schema**: the sink's real persistence is a
whole-snapshot JSON blob (file or single Postgres row) that is rewritten in full
per event, while the purpose-built relational ledger (`migrations/001`) is **dead
schema** — no Go symbol references `ledger_events`/`ledger_streams`/
`ledger_segments`. Every ingest is O(snapshot) write + O(all-events) idempotency
scan; every query is O(all-events) scan. The batch plan already acknowledges this
(B1-3: "migration-001 接线 … 或 `BenchmarkIngest` envelope") — this review confirms
it is the release-blocking item for any production cutover, and adds the missing
measurement requirement.

Two findings are **Critical** because they are stock defaults, not opt-in mistakes:
(1) `audit-api` accepts `dev:*` tokens by default (`AUDIT_ALLOW_DEV_AUTH` defaults
true) — an authentication bypass on the default deployment; (2) the stock IdP
config ships `identity.backend: memory`, `oauth.backend: memory`,
`audit.backend: memory` — the stock binary loses all identity/audit state on
restart. Both are already on the batch plan (S1, B4-5) — the review verifies them
and sharpens the acceptance criteria.

Verified-good designs worth preserving: aero-id hash-chained append-only ledger
(advisory-lock serialized chain append + append-only trigger + signed records +
same-tx publication outbox), aero-vault lease-claim outbox (PG `SKIP LOCKED`),
aero-im trigger-enqueued delivery outbox with `UNIQUE(destination, idempotency_key)`,
IdP Redis refresh-token Lua rotation.

---

## 1. Store inventory

| # | Store (repo) | Purpose | Durability | Implementation | Stock wiring | Consistency requirement |
|---|---|---|---|---|---|---|
| S1 | Sink control-plane state — `snaplink-audit-governance` | Audit ledger: events, receipts, hash-chained streams, segments/checkpoints, retention, legal holds, exports, restores | Durable (by design); **fsync gap on file backend**; whole-blob rewrite | `internal/store/store.go` — snapshot map persisted either as JSON file (`os.WriteFile`+`os.Rename`, **no `Sync()`**) or single-row `audit_state_snapshot` (migration 004) with optimistic `version`; relational ledger (migration 001: `ledger_events`, `ledger_streams`, `ledger_segments`, RLS policies) is **dead schema — zero Go references** | `cmd/audit-api/main.go`: `AUDIT_POSTGRES_DSN` → PG single-row backend; else file (default `./data/state.json`). **No migration runner in any binary** — migrations applied by operator psql (e2e applies 001+003 only, never 004) | Single logical writer: in-process `Store.mu` + PG version CAS across replicas; `ErrSnapshotConflict` → today **HTTP 500, no retry** |
| S2 | aero-vault audit-governance outbox — `aero-vault` | Durable in-tx fact queue (admin/file events) → sink via OAuth CC HTTP; binding revision fencing; delivered-origin dedupe | Durable (Postgres or SQLite, migration 0039/0040) | `audit_governance_outbox` with claim lease (`claimAuditGovernancePostgres`: `UPDATE … FOR UPDATE OF o SKIP LOCKED … RETURNING`; SQLite: tx + row-wise UPDATE), `CompleteAuditGovernance`/`RetryAuditGovernance` fenced by owner+token, bounded backoff ≤300 s, delivered cleanup 7 d → `audit_governance_delivered_origins` dedupe table | Stock `cmd/server` wires runtime when `AUDIT_GOVERNANCE_ENABLED=true` (`main.go:69`, `audit_governance.go`); fail-closed boot: bindings file must exist, secrets mandatory, unbound backlog blocks disable; `Ready()` wired into `/readyz` (`runtimeReadiness`) | At-least-once per origin (`UNIQUE(origin_kind, origin_id)`), lease-fenced claim, revision-fenced bindings |
| S3 | aero-id audit ledger + publication outbox — `aero-id` | Hash-chained append-only audit ledger (account/security facts) + durable delivery queue to sink | Durable (Postgres) | `audit_ledger` (migration 0034): per-partition `pg_advisory_xact_lock`, `UNIQUE(source, partition_key, chain_sequence)`, `UNIQUE(source, event_id)`, append-only trigger, HMAC signature; `audit_publications` (0045) same-tx outbox; worker `drainPublications` (`callback_audit.go`) claims with SKIP LOCKED | Stock `cmd/server` wires ledger + publication loop (`app/new.go:179`); **but 8+ call sites still use post-commit `_ = s.audit.Record(...)`** (account.go:155,184, callback.go, audit_hold.go, legacy_cutover.go) and `Record()` still does **inline best-effort publish** (`audit_ledger.go:79` `_ = publisher.Publish`) — dual delivery path | Per-partition total order; at-least-once delivery via outbox; duplicate event_id rejected |
| S4 | aero-im audit trail + Snaplink delivery outbox — `aero-im` | Workspace security audit trail (append-only, daily RANGE partitions, legal-hold-aware retention sweep) + Snaplink audit/usage delivery | Durable (Postgres) | `audit_events` (migration 0146 partitions, 0154 legal-hold guard); `snaplink_delivery_outbox` (0235) with trigger `aero_enqueue_snaplink_audit` (0236) enqueuing **in the same transaction** as `audit_events` insert; CTE claim lease; `UNIQUE(destination, idempotency_key)` | Stock relay (`snaplink_commercial/runtime.rs`) wired when `snaplink_commercial_runtime.enabled=true` (default **FALSE**) + bindings; destination 'audit' uses `audit_client_id` credential (0237) | In-tx enqueue; at-least-once with idempotency key; binding identity fencing (`binding_for_claim`) |
| S5 | IdP identity/oauth/audit stores — `workspace/demo/snaplink` sso-server | Users/clients/sessions, refresh tokens, auth codes, MFA challenges, pairwise subjects, consent, audit events | **Stock config is hot**: `identity.backend: memory`, `oauth.backend: memory`, `audit.backend: memory` (ring capacity 10000) — all lost on restart; sqlite/postgres/redis backends exist and are wired per-config | `serverbuildstore/build_oauth_stores.go` (memory/sqlite/redis/postgres), `serverbuildauthn/build_audit_secrets.go` (`BuildPrimaryAuditSink` memory/sqlite/postgres), Redis refresh token Lua rotation (`infrastructure/redis/refresh_token*.go`) | Stock binary wires per config; HA gate `enforceHACoherence` refuses per-process stores in multi-replica mode; **no audit-governance connector in stock binary** (`infrastructure/auditgovernance` imported only by `cmd/snaplink-billing`, not `cmd/sso-server`) | Refresh-token atomic consume/rotate; audit chain in-process only (`hash_chain: false` stock) |
| S6 | Console audit ring — `snaplink-console` | Admin-operation display log | **Hot, non-authoritative**: browser `localStorage` (`sso_audit_log`, 1000 entries) | `lib/services/audit_log_service.dart` — client-side ring, forgeable via devtools, no server read path | Wired as the audit page's only source (B6-1 plans switching to sink reads) | None (evidence value = nil today) |
| S7 | Legacy monolith `snaplink` (IdP v1) — `/home/u1/snaplink` | Same domain as S5, older codebase; `token/jwt/generator.go` header `alg`/`typ` **without `kid`** (active IdP already stamps `kid`) | memory/postgres/redis backends (`store/{memory,postgres,redis}`) | — | Not the active IdP (HEAD 2026-05-19) | Note: batch evidence mixes S5 and S7 paths under "snaplink" (see F-13) |

---

## 2. Findings (severity-ordered)

### Critical

**F-1. Sink ingest is O(snapshot) write + O(N) idempotency scan on a whole-blob store — no capacity envelope exists (Verified).**
- Evidence: `internal/store/store.go` — `fileBackend.Save` marshals the *entire*
  snapshot (`json.MarshalIndent`) and rewrites the whole file per event; PG backend
  (`postgres.go`) `Load` decodes the entire `jsonb` row on **every** `Read`, and
  every `Store.Update` = load-whole-blob → mutate → save-whole-blob. `Ingest`
  (`service.go:353`) idempotency checks by iterating **all** events:
  `for existingKey, existing := range data.Events` (line ~414). `QueryEvents`
  (service.go:585) iterates `data.Events` and filters/sorts/cursors in memory.
  `grep -rn "ledger_events\|ledger_streams\|ledger_segments" --include="*.go"` →
  **zero matches** outside migrations.
- Impact: ingest cost grows linearly with total event count; at 10⁵–10⁶ events the
  hot path degrades to seconds per event; concurrent reads on the PG backend each
  pay a full-blob decode. `QueryEvents` is O(ledger) per request (batch B1-3
  acceptance "不再 O(ledger)" is correct).
- Recommendation: wire migration-001 relational ledger behind the same service
  interface with dual-write + digest comparison, or (cheaper, still required)
  benchmark and publish a capacity envelope (`BenchmarkIngest` per B1-3). The
  snapshot backend must remain readable (restore/fallback) — do not drop it.
- Validation: `go test ./internal/store/... ./internal/service/...` with an
  N=10⁴/10⁵ event benchmark asserting per-ingest p99 bound; `SELECT count(*) FROM
  ledger_events` vs snapshot event count after dual-write.

**F-2. Stock `audit-api` defaults to dev-token auth — authentication bypass on the default deployment (Verified).**
- Evidence: `cmd/audit-api/main.go`:
  `allowDev := flag.Bool("allow-dev-auth", boolEnv("AUDIT_ALLOW_DEV_AUTH", true), …)`;
  `internal/auth/auth.go:52` `if a.AllowDev && strings.HasPrefix(token, "dev:")`
  → `parseDevToken` grants `Platform=true` for `dev:platform:platform-admin`
  (auth.go:72), i.e. `audit:platform:cross_tenant` + every permission. Deploy
  manifest `deploy/docker-compose.verify.yml:12` sets `AUDIT_ALLOW_DEV_AUTH: "true"`.
- Impact: any caller holding the well-known token format is platform-admin on a
  default-configured sink; cross-tenant read/write/policy control.
- Recommendation: B1-1 (S1) is exactly right — flip default to **false**,
  `ValidateConfiguration` must reject `allow-dev-auth=true` combined with
  production trust material (and reject dev-only configs), and a CI manifest scan
  must fail on `AUDIT_ALLOW_DEV_AUTH: "true"` in non-test manifests. Also remove
  `AUDIT_ALLOW_DEV_AUTH: "true"` from `docker-compose.verify.yml` and mint IdP
  tokens in e2e instead (B1-7 already plans this).
- Validation: boot with default env → `POST /api/v1/events` with
  `dev:platform:platform-admin` must return 401 (currently 202); `--check-config`
  with dev-auth + real JWKS must exit non-zero.

**F-3. Sink file backend has no fsync — the default durability path can silently lose or revert the audit snapshot (Verified).**
- Evidence: `store.go` `fileBackend.Save`: `os.WriteFile(tmp, …)` → `os.Rename(tmp, f.path)` — no `file.Sync()` on the file and no directory fsync. Contrast:
  `internal/archive/archive.go:52` does `file.Sync()` for the WORM archive — the
  state store is weaker than its own archive.
- Impact: power loss/crash between write and rename (or rename without dir fsync)
  reverts the snapshot to a prior version; committed audit events vanish without
  error. For an audit store whose promise is tamper-evidence this is a data-loss
  path on the default backend.
- Recommendation: fsync file + parent dir on every save (or refuse file backend for
  production and require PG); add the fsync item to B1-2 ("fsync" is already
  listed) with an explicit crash test (kill -9 during ingest loop → snapshot
  integrity holds; `VerifyIntegrity` endpoint passes).
- Validation: fault-injection test killing the process mid-save then running
  `VerifyIntegrity` + counting events after restart.

### High

**F-4. aero-id dual delivery path + swallowed publish errors (Verified; B2-1/B2-2).**
- Evidence: `internal/service/audit_ledger.go:73-80` `Record()` appends (own tx,
  outside the caller's business tx) then `_ = publisher.Publish(ctx, fact)` inline
  (errors dropped). The same fact also gets a publication row in
  `createAuditRows` (audit_ledger.go) delivered by `drainPublications`
  (`callback_audit.go:44`). 8+ call sites use post-commit `_ = s.audit.Record(...)`
  (account.go:155,184; callback.go; audit_hold.go:59,74; legacy_cutover.go:88) —
  failures invisible (the `ObserveAuditIngestFailure` counter only fires on
  `repo.Append` error, not on publish error).
- Impact: 2× sink load per fact (B2-2 "2× 负载消除" confirmed); in-flight window
  where the inline path fails silently and the outbox path is the only record;
  audit gaps with no observability.
- Recommendation: B2-1/B2-2 as planned — move all call sites to `AppendInTx` on
  the caller's tx; delete the inline publish; make `_ =` a grep-gate failure;
  alert on `ObserveAuditIngestFailure` increments.

**F-5. aero-vault has no dead-letter terminal state; permanent failures retry forever and Ready() 503s the instance (Verified; B3-1/B3-2).**
- Evidence: `relay.go retryFact` always `RetryAuditGovernance` with
  `boundedBackoff` (cap = `MaxBackoffSeconds` 300); `classifyRelayError` labels
  errors but no path converts 422/409/tenant-mismatch/invalid-receipt to terminal.
  `runtime.go Ready()` fails when `OldestPendingAuditGovernance` age > `maxLag`
  (900 s default) → `readyz` 503 (wired at `cmd/server/audit_governance.go:51-59`,
  `http.go:46`). `OldestPendingAuditGovernance` counts **all** `delivered_at_ns=0`
  rows, including permanently-failing ones.
- Impact: a sink that permanently rejects an event (contract change, tenant
  mismatch) keeps that fact in the queue at ≤300 s cadence forever; a 60-minute
  sink outage flips `/readyz` → orchestrator restart loop (the D1 drill scenario),
  and restart cannot drain because the sink is still down.
- Recommendation: implement B3-1 (status/dead_at columns, partial index,
  `DeliveryError.Permanent` classification, dead rows excluded from claim/lag) and
  B3-2 (Ready returns degraded + 450 s alert instead of 503; exclude terminal rows
  from `OldestPending`). Keep `Ready()` failing on unbound backlog (that one is
  correct).
- Validation: D1 drill (stop sink 60 min → `/readyz` 200 + alert; no restart
  loop); 422 → terminal within one poll+backoff cycle.

**F-6. Stock IdP config is hot: memory backends for identity, oauth, and audit (Verified; B4-5).**
- Evidence: `cmd/sso-server/config.yaml` — `identity.backend: memory` (line 114),
  `oauth.backend` resolves `memory` default (line ~145 context; `BuildRefreshTokenStore` memory branch), `audit.backend: memory` (line 210, ring capacity 10000), `hash_chain: false` (line ~220), pairwise `backend: memory` (line 87).
- Impact: stock binary loses clients/users/sessions/audit on every restart —
  including the audit events the platform v2 wants to treat as evidence; the
  10-logins→10-rows L0 restart test (B4-5) fails today by default.
- Recommendation: flip stock defaults to sqlite (single-host) or require postgres
  for `audit.backend`; enable `hash_chain` (with deployment-stable salt file); make
  the L0 restart test a gate. Keep memory available for `topology.mode: single`
  dev but refuse it when `audit.api_enabled` or governance is on.

**F-7. Sink `ErrSnapshotConflict` surfaces as HTTP 500 with no retry — the multi-replica path is unusable under contention (Verified; B1-2).**
- Evidence: `internal/httpapi/server.go statusForError` has no case for
  `store.ErrSnapshotConflict` → falls to `http.StatusInternalServerError`;
  `postEvent` calls `Ingest` once with no retry; `Store.Update` serializes only
  in-process (`store.go` `mu.Lock`), so PG-backed multi-replica writes conflict on
  the version column.
- Impact: with >1 replica on PG, concurrent ingests produce 500s (not 409, not
  retried); events are not lost (client retries) but availability collapses.
- Recommendation: B1-2 as planned — map `ErrSnapshotConflict` to 409
  (`domain.ErrConflict` semantics or its own code) and add bounded jitter retry in
  the service layer; also add the readyz store probe (currently `readyz` checks
  only the archive, `server.go:161-174`) so a missing `audit_state_snapshot` table
  surfaces as not-ready instead of 500ing every request.

### Medium

**F-8. Sink migrations are never auto-applied; relational schema and snapshot schema drift (Verified; B1-3 context).**
- Evidence: no `migrate` runner in any sink binary; `test/e2e/fullstack.sh` applies
  001+003 manually via psql and **never applies 004** (`audit_state_snapshot`) —
  the e2e stack therefore exercises only the file backend; `audit-api --postgres-dsn`
  without an operator-applied 004 fails every Save with "relation does not exist"
  surfaced as 500.
- Recommendation: ship an idempotent migration step inside `audit-api` (or a
  dedicated migrate command) applying 001→004 in order with a schema-version row;
  extend e2e to run the PG backend path so T-6 ("PG CI 不 SKIP") is real.

**F-9. IdP access-token claims `tenant_id`/`roles` are missing; no scope registry; `iss` not allowlisted (Verified; B4-1..3).**
- Evidence: `infrastructure/defaultimpl/issue_payload.go:26` `buildAccessPayload`
  sets Iss/Sub/Exp/Nbf/Iat/Scope/ClientID/JTI/ACR/SID — **no `tenant_id`, no
  `roles`** claims (they can only ride `subject.Claims` via `Extra`). The `kid`
  header **is already present** in the active IdP issuers
  (`ed25519_issue.go:33`, `rsa_issue.go`, `ecdsa_issue.go` all stamp
  `Kid: kid`); it is missing only in the legacy monolith
  `token/jwt/generator.go` (alg/typ only — F-13 repo-mix trap). `/token` has a
  scope-count cap but **no scope registry** (`grep` for registry/invalid_scope
  enforcement → only count-cap and policy tests). `iss` derivation not
  allowlisted.
- Impact: without `tenant_id`/`roles`, the sink's per-tenant isolation
  (`tenantFor` from claims) and scope matrix cannot be enforced end-to-end;
  unknown scopes are minted silently. `kid` work in the batch should be scoped
  to the legacy tree only (or dropped for the active IdP).
- Recommendation: B4-1..3 as planned — additive claims first (kid/tenant_id/roles
  are additive; RS validators ignore unknown claims, so rollout is compatible),
  then scope registry enforcement **after** the matrix is granted to every client
  (grant-before-enforce to avoid breaking existing clients); issuer allowlist with
  Host-derivation removed.

**F-10. aero-im relay treats all failures as retryable — permanent 422s loop; audit coverage is audit_events-only (Verified; B5-1/B5-2).**
- Evidence: `snaplink_commercial/http.rs:116-119` any non-200 → `Err` →
  `mark_failed` → bounded backoff (≤300 s, `commercial_delivery_backoff`),
  no 422/permanent classification; `migrations/0236` trigger covers only
  `audit_events` (admin/security workspace events) — `message.*`/`room.*` events
  have no audit enqueue yet (B5-1 planned); `snaplink_commercial_runtime.enabled`
  defaults FALSE (delivery inert until operator enables — by design, but the
  batch's "首个 room.create 到 sink" acceptance requires the flag flipped in the
  target deployment).
- Recommendation: B5-1/B5-2 as planned — add `Permanent` classification for
  non-retryable statuses; add outbox/in-tx coverage for message/room/admin events
  per the DDL normative statuses; grant `audit:event:write` scope only after relay
  enablement (B5-4), keeping fail-closed rejection when unprovisioned.

**F-11. aero-vault reconcile gap queries are per-tenant anti-join scans on an unindexed column, every poll (Verified).**
- Evidence: `audit_governance_write.go listGovernanceAuditGaps`:
  `audit_log a LEFT JOIN audit_governance_outbox … LEFT JOIN …_delivered_origins
  WHERE a.tenant_id=$1 AND o.id IS NULL AND d.origin_id IS NULL ORDER BY a.id`
  — `audit_log` has only `idx_audit_log_created_at` (migration 0016), **no
  tenant_id index**; reconcile runs per bound tenant each poll (default 1000 ms,
  `runtime.go run()` → `reconcile()` → `ListAuditGovernanceGaps`).
- Impact: with large `audit_log`, every poll is a tenant-filtered seq scan per
  binding; CPU grows linearly with tenants × audit volume.
- Recommendation: add `(tenant_id, id)` index for `audit_log` (and confirm
  `object_events_tenant_idx` covers the event gap query — it does); throttle
  reconcile (e.g., only when outbox idle) or make it incremental with a cursor.

**F-12. Console audit ring is client-side and forgeable — not evidence (Verified; B6-1).**
- Evidence: `lib/services/audit_log_service.dart` — `localStorage` key
  `sso_audit_log`, 1000-entry ring, `record()` writes on every admin action,
  writable from devtools; no server round-trip.
- Impact: console's audit page presents forgeable data as audit (the v2 contract's
  whole point is that client-side rings must not constitute evidence; T-12
  "devtools 伪造不再构成证据" is exactly this).
- Recommendation: B6-1 as planned — audit page reads the sink read API via BFF
  (tenant_id + trace_id), localStorage ring demoted to a debug record; keep the
  ring but label it "debug only".

**F-13. Batch evidence mixes two "snaplink" codebases (Verified — metadata issue).**
- Evidence: implementation-batches.md attributes `cmd/server/routes.go`,
  `token/jwt/generator.go:52-55`, `sso.go:894-960` to "snaplink (IdP)", but those
  paths exist in the **legacy monolith** `/home/u1/snaplink` (HEAD 2026-05-19),
  while `infrastructure/defaultimpl/issue_payload.go` lives in the **active IdP**
  `/home/u1/workspace/demo/snaplink`. The identity-protocol review already flagged
  the repo identity split; the batch plan must pin one repo per row or the
  B4 tickets will be applied to the wrong tree.
- Recommendation: before executing B4, re-map each snaplink row to the active IdP
  repo paths (e.g., `interfaces/sso/server_token.go` for the token handler, not
  legacy `cmd/server/routes.go`).

### Low / Info

- **F-14 (Info).** Sink `Ingest` wait_for semantics: `waitFor=accepted` returns a
  receipt with `Status=Accepted` even though the event is already ledgered — by
  design (client-visible contract), but worth an API doc note.
- **F-15 (Info).** IdP Redis refresh-token store uses Lua-atomic consume/rotate
  with per-family rotation caps and subject/client HMAC indexes
  (`refresh_token.go`, `refresh_token_rotation.go`) — verified good; Redis is a
  hot store by intent (loss forces re-login only).
- **F-16 (Info).** aero-vault `delivered_origins` dedupe table grows unbounded
  (cleanup purges outbox rows after 7 d but never the dedupe rows) — plan a
  bounded retention for it.
- **F-17 (Info).** aero-id payload redaction (64 KiB truncate+sha256, sensitive-key
  list) and microsecond truncation before hashing (chain round-trip) are
  correctly handled (`audit_ledger.go`, `service/audit_ledger.go`).
- **F-18 (Info).** 674 `#[ignore]` tests across aero-im crates (incl. storage);
  B5-1's "30 个忽略测试 CI 全绿（37/37）" targets a subset — the acceptance should
  state which tests are un-ignored and why the rest stay ignored.

---

## 3. Query/index and transaction analysis (demonstrated hot/atomic paths)

**Hot path 1 — sink ingest (S1).** Not an indexed path at all: single-blob
read-modify-write serialized by `Store.mu`; cross-replica atomicity only via
`version` CAS (`postgres.go snapshotUpdateQuery WHERE id=1 AND version=$2`).
Correct but O(snapshot). The intended relational design (001) has correct indexes
(`idx_ledger_events_tenant_occurred`, partial `operation_id` index, outbox partial
pending index, RLS policies with `current_setting('app.tenant_id')`) — none of it
is reachable. **Recommendation**: wire 001 with per-tenant insert + receipt +
segment in one transaction (the B1-3 "migration-001 接线" option) — the schema
already contains everything needed, including the transaction-local tenant
setting for RLS.

**Hot path 2 — aero-id chain append (S3).** Verified atomic: duplicate check
(before lock) → `SELECT pg_advisory_xact_lock(hashtext(partition_key))` → duplicate
check (after lock, race-free) → chain-head read by `(source, partition_key) ORDER
BY chain_sequence DESC` (backed by `idx_audit_ledger_partition`) → append →
publication row, all in one tx; `UNIQUE(source, partition_key, chain_sequence)` +
`UNIQUE(source, event_id)` + append-only trigger are the backstops. This is the
reference implementation the other stores should match. **Only issue**: `List`
filtering/pagination keyset correctness relies on `idx_audit_ledger_query`
`(account_id, occurred_at DESC, id DESC)` — verified present.

**Hot path 3 — aero-vault claim (S2).** Verified atomic:
`UPDATE … SET attempts=attempts+1, claim_owner, claim_token, lease_expires_at
WHERE id IN (SELECT … FOR UPDATE OF o SKIP LOCKED … LIMIT n) RETURNING …` — no
double-claim possible; `Complete`/`Retry` are fenced on owner+token+lease. SQLite
fallback does row-wise UPDATE in one tx (correct, N+1 style). Index
`audit_governance_due_idx (delivered_at_ns, available_at_ns, lease_expires_at_ns,
created_at_ns)` supports the claim predicate.

**Hot path 4 — aero-im enqueue+relay (S4).** Verified: trigger inserts the
delivery row in the same tx as `audit_events`; `UNIQUE(destination,
idempotency_key)` makes redelivery idempotent; CTE claim (`claim_due`) with
`gen_random_uuid()` lease token; `binding_for_claim` fences delivery identity
against the trusted binding table. Backoff bounded at 300 s. Missing: permanent
classification (F-10) and moderation priority (B5-3).

**Hot path 5 — IdP refresh token rotate (S5).** Verified atomic via Lua script
(consume + rotate + family counter + subject/client index maintenance in one
server-side execution; `refresh_token_rotation.go` comment pins unit semantics).
Redis loss = forced re-login, documented hot store.

**Retention paths.** aero-im `audit_events` daily RANGE partitions with
legal-hold-aware `DELETE … NOT EXISTS (legal_holds)` sweep + partition DROP
(verified, migration 0146/0154). Sink retention is snapshot-map based
(`EvaluateRetention` iterates events; archive to WORM file/S3 best-effort).
aero-vault delivered-cleanup 7 d + dedupe table (unbounded, F-16).

---

## 4. Safe migration sequence

Ordering constraint from the batch plan (sink → aero-id → aero-vault → IdP →
aero-im → console) is sound; the sequence below adds compatibility-window details.

1. **P0 (parallel-safe): F-2 dev-auth default false** (config + CI manifest scan +
   `ValidateConfiguration` rejection), **F-3 fsync** on file backend, **F-7
   conflict→409+retry + readyz store probe**, **F-1 capacity measurement**:
   `BenchmarkIngest` envelope BEFORE any ledger wiring — the envelope decides
   whether the relational ledger must land before the first production tenant.
2. **aero-id (B2):** move 8 call sites to `AppendInTx` (behavioral change is
   within one tx per call — compatibility window: no schema change, only
   transaction scope; watch for lock-order regressions where the business tx
   already holds row locks, e.g. account row update then ledger append — append is
   advisory-locked per partition, so deadlock requires same-partition concurrent
   appends; validate with a concurrent-login test), delete inline publish, wire
   `ObserveAuditIngestFailure` alert. Rollback: revert call sites only; schema
   untouched.
3. **aero-vault (B3):** add `status`/`dead_at` columns + partial index (additive
   migration 0039 extension; old rows default `pending`), classify permanent
   errors, change `Ready()` to degraded+alert (roll-forward safe; rollback =
   revert Ready logic only). Add `(tenant_id, id)` index on `audit_log` (F-11)
   before enabling reconcile at scale.
4. **IdP token contract (B4-1..3):** claims are **additive** (`tenant_id`,
   `roles`, configurable `iss`; `kid` already present in the active IdP) —
   deploy before any consumer enforces them;
   then scope registry with **grant-before-enforce**: register the full
   scope-matrix-v2 table first, verify every client's requested scopes are
   registered, then flip unknown→`invalid_scope`. Discovery truthfulness (F-3) and
   endpoint hardening are independent of the registry and can land earlier.
5. **IdP governance connector (B4-5):** new wiring only — outbox in-tx +
   relay + `auth.token.issue` L1 aggregate; stock audit backend memory→sqlite/
   postgres + `hash_chain` with a deployment-stable salt file; activation gate
   env-gated (boot fail-closed when enabled without bindings — mirror aero-vault
   `AUDIT_GOVERNANCE_ENABLED` semantics). Rollback: flip env off; relay drain
   semantics from aero-vault apply.
6. **aero-im (B5):** message/room/admin in-tx enqueue via triggers (additive),
   permanent classification, moderation priority, scope grant **after** relay
   enablement. Compatibility: delivery is idempotent at the sink (event_id dedupe),
   so enabling the audit destination is safe with an existing sink; grant
   `audit:event:write` only after B4-2 registry is live.
7. **Console (B6):** read-path switch is client-only; keep the ring as debug
   fallback behind a flag.

**Validation queries per step** (run in the target env):
- Sink ledger parity: `SELECT count(*) FROM ledger_events` vs snapshot count
  (dual-write window) and `VerifyIntegrity` endpoint over each stream.
- Vault: `SELECT status, count(*) FROM audit_governance_outbox GROUP BY status;`
  `SELECT count(*) FROM audit_governance_outbox WHERE delivered_at_ns=0;` → must
  reach 0 after drain; D1 drill as specified.
- aero-id: `SELECT status, count(*) FROM audit_publications GROUP BY status;`
  (pending → 0); `VerifyChain(source, partition)` per partition.
- aero-im: `SELECT destination, count(*) FROM snaplink_delivery_outbox WHERE
  delivered_at IS NULL GROUP BY destination;` → 0; first `room.create` event
  present in sink.
- IdP: `POST /token` → header `kid` present; claims contain `tenant_id`/`roles`;
  unknown scope → 400 `invalid_scope`; discovery sweep green (T-2).

**Data-integrity checks:** sink `VerifyIntegrity` after every restart and after
the fsync fix; aero-id `VerifyChain` on a sample of partitions per release;
vault reconcile gap count must stay 0 (F-11 query returns no rows); IdP L0 restart
test (10 logins → 10 rows, no loss); console T-12 self-audit rows appear server-side.

---

## 5. Unknown volume, retention, and recovery assumptions

- **Volume (Unknown — measurements required):** no workload evidence anywhere in
  the reviewed trees: no events/sec per tenant, no ingest p99, no snapshot size
  growth curve, no segment/day rate. The sink bootstrap default
  `EventsPerSecond: 1000` (`cmd/audit-api/main.go` `bootstrap`) is a guess. The
  first deliverable of B1-3 must be a published envelope: snapshot bytes per
  event, ingest p99 at 10³/10⁴/10⁵ events, PG single-row vs file crossover point,
  and the relational-ledger comparison. All cutover gates (G8) should reference
  this envelope, not assumptions.
- **Retention (Unknown):** sink `hot_days 30 / warm_days 180 / archive_days 365`
  (`migrations/001 retention_policies` defaults) are unverified against any
  compliance requirement; aero-vault `DeliveredRetentionSeconds` 7 d default and
  the unbounded `delivered_origins` table (F-16) have no stated policy; aero-im
  `audit_events` `keep_days` is operator-supplied with no default visible at the
  call site; IdP audit memory ring 10 000 events is a capacity, not a policy.
- **Recovery (Unknown/Missing):** no backup/restore drill exists for the sink
  snapshot (file vs PG single-row), no PITR story for the file backend, no
  documented restore procedure for aero-id `audit_ledger` (append-only trigger
  blocks UPDATE/DELETE — restore must be insert-based), no backup verification
  for aero-im partitioned `audit_events`. Restore paths in the sink schema
  (`restore_runs`, `previewRestore`) exist but their drill is not evidenced.
- **Migration runner (Missing):** no automated migration application in any sink
  binary (F-8); operator manual psql is the only path today.

---

## 6. What the batch plan gets right and one correction

The plan's ordering (sink → aero-id → aero-vault → IdP → aero-im → console),
the fail-closed activation gates, the L0 restart test, the D1 drill, and the
"grant-before-enforce" scope sequencing all match the code. The one correction:
**every "snaplink (IdP)" row must be re-pinned to the active repo
(`/home/u1/workspace/demo/snaplink`) with its real paths before B4 execution**
(F-13) — otherwise the B4 work lands in the legacy monolith.

Advisory only: this review approves nothing and blocks nothing; it sharpens the
acceptance criteria for B1-1/B1-2/B1-3, B2-1/B2-2, B3-1/B3-2, B4-1..5, B5-1..4,
B6-1 and adds the measurement and recovery work the cutover gate (G8) will need.
