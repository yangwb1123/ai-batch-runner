# Security Review v2 — Audit Governance Platform (adversarial production behavior)

**Role**: principal security engineer (adversarial review)
**Date**: 2026-08-06
**Scope**: v2 contract docs (`docs/snaplink-platform/v2/` — ingestion design, scope matrix, event registry, batch plan) verified against executable code at pinned HEADs:

| Repo | Path | HEAD |
|---|---|---|
| IdP (deployed, SDK tree) | `/home/u1/workspace/demo/snaplink` (module `yangwb1123/snaplink`) | `b3c839bb` |
| IdP (second tree) | `/home/u1/snaplink` (module `github.com/opensso/sso`) | `ac5d367` |
| Sink | `/home/u1/workspace/demo/snaplink-audit-governance` | `fb4fd97` |
| aero-id | `/home/u1/aero-id` | `756b65b` |
| aero-vault | `/home/u1/aero-vault` | `8351376` |
| aero-im | `/home/u1/aero-im` | `5feaf78` |
| console | `/home/u1/workspace/demo/snaplink-console` | `ab204c4` |

**Method**: `prompts/README.md` evidence standard. All claims Verified against code at cited paths unless marked. Tests executed for this review: none new — I re-verified by code inspection and grep across all 7 trees; the protocol_expert's executed suites (`go test ./cmd/server/ -run TestOIDCDiscovery` — legacy tree; `go test ./internal/auth/` — sink) are cited where relevant. No files modified (advisory only).

**Topline**: every v1 security finding (S1–S15) is **still open at v2 HEADs** — zero of the S-series items have been implemented. Two corrections to the record are security-relevant: (1) the **deployed IdP is the SDK tree** (`yangwb1123/snaplink`), not the tree the protocol_expert reviewed — several "Critical/High" protocol findings (V2-1 no-kid, V2-3 broken discovery, V2-4 unauth'd introspection, DS-06 non-atomic rotation) apply to the **non-deployed** `opensso/sso` tree; (2) the **v2 batch plan B4 rows mix both trees** — three of five rows name files that do not exist in the deployed tree, and row B4-1's "header 补 kid" is already done in the deployed tree. New findings in this review: unauthenticated JWKS-fetch amplification (SEC-V2-10), aero-id dead-letter purge before replay tool (SEC-V2-12), scope-to-platform escalation coupling (SEC-V2-13), and the plan's F5 scope understatement (SEC-V2-7).

---

## 1. Assets, trust boundaries, attacker capabilities, entry points

### 1.1 Assets (ranked)

| Asset | Location | Notes |
|---|---|---|
| Audit ledger state (events, receipts, streams, segments, checkpoints, tenant/source/schema/policy/legal-hold config) | sink `internal/store` whole-snapshot (file or single PG row) | Confidentiality + integrity core; tamper-evidence is the product |
| Encrypted export blobs + WORM archive | sink archive (file/S3 Object Lock) | AES-GCM sealed |
| Producer outboxes + local hash-chain ledgers | aero-id `audit_publications`/`audit_ledger`; aero-vault `audit_governance_outbox`/`audit_events`; IdP `audit` store (PG, hash_chain in deployed config) | Durability of the pipeline |
| Credential material | producer config/env (HMAC keys, binding secrets, vault static L2 bearer tokens, IdP client secrets) | Long-lived, mostly rotation-less |
| Sink keys | `AUDIT_SIGNING_SECRET`, `AUDIT_ENCRYPTION_KEY`, JWKS trust config | Key material |
| IdP signing keys + access tokens | deployed IdP (`snaplink/sso-server:sv3` built from SDK tree) Ed25519/ES/RS issuers | Root of auth chain; tokens carry scopes that become sink permissions verbatim |
| **Second, non-deployed IdP tree** | `/home/u1/snaplink` (opensso/sso) | Contains weaker issuer/discovery/introspection; not evidenced as deployed, but present and batch-plan-referenced (SEC-V2-11) |

### 1.2 Trust boundaries

1. **Edge → sink API** (HTTP `:8089`, gRPC optional): bearer boundary. The only cryptographic boundary is the JWKS verifier; `dev:` tokens cross it with no signature while `AUDIT_ALLOW_DEV_AUTH=true` (SEC-V2-1).
2. **Producer relay → IdP `/token` (client_credentials)**: deployed IdP authenticates clients, validates scope against client allowlist (RFC 6749 §3.3, `invalid_scope` → 400), mints RFC 9068-shaped tokens with `kid`/`jti`/`scope`/`client_id`/`iss` — **but no `tenant_id`/`roles`** (SEC-V2-2).
3. **Producer relay → sink ingest**: per-tenant source binding (`AllowsClient`, fail-closed); `client_id` is the principal; envelope `tenant_id` **silently overwritten** on mismatch (SEC-V2-8).
4. **Sink → IdP JWKS**: outbound HTTPS fetch, **uncached, triggered before signature verification** (SEC-V2-10).
5. **Sink → archive/Vault/OTLP**: config-only URLs, no request-driven SSRF surface (verified).
6. **Console (browser) → ledger**: none today — localStorage ring only (SEC-V2-9).
7. **IdP discovery/introspection**: deployed IdP requires client auth on introspection (RFC 7662, 401 invalid_client, oracle-collapsed); the non-deployed tree's `/introspect` accepts any `client_id` with no credential check (SEC-V2-11).

### 1.3 Attacker capabilities

- **A0 unauthenticated remote**: exploitable today via dev-auth default (SEC-V2-1); can also force sink→IdP JWKS fetches with crafted tokens (SEC-V2-10).
- **A1 tenant auditor/service** (legit token): query cost, export exhaustion (SEC-V2-10).
- **A2 write-scoped service**: stream pollution, `occurred_at` forgery, envelope tenant relabeling (SEC-V2-8/14).
- **A3 mis-provisioned platform client**: `audit:platform:cross_tenant` scope or `platform-admin` role = full platform powers at the sink (SEC-V2-13).
- **A4 network position**: plaintext gRPC when enabled (SEC-V2-12).
- **A5 insider/operator**: out of scope except credential handling (SEC-V2-15).

### 1.4 Entry points

Sink HTTP: `POST /api/v1/events` (+`/events:batch`), `/exports`, `/legal-holds`, `/restores…`, `/integrity/verify`, `/tenants`, `/sources`, `/schemas`, `/policies/retention`, `/admin/actions`; `GET /events…`, `/operations…`, `/aggregates…`, `/exports/{id}/download`, `/metrics`, `/healthz`, `/readyz` (last three unauthenticated). Sink gRPC `Write`/`WriteBatch`/`WriteStream` (same authenticator, plaintext). Producers: outbound relay HTTP. Deployed IdP: OAuth/OIDC surface incl. `/token`, `/token/introspect`, discovery, JWKS. Non-deployed tree: same-named surface with weaker behavior (SEC-V2-11).

---

## 2. Findings (sorted by severity)

### SEC-V2-1 — CRITICAL: Dev-token auth still defaults ON; a default deployment has no cryptographic trust source (S1 re-verified)

**Evidence (Verified)**
- `cmd/audit-api/main.go:45`: `allowDev := flag.Bool("allow-dev-auth", boolEnv("AUDIT_ALLOW_DEV_AUTH", true), …)` — default still `true` at HEAD `fb4fd97`.
- `internal/auth/auth.go` `AuthenticateTokenContext`: `if a.AllowDev && strings.HasPrefix(token, "dev:") { return parseDevToken(token) }` — unsigned, stateless; `dev:platform:platform-admin` → `Platform=true` + every permission (`permissionsForRoles`).
- `verifier.go:30` `ValidateConfiguration`: `if !hasSecret && !a.AllowDev { return … }` — dev-auth alone still satisfies config; a default boot has **no trust source**.
- `deploy/docker-compose.verify.yml:12` still sets `AUDIT_ALLOW_DEV_AUTH: "true"`.
- `test/e2e/fullstack.sh` still exercises only `dev:demo:tenant-auditor` / `dev:demo:compliance` tokens — the only end-to-end path in the tree is the forgeable one.

**Exploit (no credentials, no network position)**: `curl -H 'Authorization: Bearer dev:platform:platform-admin' https://<audit>/api/v1/events?tenant_id=<victim>` → full cross-tenant read; same token drives exports, legal holds, restores, retention, schema registration.

**Impact**: total confidentiality/integrity loss of the tamper-evident store; release blocker. G1 (T-1.1) cannot pass.

**Remediation**: invert default to `false`; `ValidateConfiguration` must refuse `AllowDev=true` as the only trust source; CI manifest scan failing on any production manifest with `true`; keep a `dev:`-only profile behind an explicit flag for local runs.

**Regression test**: (a) `ValidateConfiguration` fails `Authenticator{AllowDev:true}` alone; (b) `dev:platform:platform-admin` → 401 on every route with `AllowDev=false`; (c) manifest scan over `deploy/` fails on `AUDIT_ALLOW_DEV_AUTH: "true"`.

### SEC-V2-2 — CRITICAL: Deployed IdP mints no `tenant_id`/`roles`; every real-token read/export/legal-hold 403s; T-1.2 blocked (S2 re-verified + repo correction)

**Evidence (Verified)**
- Deployed IdP = SDK tree: `infrastructure/defaultimpl/issue_payload.go` `buildAccessPayload` mints `{iss,sub,exp,nbf,iat,scope,extra,client_id,jti,acr,sid,serving_region}` — no `tenant_id`, no `roles`. `shared/core/types_token.go` `Subject` has no such fields. All three issuers (ed25519/ecdsa/rsa `issue.go`) share it. Header **already contains `kid`** (`ed25519_issue.go:29` `header := ed25519Header{Alg: EdDSA, Typ: jwtTypAT, Kid: kid}`).
- Sink `parseJWT` (`internal/auth/auth.go`): `claims.TenantID = stringClaim(payload, "tenant_id")` else `"tenant"` → always empty for real IdP tokens; `roles` empty → `permissionsForRoles` empty; only scope-derived `audit:event:write` survives.
- Sink `require()` (`internal/httpapi/server.go:845-852`): every permission ≠ `audit:event:write` with empty `TenantID` and not platform → 401. Reads/exports/legal-holds/restores are **unreachable with a real token**.
- Writes work only when the client is bound to exactly one active source across all tenants (`resolveIngestTenantFromData`, `internal/service/sources.go:101`).

**Impact**: the production pipeline works only on forged dev tokens today; every real-token workflow breaks at first read. G1/G5 (T-8(a), T-1.2) cannot pass.

**Remediation** (per v2 contract §1.7/S2): add `tenant_id` (client-binding resolved at mint) + `roles` to `buildAccessPayload`/`Subject`; provision a scope registry (SEC-V2-13); align the sink to reject tenant-scoped tokens lacking `tenant_id` (contract says REQUIRED; sink today only 401s on read paths, write path works without it).

**Regression test**: e2e — client_credentials token (`resource=audit-governance`) → sink `POST /events` 202 + `GET /events` 200 with tenant context + `POST /exports` 202, with `AUDIT_ALLOW_DEV_AUTH=false`.

### SEC-V2-3 — CRITICAL (planning): Batch plan B4 rows target the wrong repo for 3 of 5 rows; "补 kid" is already done; implementers will edit dead code (confirms V2-5/F-13 with deployment evidence)

**Evidence (Verified)**
- Deployed IdP pinned by deployment assets: `/home/u1/sv-sso-deploy-k8s-20-sso-configmap.yaml` (namespace `sv-sso`, image `snaplink/sso-server:sv3`, `setup_wizard.enabled`, `bootstrap.disabled`, `identity.backend postgres`, `audit: {enabled: true, backend: postgres, hash_chain: true}`) matches **exactly** the SDK tree's `ops/deploy/k8s-distributed/config.yaml` structure; `setup_wizard` exists only in the SDK tree (`cmd/sso-server/build_app_cluster.go:283`); the SDK tree Dockerfile tags `snaplink/sso-server`.
- Batch plan rows B4-1..5 name: `infrastructure/defaultimpl/issue_payload.go` (SDK tree ✅ exists), `token/jwt/generator.go:52-55` (**legacy tree only** — `find` over SDK tree: no match), `sso.go:894-960` (**legacy tree only**), `cmd/server/routes.go:63-77,663-684` + `routes_test.go:41` (**legacy tree only**).
- The legacy tree's `token/jwt/generator.go` `Generate` stamps header `{alg,typ}` with **no kid** and claims `{sub,aud,iat,exp,uid}`; the SDK tree already stamps kid (SEC-V2-2 evidence). "Header 补 kid" is therefore already true in the deployed tree.

**Impact**: executing B4 rows as written either ships nothing (edits to non-deployed files) or, worse, an operator "fixes" the legacy tree and deploys it as the platform IdP — regressing to a no-kid issuer that the sink's JWKS verifier rejects (`verifier.go:167` `remote JWKS token requires kid`), breaking the entire trust path. The two-tree ambiguity is itself a release risk until one tree is declared and the other archived.

**Remediation**: re-pin all B4 rows to the deployed tree with verified paths; add an explicit deployment-topology declaration to the v2 docs (which repo builds `snaplink/sso-server:sv3`); decide the legacy tree's fate (archive or explicitly support).

**Regression test**: CI check that every path named in `implementation-batches.md` exists in the repo the row is assigned to (a path-existence gate); e2e minted against the deployed tree only.

### SEC-V2-4 — HIGH: Sink 401/500 bodies are an oracle; auth failures leak trust topology (S4 re-verified)

**Evidence (Verified)**
- `internal/httpapi/server.go:845`: `require()` wraps: `fmt.Errorf("%w: %v", domain.ErrUnauthorized, err)` — verifier errors pass verbatim into `errorBody` (line 934: `message := err.Error()`, only `"internal server error"` string is masked).
- Distinguishable 401s: expired / not-active / issuer-mismatch / audience-mismatch / `client_id`-mismatch / `fetch JWKS: HTTP 500` (reveals JWKS host reachability) / `parse JWKS` errors.
- 500s leak `err.Error()` from store, archive/S3, and `VaultTransitSigner` paths (`vaultsigner.go` embeds `body=%s` from Vault).

**Impact**: targeted recon of trust topology (JWKS host, Vault address, storage paths); violates the platform's own oracle-safe discipline (AGENTS.md §3).

**Remediation**: single opaque `401 invalid_token` for all auth failures (detail to logs); generic 500 body `{"error":{"code":"internal_error","request_id":…}}`.

**Regression test**: byte-identical 401 bodies for expired/issuer-mismatch/signature-failure; 500 bodies contain no path/Vault/S3/bucket substrings.

### SEC-V2-5 — HIGH: Sink store conflict → bare 500, no retry; no fsync; `/readyz` doesn't probe the store (F-01 re-verified)

**Evidence (Verified)**
- `internal/store/postgres.go:101,114` `ErrSnapshotConflict` on optimistic-lock failure; no service-layer retry (grep: zero references outside `store/` and its tests) → `statusForError` default 500.
- `internal/store/store.go:258-263` `fileBackend.Save` = `os.WriteFile(tmp)` + `os.Rename` — no `Sync()`/dir-sync; crash can silently revert the last snapshot.
- `internal/httpapi/server.go:161-175` `readyz` probes **archive only** — PG dead still reports ready (T-6 red today).

**Impact**: multi-replica/concurrent writers lose writes with opaque 500s; power loss can revert ledger state; orchestrators see "ready" while the store is down — availability + integrity of the evidence store.

**Remediation**: bounded jittered retry on `ErrSnapshotConflict` (or explicit single-writer claim), `statusForError` mapping to 409-style domain error, fsync + dir sync, store probe in `/readyz`.

**Regression test**: T-6 (two concurrent ingests, same digest → one row + one 202 + one Duplicate, no 500); stop PG → `/readyz` 503; crash-after-rename test for the file backend.

### SEC-V2-6 — HIGH: Vault relay retries permanent failures forever; one poison row flips `Ready()` 503 (S3 re-verified)

**Evidence (Verified)**
- `internal/auditgovernance/relay.go:97-110` `retryFact`: every error class → `RetryAuditGovernance` with `boundedBackoff` (cap 300s) — no terminal state, no dead column (repository has `delivered_at_ns` only).
- `internal/auditgovernance/http.go` `validateReceipt`: any non-202 (incl. sink 409/422/403) → `httpStatusError` → retried forever.
- `internal/auditgovernance/runtime.go:145-161` `Ready()`: `OldestPendingAuditGovernance` > `maxLag` (900s) → 503 — one poison row takes the file platform's readiness down.
- Zero metric calls in the whole package (`grep -c Observe|Inc|Gauge` = 0 in all files) — H6 confirmed.

**Impact**: schema/deploy mismatch bricks tenant relay + readiness → restart loops; permanent-class failures never surface to operators as dead rows.

**Remediation**: per v2 §1.4 — permanent classes (422/409/tenant mismatch/invalid receipt) → dead immediately; transient bounded retry cap 300s; `Ready()` decoupled (degraded + alert at maxLag×0.5, never 503); relay metrics.

**Regression test**: T-3 (422 → dead within one cycle; `Ready()` with a dead row = true); D1 drill (sink down 60min → vault `/readyz` 200 + 450s alert).

### SEC-V2-7 — HIGH: aero-id silent audit drops — 8 governance sites + a wider `_ =` surface; dual delivery with swallowed inline-publish error (F5/F6 re-verified; plan scope understated)

**Evidence (Verified)**
- The 8 governance sites exist as listed: `service/account.go:155,184`, `callback.go:160,221,247`, `audit_hold.go:59,74`, `legacy_cutover.go:88` — all `_ = s.audit.Record(...)`.
- **Wider surface (not in the plan)**: 49 total `_ =` lines touching audit in `internal/`+`service/`+`cmd/` (grep at HEAD `756b65b`), e.g. `service/ban.go` ×2, `service/admin.go` ×2, `service/block.go` ×2, `internal/comment/service.go:431`, `internal/middleware/idempotency.go:45`, `internal/file/service_share.go` ×2, `internal/block/block.go` ×2. Most are `audit.Log` (generic event channel) vs `Record` (governance fact) — both silently dropped on failure.
- `service/audit_ledger.go:77-79`: `Record` inline-publishes `_ = publisher.Publish(ctx, fact)` **and** the durable outbox worker delivers the same fact → dual delivery (F6); the inline path swallows errors and adds sink latency to the request path.

**Impact**: audit events silently vanish under transient failure; duplicates flow from two paths with different IDs; the plan's T-5 grep gate (`无 _ = 调用点`) will fail against 49 sites, not 8 — scope the F5 batch accordingly.

**Remediation**: move Record/Log sites in-tx (`AppendInTx`) or make failure observable (`ObserveAuditIngestFailure` + alert, never `_ =`); delete inline publish (outbox worker is the only path).

**Regression test**: T-5 (mock Record failure → counter increments or operation fails; grep gate across all `_ =` audit sites, not just the 8).

### SEC-V2-8 — MEDIUM: Sink silently overwrites envelope `tenant_id` instead of 422 (DS-08 re-verified; contract fixes it, code doesn't)

**Evidence (Verified)**
- `internal/service/service.go:389`: `event.TenantID = tenantID` — the token/URL-resolved tenant **replaces** the envelope's tenant field with no comparison. A producer sending `tenant_id: tenant-b` in the envelope while authenticated for tenant-a gets a silently relabeled record (when source binding allows).
- v2 contract (ingestion-design sink §"Ingest API contract") now mandates mismatch → 422 — contract correct, implementation pending.

**Impact**: tamper-evidence gap — the envelope claims a tenant the ledger doesn't record; misrouted events become evidence against the wrong tenant.

**Remediation**: compare envelope `tenant_id` to the resolved token tenant; mismatch → 422 (producer marks dead per §1.4).

**Regression test**: ingest with envelope tenant ≠ token tenant → 422 and no row; matching → 202.

### SEC-V2-9 — MEDIUM: Read-path self-audit absent; console log remains forgeable localStorage (S9 re-verified)

**Evidence (Verified)**
- No `audit.event.read`/`audit.event.export` writer anywhere in the sink (grep over `internal/` — only permission strings in `auth.go`).
- `snaplink-console/lib/services/audit_log_service.dart`: localStorage 1000-ring (`sso_audit_log` key), no sink read API calls (console HTTP surface: `lib/api/portal_api.dart`, `snaplink_admin_api.dart` — no audit reads). Batch plan B6-1's path `lib/services/portal_api.dart` is wrong (actual `lib/api/portal_api.dart`) — minor plan path error.

**Impact**: "who read what" is untraceable; console-side records are worthless as evidence (devtools forge/clear).

**Remediation**: sink `QueryEvents`/`getEvent`/`downloadExport` append self-audit in the same store update (T-12); console displays server-side records only.

**Regression test**: T-12 (query → caller's `audit.event.read` row appears; direct API cannot bypass).

### SEC-V2-10 — MEDIUM-HIGH: **New** — unauthenticated JWKS-fetch amplification; no query/export cost caps (S5 re-verified + elevation)

**Evidence (Verified)**
- `internal/auth/verifier.go` `verifyJWT` (line 59): `parseVerifiedHeader` → `verificationKey` → `remoteVerificationKey` → `fetchJWKS` **runs before signature verification** (`jws.Verify` is last). Any unauthenticated caller can send a compact 3-segment token with `alg=EdDSA` + any `kid` and force a sink→IdP HTTPS fetch per request. No rate limiter exists on any route (grep: zero limiter references in `internal/httpapi/`, `cmd/audit-api/`).
- No JWKS cache (each verification refetches; `fetchJWKS` line 270).
- `QueryEvents`/`OperationTimeline`/`AggregateTimeline`/`EvaluateRetention`/`runExport` scan the whole snapshot (page-cap 1000, O(n)); `runExport` spawns an unbounded goroutine per job; export re-encrypts per job.

**Impact**: A0 can turn the IdP's JWKS endpoint into a DDoS target through the sink (amplifier), and any auditor can drive CPU/memory exhaustion + starve the single-writer store with concurrent exports.

**Remediation**: cache JWKS keyed by kid with short TTL (revalidate on unknown kid); rate-limit the auth path; per-tenant query cost caps (time-range width, scan budget) and bounded export concurrency.

**Regression test**: httptest counting JWKS fetches — N verifications with same kid → ≤1 fetch; crafted-token flood → 429 before any outbound fetch.

### SEC-V2-11 — MEDIUM (deployment hazard): Non-deployed IdP tree still contains no-kid minting, broken discovery (codified by a passing test), unauthenticated introspection, non-atomic refresh rotation

**Evidence (Verified, legacy tree `/home/u1/snaplink` HEAD `ac5d367`)**
- `token/jwt/generator.go:52-55` `Generate`: header `{alg,typ}` — **no kid**; `Claims` `{sub,aud,iat,exp,uid}` — no iss/scope/jti. Used by `sso.go:935` `ClientCredentials` (wired at `cmd/server/routes.go:257` `POST /token`) and `app.go:459`, `sso.go:1018,1217`.
- `cmd/server/routes.go` `oidcDiscovery`: `token_endpoint = base + "/authenticate"` (real route is `/token`), `authorization_endpoint=/login` with `response_types_supported:["code"]` (no `/authorize`), `revocation_endpoint=/revoke` (**not wired anywhere**), issuer derived from `r.Host` when unset. `routes_test.go:497` `TestOIDCDiscoveryEndpoint` **asserts the port-bug string** `http://localhost:8080:0/login` — the defect is codified by a passing test (V2-3; protocol_expert executed it).
- `cmd/server/routes.go:174-188` `introspect`: `token` + `client_id` form values, **no client credential check** → unauthenticated token oracle (V2-4).
- `sso.go:972` `RefreshWithRotation`: Get → best-effort Delete (error swallowed) → Create — non-atomic; concurrent replay splits the family; no reuse detection (DS-06). Contrast the deployed tree's atomic GETDEL + reuse-detection family kill (`infrastructure/redis/refresh_token.go`, OAuth BCP §4.13/4.14).
- No deployment evidence found for this tree (deployed config matches the SDK tree, SEC-V2-3); its deployment status is **Unknown** — must be confirmed by operators.

**Impact**: if this tree is ever deployed as the platform IdP (or pointed at by producer relays), the sink rejects its tokens (no kid), discovery breaks OIDC clients, introspection leaks token state, and refresh families are replay-splittable. The batch plan currently tells implementers to edit this tree.

**Remediation**: declare one IdP; archive or explicitly support the other; if kept, port the SDK tree's issuer/introspection/rotation behavior. Do not run B4 rows against this tree.

**Regression test**: discovery sweep (every advertised endpoint → 2xx/405, never 404); introspection without client secret → 401; rotation concurrency test asserting single-family invariant.

### SEC-V2-12 — MEDIUM: **New** — aero-id dead-letter purge runs before any replay tool exists; dead letters silently deleted (T-10.2 partially open)

**Evidence (Verified)**
- `internal/outbox/repository.go:188-208`: scheduled purge loop deletes `dead_letter=true` rows older than `deadLetterRetention` and published rows older than `publishedRetention` — logs only, no alert, no replay-before-purge workflow. The plan's B2-5 (replay tool) is not yet implemented.

**Impact**: permanent failures (e.g. sink 422 classes) are deleted after retention with no recovery path; evidence of the failure is lost.

**Remediation**: pause purge until replay tool + DLQ alert are live (plan says "TTL purge 暂停至告警确认" — make it a hard gate); alert on dead-letter count > 0.

**Regression test**: enqueue a dead row, run purge, assert row survives until explicitly replayed or alert-acknowledged.

### SEC-V2-13 — MEDIUM: **New** — scope/role → platform escalation coupling; `audit:platform:cross_tenant` is one mis-grant away from full platform powers

**Evidence (Verified)**
- Sink `parseJWT`: `claims.Platform = claims.Permissions["audit:platform:cross_tenant"] || contains(claims.Roles, "platform-admin")`; `Allows()` = permission OR platform. Scope strings from the token become permissions verbatim (`auth.go:160-163`).
- The deployed IdP has no platform-scope registry yet (B4-2); its per-client scope allowlist (`protocols/oauth/oauthvalidate/scope.go` — `invalid_scope` 400) is the only gate.
- `roles` claim and sink `permissionsForRoles` (`auth.go:166-205`) are two independently-maintained mappings that must never drift (e.g. `tenant-admin` grants `audit:policy:write`; `compliance` grants export+legal-hold).

**Impact**: a single mis-provisioned client (scope `audit:platform:cross_tenant` or role `platform-admin`) gets cross-tenant read + control-plane write at the sink — tenant isolation collapses without any code defect.

**Remediation**: the scope registry (B4-2) must treat `audit:platform:cross_tenant`/`platform-admin` as deny-by-default, single-client grants with review; add a contract-level statement (scope-matrix deny rules already exist — extend to the claim contract); regression-test the registry against the sink role map (grep-level consistency check per F2's precedent).

**Regression test**: provision a client with only `audit:event:read`; assert sink `Platform=false`, cross-tenant read → 403; attempt to register `audit:platform:cross_tenant` for a non-platform client → registry refuses.

### SEC-V2-14 — MEDIUM: `occurred_at` skew and stream integrity still unchecked (S8 re-verified; contract still `[PROPOSED]`)

**Evidence (Verified)**
- `internal/service/service.go` `Ingest`: no skew bound vs `ReceivedAt` (v1 harness demonstrated 1h skew accepted); no `StreamID` validation against operation/aggregate (client-supplied verbatim, `domain/models.go` `Event.Stream()`).
- v2 contract §1.5 lists skew rejection as `[PROPOSED]` — not normative; no implementation.

**Impact**: write-scoped producers can pollute hash chains and game retention/legal-hold time windows.

**Remediation**: server-derived stream IDs; normative skew window (reject >5min future, annotate past) with fail-closed default.

**Regression test**: cross-stream append rejected; 6-min-future `occurred_at` → 422.

### SEC-V2-15 — LOW/MEDIUM: Long-lived static credentials + per-instance quota (S6/S14 re-verified)

**Evidence (Verified)**
- aero-vault `internal/events/audit_sink_l2.go`: second egress with **static per-tenant bearer tokens**, no rotation path; mitigated to loopback HTTP (`auditSinkL2Loopback`) — still unredacted payload (bucket/key/metadata) to a second destination.
- Sink quota `internal/service/service.go:746-766`: in-process map; `EventsPerSecond <= 0` (or negative, via `createTenant`) **disables** quota; multi-replica bypass.

**Remediation**: sunset L2 or short-lived rotated tokens; validate `EventsPerSecond >= 1` at tenant creation; shared quota backend for replicas.

**Regression test**: tenant creation with negative EPS refused; two-replica test shows shared budget.

### SEC-V2-16 — LOW: IdP stock audit backend defaults to memory (S10 re-verified; mitigated in the specific deployment)

**Evidence (Verified)**
- Deployed tree `cmd/sso-server/serverbuildauthn/build_audit_secrets.go:34-58`: `case "", "memory"` → `audit.NewMemorySink` — stock default volatile.
- **Mitigation in the actual deployment**: k8s configmap `audit: {enabled: true, backend: postgres, hash_chain: true}` — the deployed IdP uses PG + hash chain. The stock default remains a trap for other deployments.

**Remediation**: make the default fail-closed (require explicit backend when audit enabled).

**Regression test**: boot with `audit.enabled=true` and no backend → error.

---

## 3. Abuse-case table

| # | Abuse case | Attack path | Status today | Mitigating control | Residual |
|---|---|---|---|---|---|
| A | **Identity spoofing** | Forge `dev:<tenant>:<role>` header | **Exploitable** (SEC-V2-1) | none (no signature) | Critical until default inverted |
| B | **Identity spoofing** | Forge JWT via legacy no-kid mint or XOR "HMAC" issuer (`defaulttoken`) | Latent (SEC-V2-11; S11) | sink verifier requires kid for JWKS; stock issuer is Ed25519 | Latent — legacy tree deploy would surface it |
| C | **Token replay** | Reuse captured access token | Bearer exposure; no jti-replay store at sink | exp/nbf checks; IdP jti (no sink-side store) | Replay within TTL (documented bearer tradeoff) |
| D | **Event replay** | Redeliver same event | Absorbed: digest dedup, 202 Duplicate | `(source,tenant,event_id)` + digest | Random UUIDs limit dedup to same-row retries (F4 pending) |
| E | **Cross-tenant read** | `tenant_id` param with non-platform token | **No leak** — `tenantFor` ignores param for non-platform | code path | Silent ignore is confusing, not a leak |
| F | **Cross-tenant read** | `dev:platform:platform-admin` + `tenant_id=<victim>` | **Exploitable** (SEC-V2-1) | none | Critical |
| G | **Cross-tenant read** | Mis-granted `audit:platform:cross_tenant` scope / `platform-admin` role | Not reachable today (no registry/roles minted) | none yet | **Becomes reachable when B4-2 lands** (SEC-V2-13) — registry must deny by default |
| H | **Proxy/header forgery** | XFF/Host on sink | Sink ignores all proxy headers; principal server-derived | verified | none |
| I | **Proxy/header forgery** | X-Forwarded-* on IdP | Honored only behind TrustedProxies CIDR gating (`interfaces/middleware/request_url.go`); issuer configured (not Host-derived) in deployed config | verified | Depends on trusted-proxy config being correct |
| J | **Resource exhaustion** | Crafted token flood → JWKS fetch amplification | **Exploitable unauthenticated** (SEC-V2-10) | 5s timeout, size caps | High — no limiter, no cache |
| K | **Resource exhaustion** | Query/export O(ledger), no rate limits | Exploitable by any auditor (SEC-V2-10) | page_size cap only | Medium-high |
| L | **Resource exhaustion** | Batch ingest | Body cap 512KiB; per-tenant in-process quota | quota | Per-process only; disable-by-config (SEC-V2-15) |
| M | **Sensitive-data leakage** | 401/500 error bodies (trust topology, Vault/S3 paths) | Exploitable (SEC-V2-4) | no-store on 5xx only | High |
| N | **Sensitive-data leakage** | Unredacted L2 egress (bucket/key/metadata/actor) | By design (SEC-V2-15) | loopback-only HTTP, static tokens | Medium — long-lived creds |
| O | **Sensitive-data leakage** | Console log forgery/clearing | Trivially exploitable (SEC-V2-9) | none | Medium — console records worthless |
| P | **Integrity** | Stream pollution via client StreamID | Exploitable by write-scoped actor (SEC-V2-14) | none | Medium |
| Q | **Integrity** | `occurred_at` forgery → retention/legal-hold gaming | Exploitable (SEC-V2-14) | none (skew contract `[PROPOSED]`) | Medium |
| R | **Integrity** | Envelope tenant relabeling (silent overwrite) | Exploitable (SEC-V2-8) | none (contract mandates 422, unimplemented) | Medium |
| S | **Integrity** | Event-id squatting under deterministic IDs | Not reachable with random UUIDs; becomes reachable if §1.9 adopted without care | claim-then-verify digest (409) at sink | Low-Medium (contract has backstop) |
| T | **Availability** | Poison row → vault `Ready()` 503 → restart loop | Exploitable via deploy bug (SEC-V2-6) | maxLag gate (misfires) | High |
| U | **Availability** | Sink CAS conflict → 500 / no retry; readyz blind | Exploitable under multi-writer (SEC-V2-5) | none | Medium-high |
| V | **Availability** | aero-id dead-letter purge deletes evidence | Exploitable by any permanent failure (SEC-V2-12) | none (purge active, no replay tool) | Medium |
| W | **Eavesdropping** | Plaintext gRPC/HTTP | Deployment-dependent (SEC-V2-12) | edge TLS assumed | Medium |

---

## 4. Positive controls verified (re-verified at v2 HEADs)

1. **Sink JWKS verifier** (`internal/auth/verifier.go`): algorithm allow-list enforced **before** key selection; embedded JWK/JWKS-URL and unencoded payload rejected; `kid` must identify exactly one key; `use=sig` + `key_ops=[verify]`; private JWK material forbidden; HTTPS-only (loopback opt-in), redirects disabled, 2MiB cap; token 1MiB cap; `iss`/`aud`/`exp`/`nbf` checked; `client_id`/`azp` consistency enforced; `sub` required. (Caveat: key selection fetches JWKS pre-verification — SEC-V2-10.)
2. **Fail-closed source binding** (`internal/domain/models.go:143` `AllowsClient`): no allow-list ⇒ client must equal source ID; tenant re-checked inside the store transaction (`sources.go` `resolveIngestTenantFromData`).
3. **Payload hardening**: `rejectSensitive` (`service.go:1126`) forbids credential-named fields; AES-GCM field encryption with AAD; export files sealed; search digests HMAC-keyed; empty `SourceDigest` fails closed (`service.go:881`).
4. **Idempotency**: digest dedup with 409 on conflict (no silent overwrite); `idempotency_key` uniqueness.
5. **Deployed IdP token mint** (`infrastructure/defaultimpl/ed25519_issue.go` etc.): RFC 9068 `at+jwt` typ, `kid` from the same key pair as the published JWKS, 128-bit `jti`, `scope`/`client_id`/`iss` stamped; refresh rotation atomic with reuse-detection family kill + rotation velocity caps (`infrastructure/redis/refresh_token.go`).
6. **Deployed IdP introspection** (`protocols/oauth/handle_introspect.go:111-243`): client auth required (Basic/body/JWT assertion), oracle-collapsed 401 `invalid_client`, no-store headers.
7. **Deployed IdP scope gate** (`protocols/oauth/oauthvalidate/scope.go`): client allowlist semantics, `invalid_scope` 400 (RFC 6749 §3.3).
8. **Relays**: strict token-response validation, `secureEndpoint` HTTPS-or-loopback, bounded response reads, receipt validation matching event_id/tenant, singleflight token fetch + duplicate-credential rejection (aero-id), token refresh skew handling (vault), vault 401 → cache invalidation.
9. **Activation gates**: vault `AUDIT_GOVERNANCE_ENABLED` default false + bindings file required and non-writable (`internal/config/config_audit_governance.go:88-99`).
10. **Sink secrets fail-fast** (`resolveSecrets`), `-check-config`; single-JSON body decode + size caps; `wait_for` allow-list; safe export filenames; panic recovery; no-store on 5xx.
11. **Atomic self-audit of control-plane mutations** (`adminAction` same store-update) — governance ops today; read path missing (SEC-V2-9).
12. **Deployed IdP audit in production config**: `audit.backend=postgres, hash_chain=true` (k8s configmap) — stock default still memory (SEC-V2-16).

---

## 5. Residual risks

- **Dev-auth default (SEC-V2-1)** until remediated — the platform is effectively unauthenticated today.
- **Two IdP trees** with divergent security posture; deployment status of the legacy tree Unknown (SEC-V2-3/11). Any operator or implementer must not assume which tree is live.
- **Real-token path is dead end-to-end** (SEC-V2-2): no tenant_id/roles claims, so no production read/export path exists; G1/G5 cannot pass.
- **Contract is sound but ahead of code**: §1.4 dead-letter, §1.7 trust posture, sink mismatch-422, read self-audit are all normative in v2 docs and all unimplemented.
- **Scope/role → permission coupling** (SEC-V2-13) introduces an escalation axis that did not exist before the claim contract — the registry and the sink role map must be kept consistent (F2-style grep gate).
- **Bearer token replay within TTL** at the sink (no jti store); documented tradeoff.
- **No workload evidence** for capacity; single-snapshot store is O(ledger) — unchanged from v1 (see database_architect review).

## 6. Prioritized validation plan

| # | Item | Owner | Gate |
|---|---|---|---|
| P0 | SEC-V2-1: invert `AUDIT_ALLOW_DEV_AUTH` default; refuse dev-only config; manifest scan; 3 regression tests | sink | G1 (T-1.1) |
| P0 | SEC-V2-2: mint `tenant_id`+`roles` in deployed tree `buildAccessPayload`/`Subject`; e2e write→read→export with real token | IdP + sink | G1 (T-1.2, T-8a) |
| P0 | SEC-V2-3: re-pin batch plan B4 rows to deployed tree; path-existence CI gate; declare single IdP topology | planning | G0/G1 review |
| P0 | SEC-V2-13: scope registry deny-by-default for `audit:platform:cross_tenant`/`platform-admin`; role-map consistency grep gate | IdP | G5 (T-8d) |
| P1 | SEC-V2-6: vault dead state + readiness decoupling + relay metrics | vault | G4 (T-3, D1) |
| P1 | SEC-V2-7: aero-id in-tx/observable recording across **all 49 sites**; single delivery path | aero-id | G3 (T-5, T-10.1) |
| P1 | SEC-V2-4: opaque 401/500 bodies | sink | G2 (httpapi tests) |
| P1 | SEC-V2-5: conflict retry + fsync + readyz store probe | sink | G2 (T-6) |
| P1 | SEC-V2-10: JWKS cache + auth-path rate limit + query/export cost caps | sink | G2 (load test) |
| P2 | SEC-V2-8: envelope tenant mismatch → 422 | sink | G2 |
| P2 | SEC-V2-9: read self-audit + console server-side records | sink + console | G7 (T-12) |
| P2 | SEC-V2-12: pause purge until replay tool + DLQ alert | aero-id | G3 (T-10.2) |
| P2 | SEC-V2-14: skew window + server-derived stream IDs | sink | G2 |
| P3 | SEC-V2-11: legacy tree disposition (archive or port fixes) | planning | G8 |
| P3 | SEC-V2-15/16: L2 sunset, quota validation, audit backend default | per repo | G8 |

*Advisory only — no files modified. All Verified claims confirmed against cited code at the HEADs listed in §Scope on 2026-08-06; items marked re-verified carry their v1 finding numbers. Where protocol_expert (V2-1..13) and database_architect (F-13) disagreed with this review's tree attribution, the deployment-asset evidence (k8s configmap ↔ `ops/deploy/k8s-distributed/config.yaml`, `setup_wizard` presence, Dockerfile image tag) is the tiebreaker — see SEC-V2-3.*
