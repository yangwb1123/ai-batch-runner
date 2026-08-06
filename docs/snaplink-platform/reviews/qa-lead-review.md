# QA Lead Review — Per-Service Audit Ingestion (Test Adequacy vs. Design Invariants)

> **Role**: QA lead (`prompts/README.md` applied — current behavior established
> from executable code and **actually executed test commands**, not documented
> suites). **Subject**: `audit-ingestion-design-draft.md` and its realization in
> the five codebases (sink `snaplink-audit-governance`, producers `aero-id`,
> `aero-vault`, `aero-im`, IdP `snaplink`).
> **Status**: advisory. Evidence labels: **Verified** (command executed this
> revision), **Inspected** (code read this revision), **Cited** (from
> protocol/distributed/database reviews, cross-checked), **Missing** (no test
> exists), **Proposed** (draft-only).
> **Baseline**: prior reviews (`identity-protocol-review.md`,
> `distributed-systems-review.md`, `database-architect-review.md`) established
> 14+ architectural findings. This review maps **each design invariant to
> executable tests**, measures what actually runs, and lists the exact tests
> that will fail today.

---

## 1. Test inventory and commands actually run (this revision)

Default CI vs. tagged/manual suites — **distinguished**:

| Suite | Command (executed) | Result | Class |
|---|---|---|---|
| Sink unit+integration (Go) | `go test ./... -count=1` | **157 tests, all pass** | default |
| Sink race | `go test -race ./internal/outbox/ ./internal/service/ -count=1` | pass | default |
| Sink Postgres-gated | `go test ./internal/store/ ./internal/outbox/ -run Postgres -v` | **all SKIP** (no `AUDIT_TEST_POSTGRES_DSN`) | **tagged/manual** |
| aero-id all (48 pkgs) | `go test ./... -count=1` | **926 tests, all pass** (worktrees excluded by `go list`) | default |
| aero-id connector | `go test ./internal/connector/auditgovernance/ -count=1` | pass | default |
| aero-vault all | `go test ./... -count=1` | **1519 tests, all pass** | default |
| aero-vault relay/events race | `go test -race ./internal/auditgovernance/ ./internal/events/ -count=1` | pass | default |
| aero-vault Postgres integration | `go test -tags=integration ./internal/integration/ -run TestPostgresAuditGovernanceConcurrentClaimsAndLeaseRecovery` | **SKIP** (no PG; server tests pass) | **tagged/manual** |
| snaplink all | `go test ./... -count=1` | **1558 tests, all pass** | default |
| aero-im audit (Rust) | `cargo test -p aero-storage audit` | **7 pass, 30 ignored** (need live PG) | default + **tagged** |
| Sink e2e compose | `test/e2e/fullstack.sh` | **NOT run** (needs docker verify stack) — inspected: uses `dev:demo:tenant-auditor` tokens only | **manual/chaos-like** |
| Sink bench | `go test -run=^$ -bench=.` | **NOT run**; targets exist (`BenchmarkIngest`, `BenchmarkQuery`, `BenchmarkQueryLargeLedger`, `BenchmarkVerifyIntegrity` — `internal/service/bench_test.go`, `integrity_content_test.go:509`) | **bench** |
| Load/chaos/conformance | none found in any repo (grep chaos/litmus/gremlin/pumba) | **Missing** | — |

**CI wiring (Verified)**: aero-id CI runs `go test ./... -count=1 -race` (no
integration/bench); aero-vault CI runs plain `go test ./...` (no `-race`, no
integration); snaplink CI runs `go test -race -coverprofile=… ./...` **plus**
`go test -tags=integration -race ./integration/...`; the sink has **no GitHub
workflow** — `cli.py ci` = quality scans only, Go tests are a separate manual
`cli.py test`/`make test`. **No e2e, bench, or load suite runs in any CI.**

Line coverage was not generated (no coverage profile in this environment for
the sink; aero-vault CI collects one, snaplink CI collects one). Coverage
percentages are therefore **not reported as evidence** — per `prompts/README.md`,
line coverage alone does not establish behavioral adequacy; this review
reports test-presence per invariant instead.

---

## 2. Requirement-to-test matrix

Draft invariant → executable test → status. "Test" column names the test that
exists (package: `snaplink-audit-governance` = sink, `aero-id`, `aero-vault`,
`snaplink`, `aero-im`).

| # | Design invariant (draft §) | Existing test / evidence | Status |
|---|---|---|---|
| R1 | Outbox written in same tx as business op (§1.1, 04-composition C13) | vault `repository/audit_governance_test.go`, `event_outbox_test.go` (Inspected); aero-id `AppendInTx` in repo paths (`repository/operation.go:182`, `account_profile.go:101`) vs **8 post-commit `_ = s.audit.Record(...)` call sites** (`service/account.go:155,184`, `callback.go:160,221,247`, `audit_hold.go:59,74`, `legacy_cutover.go:88` — Verified) | **Partial** — in-tx paths tested; post-commit loss window **untested** (T-5) |
| R2 | Lease-based claim, SKIP LOCKED, crash-safe (§1.2) | vault `TestOutboxRelay_ClaimLostLeadsToReclaimNotDoubleSchedule`, `TestRuntimeRelaysAtomicAdminAndFileFactsAndDrains`; sink `TestRelayRunOnceDelivers`; aero-id callback delivery tests | **Partial** — SQLite/in-memory exercised; **Postgres SKIP LOCKED + lease recovery SKIP** without DSN (T-6) |
| R3 | Never drop; retries reuse event_id (§1.2/§1.3) | aero-id `TestPublicationOutcomeKeepsUnboundTenantRecoverable`; sink dedup `TestIngestIdempotencyConflictAndIntegrity` (dup→202 Duplicate, content-conflict→409); `TestHTTPIngestLargeAggregateVersionConflictNotDuplicate` (2^53 boundary); vault `TestOutboxRelay_ClaimLostLeadsToReclaimNotDoubleSchedule` | **Partial** — dedup strong at sink; **vault random-UUID fact IDs make redelivery-after-regeneration un-collapsible, untested** (T-4) |
| R4 | Permanent 4xx/422 → dead, poison doesn't block (§1.4) | sink relay `TestRelayDeadLettersPermanentError` + `TestRelayDeadLettersAfterMaxAttempts` (400/403/409 → `StatusFailed` immediately — Inspected `relay.go:160-166`); aero-id `TestPublicationOutcomeDeadLettersBoundPublisherFailure` (but only after 8 attempts) | **Partial** — sink relay covered; **vault has no dead state at all** (outbox DDL has no status column — Inspected `0039_audit_governance_outbox.up.sql`; `retryFact` for every error — `relay.go:85,97-100`); no test (T-3) |
| R5 | Backoff: exponential + jitter, rate-class 30–60s, cap 300s (§1.2) | sink `TestRelayBackoffCaps`; vault `TestBoundedBackoffIsDeterministicAndCapped`; aero-id `TestCallbackDelivery_TransientFailureRetriesThenDeadLetters` | **Covered** (constants differ per service — F12; draft must state contract not constants) |
| R6 | Sink dedup on (source, event_id); idempotency_key unique (§1.3, sink contract) | sink `TestIngestIdempotencyConflictAndIntegrity`, `TestIdempotencyKeyCannotBeReusedAcrossEvents`, `TestHTTPIngestLargeAggregateVersionConflictNotDuplicate`; `TestSensitiveFieldsAreEncryptedWithoutBreakingIdempotency` (digest stability) | **Covered** (executed) |
| R7 | Tenant isolation; source allowlist; fail closed (§sink) | sink `TestHTTPIngestQueryAndTenantIsolation`, `TestHTTPIngestRejectsCrossSourceWithoutEnumeration`, `TestIngestBindsSourceToAuthenticatedClient`, `TestSourceAccessFailClosedSameError`; vault `TestRedactionIsKeyedTenantScopedAndDomainSeparated`; aero-im `audit_list_is_tenant_scoped` | **Covered** (executed) |
| R8 | JWT verification at sink: alg allowlist, kid, iss, aud, client identity (§sink) | sink `verifier_test.go` 7 test groups (`TestRemoteJWKSSupportedAlgorithms`, `TestVerifiedJWTClaimsChecksRemainEnforced`, kid-uniqueness, alg confusion); `TestJWTClientIdentityUsesClientIDOrAZP` | **Covered in isolation — but only against hand-built JWTs; no token minted by the real IdP passes** (T-1) |
| R9 | Dev-auth off in production (security baseline) | sink `TestDevAuthRejectsWithoutOptIn` (unit, passes) | **Partial — default is ON** (`AUDIT_ALLOW_DEV_AUTH` default true, `cmd/audit-api/main.go:45`); all 157 sink tests + e2e use `dev:` tokens; no HTTP-level reject test (T-1) |
| R10 | Scope contract `audit:event:ingest`/`audit:event:write` (§1.2, Flow 3) | vault `TestTokenSourceRejectsExpandedScope`; aero-id `TestOAuthTokenSourceUsesPerTenantCredentialsAndRejectsUnboundTenant`, `TestOAuthTokenSourceRejectsCredentialReuse` | **Covered at producers**; **draft/scope-matrix name is stale** (everything enforces `audit:event:write`); **IdP has no scope registry and no scope claim — untested** (T-8) |
| R11 | Clock skew: >5min future reject / past annotate (§1.5) | `TestOutOfOrderOccurredAtEvents` (sink — sequence follows write order, timeline by occurred_at; **no skew bounds**) | **Missing** — no test rejects/annotates skew; no skew code exists (T-7) |
| R12 | Sync minimal ack for extreme-risk ops (§1.2, 04-composition) | none found in any suite | **Missing** (open decision #4; no code path) |
| R13 | Governance ops self-audited; read self-audit (§sink) | sink `TestAdminActionSelfAudit`, `TestHTTPAdminActionsSelfAudit` (mutations) | **Partial** — `QueryEvents` records no `audit.event.read` (Inspected `service.go`); untested (T-12) |
| R14 | Integrity: hash chain, signed checkpoints, restore approval | sink `TestVerifyIntegrity*` group (~15 tests incl. tamper, stream filter, concurrent ingest), `TestAggregateCheckpointCreatedAndVerified`, `TestRestoreApprovalWorkflow`; aero-id `VerifyChain`/canonical-bytes tests (`TestCanonicalAuditPayloadMatchesAppendSideBytes` 14 subtests) | **Covered** (executed) |
| R15 | Snapshot store capacity / concurrent writers (§sink) | sink `TestPostgresBackendConcurrentReadsRace`, `TestPostgresStoreIntegration` — **all SKIP** without DSN; `TestHTTPIngestLargeAggregateVersionConflictNotDuplicate` covers version-conflict-not-duplicate at HTTP layer | **Partial** — conflict semantics tested in-memory; **real Postgres single-row RMW contention untested in this env** (T-6); capacity **unmeasured** (no load suite) |
| R16 | Token refresh/rotation; 401 invalidation (§1.2) | vault `TestRuntimeCredentialRotationRequiresHigherRevisionAndUsesNewSecret`, `TestRuntimeCloseDrainsBlockedHTTPDelivery`; aero-id `TestOAuthTokenSource…` (fetch, refresh skew) | **Partial** — vault covered; **aero-id never invalidates on 401 (≤24h stall), untested** (T-10); connector fault-injection covers 503/429/timeout/malformed only |
| R17 | Relay HTTP hygiene: no redirects, endpoint policy, redaction | vault `TestPublisherRejectsRedirectAndInvalidReceipt`, `TestSecureEndpointRejectsRemoteHTTPAndCredentials`, `TestRelayLogsNeverExposeRawFactInputs`, `TestAuditSinkL2_ErrorsRedactTokenAndPayload`; aero-id `TestConnector_*` fault suite (9 tests) | **Covered** (executed) |
| R18 | Migration safety (append-only trigger, outbox DDL) | aero-id `migrations_test.go`, append-only trigger tests; sink migration 001 (relational schema) **unused by runtime** (Cited F-01) | **Partial** — producer migrations tested; sink relational schema dead code, untested |
| R19 | Identity protocol surface: `/token`, introspection, discovery, device (§OAuth) | snaplink `TestOIDCDiscovery` (**asserts wrong values**: token_endpoint=/authenticate, authorization_endpoint=/login — Inspected `routes_test.go:41-58`); `TestIntrospect_Missing*` (params only); engine `token_test.go` (mint/verify claims) | **Missing at HTTP layer** — no `/token` success/error test, no introspection-auth test, no device-auth test (T-8, T-9) |
| R20 | E2E: business op → outbox → relay → sink ledger (§MULTI_REPO §1) | sink `test/e2e/fullstack.sh` (relay→Kafka→consumer→ledger→CH→MinIO; **dev-token only**); no cross-repo compose suite exists | **Missing for the platform claim** — no IdP→producer→sink path (T-1) |

**Coverage summary**: R5/R6/R7/R8/R14/R17 are genuinely covered by executed
tests (the sink's digest idempotency, verifier hardening, integrity suite, and
both producers' HTTP hygiene are strong). R1/R2/R3/R4/R9/R10/R13/R15/R16 are
partial with specific holes. **R11/R12/R19/R20 are missing entirely.**

---

## 3. Findings

Sorted by severity. Every finding names the exact test to add and its
acceptance assertion. Tests marked *fails today* are the ones to write first:
they convert the cited architectural findings into executable gates.

### T-1 Critical — No test exercises the production trust path; every suite runs on forged dev tokens
- **Untested failure**: the F-2 end-to-end break — IdP client_credentials JWT
  (`snaplink/sso.go:894`, claims `{sub,aud,iat,exp,uid}`, no `kid` header,
  no `iss`/`scope`/`client_id`) can never satisfy the sink's hardened verifier
  (requires `kid`, validates `iss`/`aud`/permissions, source allowlist). All
  157 sink tests + e2e `fullstack.sh` authenticate as `dev:tenant:*`
  (Verified — grep of `internal/httpapi/server_test.go`, `auth_test.go`,
  `test/e2e/fullstack.sh:62,85`), and `AUDIT_ALLOW_DEV_AUTH` defaults **true**
  (`cmd/audit-api/main.go:45`). A release with this default flips the audit
  pipeline on with **zero security** and **zero functionality** simultaneously.
- **Regression risk**: any change to IdP claim minting or sink verification
  is invisible to CI; the F-2 break shipped because no test could see it.
- **Tests to add**:
  1. *Sink HTTP reject-dev*: `AUDIT_ALLOW_DEV_AUTH=false` → `POST /api/v1/events`
     with `Bearer dev:demo:compliance` → **expect 401**. (Passes today only if
     the flag is honored; run against both flag states.)
  2. *Cross-repo E2E production token* (suite compose per
     `MULTI_REPO_OPERATIONS.md` §1): `POST snaplink/token`
     (client_credentials, scope `audit:event:write`) → `POST sink/api/v1/events`
     with that Bearer, `AUDIT_ALLOW_DEV_AUTH=false` → **expect 202,
     `receipt.status=ledgered`**. *Fails today (403)* — release blocker.
- **Acceptance assertion**: `resp.StatusCode == 202 && receipt.Status == "ledgered"` with `AUDIT_ALLOW_DEV_AUTH=false` and a token minted only by the IdP's `/token` endpoint.

### T-2 High — Discovery conformance test codifies the wrong contract
- **Untested failure**: F-3 — `TestOIDCDiscovery` (snaplink `routes_test.go:41`)
  asserts `token_endpoint == /authenticate` and
  `authorization_endpoint == /login`; `/revoke` 404s; issuer from Host header.
  The test is a **regression lock on the defect** — it passes green while every
  standards-driven client fails.
- **Tests to add**: *Discovery sweep* — fetch `/.well-known/openid-configuration`,
  then for each advertised endpoint (token_endpoint, revocation_endpoint,
  jwks_uri, device_authorization_endpoint if advertised) issue the documented
  request and **expect 2xx/405 (route exists), never 404**; assert
  `token_endpoint == "/token"`; assert advertised auth methods ⊆ implemented.
  *Fails today* for `/revoke`, `/authenticate`, device metadata.
- **Acceptance assertion**: `resp.StatusCode != 404` for every advertised
  endpoint and `metadata.token_endpoint == "/token"` (or the test is rewritten
  to assert the corrected metadata).

### T-3 High — Vault poison-event/readiness coupling untested (no dead state)
- **Untested failure**: F-3/F-10 — vault outbox has no status column
  (`0039_audit_governance_outbox.up.sql` — Inspected); `retryFact` retries every
  error class forever (`relay.go:85,97-100`); one 409/422 row permanently
  red-flags `Ready()` (maxLag 900s) → orchestrator restart loops.
- **Tests to add**: *`TestVaultPoisonEventReachesTerminalState`* — inject a fact
  the sink rejects with 422 → run `deliverFact` → **expect a terminal/dead
  state and `Runtime.Ready()==true`**. *Fails today* (no terminal state).
- **Acceptance assertion**: after a 422, the outbox row's
  `delivered_at_ns`-terminal marker is set within one delivery cycle AND
  `OldestPendingAuditGovernance` excludes it (Ready stays green).

### T-4 High — Vault event_id determinism untested (dedup vacuous on regeneration)
- **Untested failure**: F-4 — vault fact IDs are `uuid.NewString()`
  (`facts.go`), so sink dedup on `(tenant_id, event_id)` cannot collapse a
  regenerated fact; gap-reconcile after a cleared row creates a **second ledger
  event** for the same business occurrence.
- **Tests to add**: *`TestVaultRegeneratedFactCollapsesAtSink`* — deliver fact
  A (event_id X) → clear outbox row → run reconcile (regenerates fact) →
  deliver → **expect sink `Duplicate` receipt, ledger row count == 1**.
  *Fails today* (second event accepted).
- **Acceptance assertion**: after regeneration, sink `QueryEvents` returns
  exactly 1 row for that occurrence and the second ingest returns
  `receipt.Duplicate == true`.

### T-5 High — aero-id silent-loss window untested (post-commit `_ =` discard)
- **Untested failure**: F-5 — 8 call sites discard `Record` errors after
  business commit (`service/account.go:155,184`, `callback.go:160,221,247`,
  `audit_hold.go:59,74`, `legacy_cutover.go:88` — Verified); a crash in the
  window = audit gap with no alert; draft's "written in the account
  transaction" is false for these paths.
- **Tests to add**: *`TestServiceAuditFailureIsObservable`* — mock
  `AuditLedgerRepository.Record` to fail; run account/callback ops; **expect
  the error to propagate or a failure metric/alert emitted — never `_ =`
  silence**. *Fails today* (errors discarded).
- **Acceptance assertion**: `ObserveAuditIngestFailure` counter increments (or
  op fails) when `Record` returns an error; no call site discards.

### T-6 High — Postgres-backed concurrency tests are environment-gated and absent from CI
- **Untested failure**: F-1/F-10 — the sink's single-snapshot-row store:
  concurrent-writer conflict behavior, `ErrSnapshotConflict` → producer retry
  churn, and vault's Postgres SKIP LOCKED lease recovery are **SKIPped in this
  environment** (all `internal/store/postgres_test.go`,
  `internal/outbox/postgres_test.go` tests and vault
  `TestPostgresAuditGovernanceConcurrentClaimsAndLeaseRecovery` — Verified
  SKIP output). No CI job sets `AUDIT_TEST_POSTGRES_DSN`/PG for these repos.
- **Tests to add**: *`TestSinkConcurrentIngestSingleWriter`* — 2 goroutines
  ingest same `(tenant, event_id)` with equal digest → **exactly one ledger
  row, one 202 + one Duplicate**; different digest → **one 202 + one 409**;
  run in CI with a disposable Postgres (`AUDIT_TEST_POSTGRES_DSN`).
- **Acceptance assertion**: no `ErrSnapshotConflict` escapes as 5xx in the
  equal-digest case; conflict rate under 2 concurrent writers is 0 for distinct
  rows (documented topology: exactly one active writer).

### T-7 Medium — Clock-skew contract untested and unimplemented
- **Untested failure**: F-7 — sink accepts arbitrary `occurred_at`
  (`models.go:374` requires presence only — Inspected); no >5min
  future-reject/past-annotate; retention cutoffs and legal-hold windows
  computed on untrusted producer clocks.
- **Tests to add**: *`TestIngestRejectsFutureSkew`* — `occurred_at = now+6min`
  → **expect 400/422 with skew code**; `now−6min` → **accepted + annotated
  field** (per the decision on draft §1.5). *Fails today* (accepted).
- **Acceptance assertion**: future-skew ingest returns a 4xx error code
  distinguishable from schema errors; past-skew events carry an annotation
  visible in `QueryEvents`.

### T-8 Medium — `/token` client_credentials HTTP surface has zero tests
- **Untested failure**: F-2/F-4/F-6/F-13 — no HTTP test for the token endpoint:
  success (claims/headers), `invalid_client`, unregistered scope, query-string
  credentials, wrong Content-Type, `Cache-Control: no-store`, empty
  `refresh_token`. The IdP's scope handling is entirely untested and the
  `audit:event:write` claim never exists in minted tokens.
- **Tests to add**: *`TestTokenEndpointConformance`* table:
  (a) valid post → **200, Bearer JWT with `kid` header + `iss` + `scope`**;
  (b) secret in query string → **400 `invalid_request`**;
  (c) non-form Content-Type → **400**; (d) unregistered scope → **400**;
  (e) response has `Cache-Control: no-store`, no `refresh_token` field.
  *Fails today on all five rows*: (a) claims missing (F-2), (b) query-string credentials are accepted (`r.FormValue` merges — F-6), (c) no Content-Type enforcement (F-6), (d) unregistered scopes accepted (no scope registry), (e) empty `refresh_token` and no cache headers (F-13).
- **Acceptance assertion**: all five rows pass or the metadata is trimmed to
  match the implemented surface (per F-4 decision).

### T-9 Medium — Introspection endpoint authentication untested (oracle)
- **Untested failure**: F-5 — `TestIntrospect_Missing*` covers params only; no
  test asserts the caller is authenticated (RFC 7662 §2.1); any caller knowing
  a `client_id` gets token-state oracle.
- **Tests to add**: *`TestIntrospectRequiresClientAuth`* — introspection with
  no credentials / wrong secret → **expect 401**; query-string params → **400**.
  *Fails today* (200 with token state).
- **Acceptance assertion**: `resp.StatusCode == 401` when no valid client
  credential is presented.

### T-10 Medium — aero-id 401/credential-rotation handling untested
- **Untested failure**: F-8 — aero-id's token source never invalidates on 401
  (vault does — `TestRuntimeCredentialRotation…` covers vault only); connector
  fault-injection covers 503/429/timeout/malformed but **not 401/403**;
  rotation can stall delivery ≤24h and dead-letter recoverable events (F-9 —
  replay tool absent, untested).
- **Tests to add**: *`TestConnectorInvalidatesTokenOn401`* — publish with
  revoked token → **expect next publish refreshes (singleflight) without
  waiting for TTL**; *`TestDeadLetterReplayTool`* — dead_letter row → replay
  command flips 3→0 and re-delivers. *Fails today* (no invalidation; no tool).
- **Acceptance assertion**: after a 401, the second publish attempt uses a
  fresh token (assert via token-source call count), and the replay command
  exists and moves the row back to pending.

### T-11 Medium — aero-im governance gap untested
- **Untested failure**: draft's aero-im section is entirely Proposed (no
  connector — Cited); its 30 audit tests are ignored without live PG; the
  "scope grant conditional on relay existing" has no test; no Rust test
  exercises outbox/relay (none exists).
- **Tests to add**: *`TestScopeGrantBlockedUntilRelayExists`* (after relay
  implementation) — provisioning a scope grant without a relay → **expect
  rejection with actionable error**; CI job with PG running the 30 ignored
  audit tests.
- **Acceptance assertion**: grant fails closed with a message naming the
  missing relay; `cargo test` audit group runs 37/37 with PG.

### T-12 Low — Sink read self-audit untested (and unimplemented)
- **Untested failure**: F-06 — `QueryEvents` records no `audit.event.read`;
  the console "cannot be bypassed" guarantee (draft §console) rests on
  nothing; `TestAdminActionSelfAudit` covers only mutations.
- **Tests to add**: *`TestQueryEmitsReadSelfAudit`* — query the ledger → **a
  `audit.event.read` row appears for the caller**. *Fails today*.
- **Acceptance assertion**: `QueryEvents` appends a self-audit row with actor
  and time bounds (once the P0 deliverable lands).

### T-13 Low — Benchmarks exist but no load/conformance suite; e2e not in CI
- **Untested failure**: F-1 capacity ceiling (O(ledger) snapshot RMW) has no
  measured envelope; `BenchmarkIngest`/`BenchmarkQueryLargeLedger` exist but
  nothing asserts thresholds; no chaos suite in any repo; sink e2e is manual
  docker and dev-token-only.
- **Tests to add**: *`LoadTestSnapshotCapacity`* — drive `BenchmarkIngest`
  to N=10k/100k events, assert per-event p50/p99 latency and snapshot size
  growth curve (record, don't fail, until the relational backend or explicit
  budget exists — F-01); *e2e smoke with production token* replaces the dev
  token in `fullstack.sh`.
- **Acceptance assertion**: benchmark run produces a recorded
  events/sec and p99 under the documented envelope (to be fixed by the P0a
  decision); e2e green with `AUDIT_ALLOW_DEV_AUTH=false`.

---

## 4. Prioritized scenario list

Order = risk × rollout dependency. Each row maps to the test above.

| # | Path | Scenario | Today's behavior (Verified/Inspected) | Gate |
|---|---|---|---|---|
| S1 | **Happy** | IdP `/token` (cc, `audit:event:write`) → relay POST → sink 202 ledgered → query returns event, dedup on redelivery | 403 (F-2); only dev-token path works | T-1.2 (release blocker) |
| S2 | **Happy** | vault delete op: legal-hold check + delete + audit + 2 facts in one tx → relay → sink | tx atomic (Inspected `HardDeleteObjectWithEvent`); relay flag-off by default | T-3 + flag-flip e2e |
| S3 | **Boundary** | Sink dedup: same event_id + same digest → 202 Duplicate; different digest → 409; 2^53 aggregate boundary | Covered (R6) | existing tests |
| S4 | **Boundary** | Clock skew: `occurred_at` +6min / −6min | Accepted (no check) | T-7 |
| S5 | **Error** | Sink 422 (unknown schema) → aero-id: 8 attempts → dead; vault: retry forever + Ready() red | F-3/F-10 | T-3 |
| S6 | **Error** | Token revoked at AS: vault refreshes on 401; aero-id stalls ≤24h | F-8 | T-10 |
| S7 | **Error** | Wrong scope provisioned (`audit:event:ingest` vs `write`) → 401/403 at sink → retry storm → DLQ | F-2 | T-1.2 + startup scope assert |
| S8 | **Race** | Two sink replicas ingest concurrently (shared snapshot row) | `ErrSnapshotConflict` → 5xx → producer retry churn | T-6 |
| S9 | **Race** | Two relay workers claim same outbox row (SKIP LOCKED/lease) | vault/sink covered in-memory; PG SKIP (F-10) | T-6 (PG CI) |
| S10 | **Recovery** | Crash between sink commit and producer ack → redelivery same event_id → sink Duplicate | aero-id/vault lease reclaim; vault regenerated-ID risk | T-4 |
| S11 | **Recovery** | Sink outage > aero-id maxAttempts (8) → dead_letter; no replay tool | F-9 | T-10.2 |
| S12 | **Recovery** | Vault gap-reconcile after binding flip: re-enqueued facts get new UUIDs → duplicate ledger events | F-4 | T-4 |
| S13 | **Security** | `dev:demo:compliance` token with flag on vs off | On by default — full read/export/legal-hold powers | T-1.1 |
| S14 | **Security** | Introspection without client auth; query-string secrets on `/token` | Oracle; secrets in logs | T-8, T-9 |
| S15 | **Recovery** | Restore/export approval workflow; integrity verify after restore | Covered (R14) | existing tests |

---

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

### CI gaps
1. **Sink has no CI test job** — `cli.py ci` = quality scans only; Go tests
   (`cli.py test`), race, bench, and e2e are all manual. **Highest-priority
   gap** for the platform's P0 component.
2. **Postgres-gated tests skip in every repo's CI** (sink store/outbox, vault
   integration). The concurrency invariants of the core stores are never
   exercised anywhere. Add a CI job with a disposable PG
   (`AUDIT_TEST_POSTGRES_DSN`, vault `test-integration`).
3. **No e2e in CI** — `fullstack.sh` is manual docker; no cross-repo suite
   exists (MULTI_REPO_OPERATIONS.md §1 is planned, not built).
4. **aero-vault CI runs no `-race`** (snaplink and aero-id do).
5. **No bench/load job** with recorded thresholds (F-01 capacity is
   unmeasured); no chaos suite anywhere (fault injection exists only as
   aero-id connector unit tests — extend to relay-level 5xx/partition storms).
6. **Conformance suite**: none (OIDF/OAuth conformance not claimed — aligns
   with protocol review §4; discovery sweep T-2 is the minimal stand-in).

### Flake risks (inspected)
- Sink `fakeStore.ListPending` uses `time.Now()` against `NextAttemptAt`
  (relay_test.go) — records are created −1min so stable; if a test ever
  creates `NextAttemptAt: time.Now()` exactly, it becomes flaky. Keep the
  `fixedRelay.Now` pattern for all new time-dependent relay tests.
- Sink Postgres tests share one snapshot row and **must not** call
  `t.Parallel` (documented in-file); a future added test doing so = CI flake —
  enforce with a linter comment or per-test row isolation.
- aero-vault SQLite single-connection serialization: tests are sequential by
  design; adding `t.Parallel` to repository tests risks SQLITE_BUSY flakes.
- aero-im: 30 ignored tests need live PG — a CI job that flips them on must
  apply migrations through 0223 first (documented in test names).
- `TestHTTPIngestLargeAggregateVersionConflictNotDuplicate` depends on JSON
  number precision (2^53) — stable, but keep the boundary comment; any switch
  of JSON decoder breaks it silently.

### Fixtures needed
1. **Disposable Postgres harness** (sink + vault): migration-applied DB per
   run; env-gated (pattern exists: `AUDIT_TEST_POSTGRES_DSN`,
   `newPostgresTestDB`). Reuse for T-6 and to un-skip the 30 aero-im tests.
2. **IdP token fixture generator** (cross-repo): a `scripts/mint-token.sh`
   that POSTs snaplink `/token` and emits the JWT for e2e use — replaces the
   `dev:` tokens in `fullstack.sh` (T-1.2).
3. **Sink conformance harness**: discovery sweep + endpoint matrix (T-2)
   parameterized by the metadata JSON, so metadata edits and route edits
   cannot drift again.
4. **Chaos fixture**: relay-level sink-down/5xx/429 injection (httptest
   already used; add a `chaos` build tag or env flag to keep it out of default
   CI) for S5/S6/S11.

### Exit criteria (before any producer cutover — P1/P3 per design)
1. **Green**: T-1.1 (dev-token reject), T-1.2 (production-token e2e), T-3
   (vault dead state + Ready), T-4 (regenerated-fact collapse), T-6 (PG
   concurrent ingest in CI), T-7 (skew policy decided and enforced), T-10
   (aero-id 401 invalidation + replay tool).
2. **No skips**: sink store/outbox PG tests and vault integration run in CI,
   not skip.
3. **Sink CI job added** running `cli.py test` + `-race` + bench-record.
4. **Coverage report** committed per repo (sink lacks any coverage target
   evidence; aero-vault/snaplink collect profiles in CI — add thresholds once
   T-series gates are green).
5. **Documented capacity envelope** from `BenchmarkIngest` (F-01) or the
   relational backend implemented — load numbers are an exit criterion, not a
   review-day unknown.
6. **e2e `fullstack.sh`** green with `AUDIT_ALLOW_DEV_AUTH=false` and an
   IdP-minted token; dev-token usage restricted to a dedicated test fixture.

---

## Bottom line

The **executable test base is strong where it matters most today** — sink
digest idempotency, verifier hardening, integrity/hash-chain, tenant
isolation, producer HTTP hygiene, and relay backoff are covered by 4160
passing tests across the five codebases (157 + 926 + 1519 + 1558, plus 7
aero-im audit tests) at this revision, with race clean on the
concurrency-critical packages. But **the platform's central claims are
untested exactly where they are broken**: the production trust path (T-1,
release blocker), discovery truthfulness (T-2), vault DLQ/readiness (T-3),
vault idempotency (T-4), aero-id loss window (T-5), and Postgres concurrency
(T-6, skipped in every CI). Until T-1–T-6 land as failing-then-green tests,
the design's rollout order (§2 of the draft) cannot be trusted to survive
first contact with production — the suites today certify a pipeline that only
works on forged tokens.

*Advisory only. No files modified. All commands executed at the local
checkouts at review time; re-run after any schema/relay/token change.*
