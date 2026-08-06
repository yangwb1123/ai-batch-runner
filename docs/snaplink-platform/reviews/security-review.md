# Security Review — Audit Governance Platform (adversarial production behavior)

**Role**: principal security engineer (adversarial review)
**Date**: 2026-08-06
**Scope**: audit ingestion design draft (`docs/snaplink-platform/audit-ingestion-design-draft.md`) verified against executable code in: snaplink (IdP, `/home/u1/workspace/demo/snaplink`), aero-id (`/home/u1/aero-id`), aero-vault (`/home/u1/aero-vault`), snaplink-audit-governance (sink, `/home/u1/workspace/demo/snaplink-audit-governance`), snaplink-console (`/home/u1/workspace/demo/snaplink-console`).
**Method**: `prompts/README.md` evidence standard applied. All findings below are **Verified** against code at the cited paths unless marked otherwise. I ran: `go test ./internal/auth/ ./internal/httpapi/` (sink, pass) and an empirical exploit harness against the real sink code (`tmp_verify/main.go`, since removed) demonstrating findings S1/S4/S8 end-to-end. Tests run for this review: sink auth + httpapi suites; prior reviews' suites (aero-id connector, snaplink discovery, sink auth/httpapi) were re-run and pass.

---

## 1. Assets, trust boundaries, attacker capabilities, entry points

### 1.1 Assets (ranked by sensitivity)

| Asset | Location | Notes |
|---|---|---|
| Audit ledger state (events, receipts, streams, segments, checkpoints) | sink `internal/store` snapshot (file or single Postgres row) | Confidentiality + integrity core. Encrypted fields at rest, plaintext metadata |
| Encrypted export blobs + archive segments | sink archive (file/S3 Object Lock) | AES-GCM sealed; download decrypts in API |
| Tenant/source/schema/policy/legal-hold/restore config | same snapshot | Control plane of the ledger |
| Producer outboxes + local hash-chain ledgers | aero-id `audit_publications`/`audit_ledger`; aero-vault `audit_governance_outbox`/`audit_events`; IdP `audit` store | Durability of the pipeline |
| OAuth client secrets, HMAC redaction keys, static L2 bearer tokens | producer config/env (`AUDIT_GOVERNANCE_HMAC_KEY`, binding secrets, `AuditSinkL2` bindings) | Credential material |
| Sink `AUDIT_SIGNING_SECRET` (checkpoint HMAC), `AUDIT_ENCRYPTION_KEY` (field/export crypto), JWKS trust config | sink env/config | Key material |
| IdP signing keys + issued access tokens | snaplink (`cmd/sso-server` Ed25519 issuer) | Root of the auth chain |

### 1.2 Trust boundaries

1. **Edge → sink API** (HTTP `:8089`, gRPC optional): bearer-token boundary. The only *cryptographic* boundary is the JWT verifier; dev tokens cross it with no signature (S1).
2. **Producer relay → IdP token endpoint**: OAuth 2.0 client_credentials + RFC 8707 `resource=audit-governance`. Strict client-side validation on both relays (positive controls).
3. **Producer relay → sink ingest API**: per-tenant source binding (client_id must be allowed by the source system). Client_id is the only principal identity the sink sees.
4. **Sink → IdP JWKS**: outbound HTTPS fetch, **uncached** (S5).
5. **Sink → archive/Vault/OTLP**: server-side configured, no request-driven URLs (SSRF surface small; Vault signer uses plain HTTP allowed by config).
6. **Console (browser) → nothing**: console audit log is client-side only; it never calls the ledger (S9).

### 1.3 Attacker capabilities considered

- **A0 unauthenticated remote** — exploitable today via dev-auth default (S1).
- **A1 tenant auditor / service** (legit token) — read paths, quota, query cost (S5, S13).
- **A2 write-scoped service** — event forging, stream pollution, occurred_at forgery, event-id squatting (S8, S10 design).
- **A3 compromised platform principal** — everything; assumed game-over, but platform powers must be *signature-gated* (S1).
- **A4 network position** — plaintext gRPC when exposed (S7).
- **A5 insider/operator** — out of scope except credential handling (S6).

### 1.4 Entry points

Sink HTTP: `POST /api/v1/events`, `/events:batch`, `/exports`, `/legal-holds`, `/restores…`, `/integrity/verify`, `/tenants`, `/sources`, `/schemas`, `/policies/retention`, `/admin/actions`; `GET /events…`, `/operations…`, `/aggregates…`, `/exports/{id}/download`, `/metrics`, `/healthz`, `/readyz` (last three **unauthenticated**). Sink gRPC: `Write`, `WriteBatch`, `WriteStream` (same authenticator, plaintext). Producers: outbound relay HTTP. IdP: `/token` client_credentials (per protocol_expert review).

---

## 2. Findings (sorted by severity)

### S1 — CRITICAL: Dev-token auth is enabled by default with no trust source required; any caller mints full platform powers (verified exploit)

**Evidence**
- `cmd/audit-api/main.go:45`: `allowDev := flag.Bool("allow-dev-auth", boolEnv("AUDIT_ALLOW_DEV_AUTH", true), …)` — **default true**.
- `internal/auth/auth.go`: `AuthenticateTokenContext` → `if a.AllowDev && strings.HasPrefix(token, "dev:") { return parseDevToken(token) }` — no signature, no HMAC, no server state. `parseDevToken` maps `dev:platform:platform-admin` → `Platform=true` and every permission in `permissionsForRoles`.
- `ValidateConfiguration` returns nil when only `AllowDev` is set (`!hasSecret && !a.AllowDev`), so a default deployment boots with **no cryptographic trust source at all**.
- Empirical (harness against real code): bare config accepted; `dev:platform:platform-admin` → `platform=true`, all perms; `GET /api/v1/events?tenant_id=tenant-a` with that token → **200 with tenant-a's full events including payload**; sink auth/httpapi test suites pass and only exercise dev tokens (`server_test.go:49` `auth.Authenticator{AllowDev: true}`).
- `deploy/docker-compose.verify.yml` sets `AUDIT_ALLOW_DEV_AUTH: "true"`; README documents the flag but the default is the fail-open value.

**Exploit preconditions/steps** (no credentials, no network position):
1. `curl -H 'Authorization: Bearer dev:platform:platform-admin' 'https://<audit>/api/v1/events?tenant_id=<victim>&from=…&to=…'` — read every event of any tenant.
2. Same token: `POST /api/v1/exports`, `/legal-holds`, `/restores/{id}/approve`, `/integrity/verify`, `/tenants`, `/sources`, `/schemas`, `/policies/retention` — full control-plane write.
3. gRPC `WriteStream` accepts the same token.
4. `dev:<tenant>:tenant-admin` / `:service` / `:auditor` mint any tenant-scoped role without authentication.

**Impact**: total confidentiality and integrity loss of the audit platform — the one system whose job is to be tamper-evident. An attacker can read all tenants' audit data, delete nothing (append-only) but can release legal holds, approve restores, rewrite retention, register schemas that change what is encrypted, and forge admin actions. This is the single release blocker.

**Remediation (required)**:
1. Invert the default: `AUDIT_ALLOW_DEV_AUTH` must default to **false**; dev auth must require an explicit opt-in *and* a startup warning-to-refusal when combined with a non-loopback listen address.
2. Refuse to start when `AllowDev=true` and no JWT trust source is configured (dev-auth must never be the *only* auth).
3. CI/deploy gate: assert dev-auth off in all production manifests (scan compose/k8s for `AUDIT_ALLOW_DEV_AUTH` true/false).

**Regression test**: (a) `ValidateConfiguration` fails for `Authenticator{AllowDev: true}` alone; (b) httpapi test asserting `dev:platform:platform-admin` → 401 when `AllowDev=false`; (c) manifest scan test enumerating deployment files and failing on `AUDIT_ALLOW_DEV_AUTH=true` unless a `dev:` profile is explicit.

---

### S2 — HIGH: IdP client_credentials tokens cannot establish tenant context at the sink; read/export/legal-hold paths 403 for every production token (verified)

**Evidence**
- `infrastructure/defaultimpl/issue_payload.go` `buildAccessPayload` + `ed25519_types.go` `ed25519Payload`: access tokens carry `iss`, `sub`, `aud` (RFC 8707 resources), `scope`, `client_id`, `jti` — **no `tenant_id`, no `roles`** (struct has no such fields; `Extra` is empty for client_credentials).
- Sink `auth.go` `parseJWT`: `claims.TenantID = stringClaim(payload, "tenant_id")` else `"tenant"` — always empty for IdP tokens; roles empty → `permissionsForRoles` yields nothing; scope alone grants `audit:event:write`.
- Sink `require()`: `if permission != "audit:event:write" && claims.TenantID == "" && !claims.Platform → 401` — **every read/export/legal-hold/restore call 403s** with a real IdP token.
- Writes only work when exactly one active source across all tenants allows the client_id (`resolveIngestTenantFromData` ambiguity → 403); multi-tenant shared service clients cannot write at all.
- Scope drift (per distributed_engineer F2, consistent with code): implementations enforce `audit:event:write` (`auth.go` role map, aero-id `auditScope`, aero-vault `RequiredScope`) while the draft/scope-matrix provision `audit:event:ingest` — provisioning per the matrix fails at rollout.
- Note: protocol_expert's F-2 claim "no scope/client_id/iss/kid" is **inaccurate for the wired Ed25519 issuer** — those claims exist. The operative gaps are `tenant_id` and `roles` (and the sink's strict JWKS+aud requirements vs IdP's `aud`=resource only).

**Impact**: the production pipeline only works on forged dev tokens today; any rollout using real OAuth tokens breaks at the first read/export; platform/tenant-admin roles are unattainable (no `roles` claim), so even multi-tenant admin workflows cannot be delegated.

**Remediation**: add `tenant_id` (and `roles`/`permissions` as needed) to the access-token payload in all three issuers (ed25519/ecdsa/rsa share `buildAccessPayload`); or have the sink resolve tenant via RFC 7662 introspection/`/userinfo`-style claim lookup for machine clients; align the scope string in the scope-matrix draft to `audit:event:write` (or implement the matrix's `audit:event:ingest` as an alias).

**Regression test**: e2e — mint a client_credentials token via sso-server test wiring with `resource=audit-governance`; assert sink `POST /events` (write, 202) **and** `GET /events` (read, 200 with tenant context) and `POST /exports` (202).

---

### S3 — HIGH: Vault relay has no terminal failure state; poison events retry forever and one row permanently flips `Ready()` (availability; restart loops) (verified)

**Evidence**
- `aero-vault/internal/auditgovernance/relay.go` `retryFact` → `boundedBackoff` (cap 300s) forever; repository has no `dead`/status column (`audit_governance_claim.go`, `audit_governance_write.go`: `delivered_at_ns=0` rows are claimed forever; `RetryAuditGovernance` never marks terminal).
- `http.go` `validateReceipt`: any non-202 (including sink **409 conflict** and **422 schema_not_found**) → `httpStatusError` → retried forever.
- `runtime.go` `Ready()`: `OldestPendingAuditGovernance` > `maxLag` → not ready — one poison row blocks readiness.
- Contrast: aero-id has `dead_letter` after `MaxAttempts` (`outbox/repository.go:109-117`) — the pattern exists in the family and is absent in vault.
- Combined with draft S3: sink returns 422 for unknown/inactive schema and 409 for event_id/idempotency conflicts — permanent classes that vault cannot distinguish.

**Exploit/impact paths**: (a) a schema mismatch (deploy-time bug) bricks the tenant's relay and readiness; (b) under the draft's **deterministic `event_id = SHA-256(service|event_type|tenant|trigger_key)`**, an attacker with write scope who can predict a victim's trigger key (request_id, message_id|edit_seq) can **squat the event_id** with different content → victim's event 409s forever → permanent audit gap for that operation, relay unhealthy. Today's random UUIDs make squatting unpredictable, but the draft's design creates the attack surface.

**Remediation**: classify terminal failures (4xx permanent: 409/422) → dead-letter state + alert + replay tool; decouple `Ready()` from backlog lag (alert-only); sink-side conflict policy with bounded retry.

**Regression test**: relay unit test asserting 409 and 422 responses produce a terminal state and stop retrying; `Ready()` test with one dead row asserting ready.

---

### S4 — HIGH: Sink leaks raw error text on 500s and auth failures are an oracle; contradicts the platform's own oracle-safe discipline (verified)

**Evidence**
- `internal/httpapi/server.go` `errorBody`: 500 → `message = err.Error()` unless the string contains "internal server error". Store failures, archive/S3 errors, and `VaultTransitSigner` errors (`vaultsigner.go` includes `body=%s` from Vault) reach unauthenticated/authenticated clients verbatim.
- `require()` wraps auth errors: `fmt.Errorf("%w: %v", domain.ErrUnauthorized, err)` — clients can distinguish expired / issuer-mismatch / audience-mismatch / signature-invalid / JWKS-fetch failures, and probe the sink's trust config (e.g. "fetch JWKS: HTTP 500" reveals the JWKS host).
- `AGENTS.md` §3 (IdP) mandates byte-identical oracle-safe errors; the sink is the same platform's audit surface and violates the same discipline.
- Empirical: harness 401/400 paths return detailed messages.

**Impact**: information disclosure aiding targeted attacks (storage paths, bucket names, Vault addresses, trust topology); violates the platform's fail-closed error contract.

**Remediation**: single opaque `401 invalid_token` for all auth failures (details to server logs/audit only); generic 500 body with request_id; keep detail server-side. Regression test asserting byte-identical 401 bodies for expired/issuer-mismatch/signature-failure and that 500 bodies contain no path/Vault/S3 substrings.

---

### S5 — MEDIUM-HIGH: No rate limiting or cost caps on read/export paths; O(ledger) scans; uncached JWKS fetch per request (verified)

**Evidence**
- `server.go` middleware: no limiter; quota (`service.go` `checkTenantAndQuota`) applies to **ingest only**.
- `QueryEvents`, `Operation`, `OperationTimeline`, `AggregateTimeline`, `EvaluateRetention`, `runExport` scan the whole in-memory snapshot (`store.Read` full map iteration), page-capped at 1000 but scan is O(n).
- `runExport` spawns an unbounded goroutine per job (`go s.runExport(job.ID)`); each export re-scans O(n) and re-encrypts.
- `verifier.go` `remoteVerificationKey` → `fetchJWKS` per verification with **no cache**: every JWT request triggers a sink→IdP HTTP fetch (5s timeout). Request pacing makes the IdP JWKS endpoint a DDoS amplifier and adds latency to every API call.
- `idempotency_key` conflict check is an O(events) scan inside every ingest (`service.go` Ingest).

**Impact**: any auditor (or dev-token) holder can drive CPU/memory exhaustion; a few concurrent export jobs starve the single-writer store; JWKS fetch doubles per-request latency.

**Remediation**: per-tenant query/export rate limits + query cost caps (time-range width, result scan budget); JWKS cache with kid-indexed entries and short TTL; bound concurrent exports. Regression: httptest counting JWKS fetches asserts ≤1 per N verifications; load test asserting bounded query latency on a large snapshot.

---

### S6 — MEDIUM: Vault has dual audit egress with inconsistent redaction and static long-lived bearer tokens (verified)

**Evidence**
- `aero-vault/internal/events/audit_sink_l2.go`: a *second* sink adapter POSTs the **verbatim `deleted@1.1` payload** (bucket/key/metadata/actor unredacted, `auditSinkL2MaxPayloadBytes` 1MiB) with a **static per-tenant bearer token** from config; no rotation path beyond redeploy.
- The governance publisher (`internal/auditgovernance/`) sends HMAC-digested identities (`redaction.go`: actor/target/detail digests) with short-lived OAuth tokens.
- Same delete event flows to both paths with different IDs (`uuid.NewString()` per fact, `facts.go`) — divergent records across two sinks.

**Impact**: sensitive object paths sent unredacted to a second destination; credential lifecycle gap (long-lived static tokens); duplicated/inconsistent audit records make reconciliation and legal-hold completeness ambiguous.

**Remediation**: single egress path; short-lived tokens with rotation; align redaction policy; document L2 as legacy with sunset date.

---

### S7 — MEDIUM: gRPC listener is plaintext; HTTP has no in-app TLS (verified)

**Evidence**: `cmd/audit-api/main.go` `grpc.NewServer()` with no credentials; `http.Server{Addr: *listen}` without TLS. Defaults bind `:8089` (all interfaces).

**Impact**: bearer tokens and event payloads in cleartext when gRPC is enabled beyond loopback; HTTP depends on edge TLS termination being present (not enforced). Network-position attacker (A4) captures tokens/payloads.

**Remediation**: document/require TLS termination; refuse to enable gRPC without credentials when bound non-loopback; default binds to loopback.

---

### S8 — MEDIUM: Write-scoped producers can pollute arbitrary tenant streams and forge `occurred_at`; clock-skew contract unimplemented (verified)

**Evidence**
- `domain/models.go` `Event.Stream()`: a client-supplied `StreamID` is used **verbatim** — a producer can append into any stream of its tenant (e.g., a victim operation's timeline), mixing attacker-authored events into another operation's hash chain and replay.
- `Ingest` never validates `StreamID` against `OperationID`/`AggregateType`/`AggregateID`.
- `occurred_at` is client-supplied with no bound vs `ReceivedAt`; empirical harness: event with 1h skew accepted (clock-skew rejection contract from the draft/§F7 distributed review is unimplemented).
- Retention evaluation and legal-hold matching filter on `occurred_at` — forged timestamps game compliance queries and retention eligibility.

**Impact**: within-tenant audit tampering surface (chain contamination, replay pollution) and time-based compliance distortion by any write-scoped service account (A2).

**Remediation**: derive `StreamID` server-side from operation/aggregate (never trust the field); enforce `|occurred_at − received_at| ≤ skew_window` (configurable, fail-closed); regression test asserting cross-stream append rejected and skew rejected.

---

### S9 — MEDIUM: Read-path self-audit absent; console "cannot be bypassed" guarantee unimplemented (verified)

**Evidence**
- No `audit.event.read` record anywhere in the sink (grep over `internal/`); `queryEvents`/`getEvent`/`downloadExport` write no AdminAction. The draft's console claim ("audit queries self-audited by the ledger's read API so direct API calls can't bypass") has no server-side counterpart.
- `snaplink-console/lib/services/audit_log_service.dart`: 1000-entry **browser localStorage ring** recording local ops only; console never calls the sink read API (`portal_api.dart` has no audit calls). Any user with devtools can forge or clear the console log.

**Impact**: read access to the ledger is untraceable — the "who looked at what" property of an audit platform is absent; console-side records are worthless as evidence.

**Remediation**: append `audit.event.read` (tenant, actor, query summary, time) in the same store update as served queries/exports; console displays server-side records only.

---

### S10 — MEDIUM: IdP audit backend is volatile by default; sink snapshot lacks fsync and conflict retry (verified)

**Evidence**
- IdP: `cmd/sso-server/serverbuildauthn/build_audit_secrets.go` `case "", "memory"` — stock default is the in-memory ring (`config_audit.go` documents it drops on restart). The draft's "L0 complement" premise needs a durability decision first.
- Sink: `store/store.go` `fileBackend.Save` = `os.WriteFile` + `os.Rename` with **no fsync/dir-sync** — crash can lose the last rename; Postgres backend: single-row snapshot, `Update` returns `ErrSnapshotConflict` with **no retry** — concurrent replicas lose writes silently.

**Impact**: audit records lost on restart (IdP), on crash (sink), and under multi-replica concurrency — integrity/availability of the evidence store.

**Remediation**: production profile defaults to sqlite/postgres for the IdP audit backend; fsync + directory sync in `fileBackend.Save`; bounded retry on `ErrSnapshotConflict`; crash/conflict regression tests.

---

### S11 — MEDIUM: `defaulttoken.JWTIssuer` implements a non-cryptographic XOR "HMAC" and a 2-segment fake JWT (latent, not wired in stock sso-server) (verified)

**Evidence**
- `infrastructure/defaultimpl/defaulttoken/jwt_issuer.go`: `hmacSHA256` is `data[i] ^ key[i%len(key)]` — a Vigenère-style keystream, **not** HMAC; `buildToken` produces `base64(base64(data)).sig` (2 segments, not a JWS); `Validate` is an in-memory map lookup of issued tokens.
- Exported via `defaultimpl.NewJWTIssuer` alias (`aliases_token.go`); used only in tests today; stock sso-server wires the Ed25519 issuer.

**Impact**: latent footgun — if any strategy or downstream code registers this as a JWT issuer, tokens are forgeable (known-plaintext keystream recovery, two-time-pad) and unverifiable by real JWT verifiers. Not exploitable in current stock wiring.

**Remediation**: delete or replace with `crypto/hmac` + real compact JWS; add a lint/test asserting no XOR-based MAC exists in the tree.

---

### S12 — MEDIUM (design): deterministic event_id design creates a squatting surface; current random UUIDs make sink dedup vacuous (verified)

**Evidence**
- Draft §1: `event_id = SHA-256(service|event_type|tenant_id|trigger_key)`. Reality: aero-vault `facts.go` and aero-id `buildAuditFact` use `uuid.NewString()`/`idgen.NewID()` random IDs; the sink's `(tenant_id, event_id)` dedup therefore only covers relay retries of the *same fact row*, and regenerated facts create duplicate ledger events.
- If the draft's deterministic scheme is adopted, S3's squatting attack becomes reachable (event_id predictable → pre-registration with different content → permanent 409 for the victim's event). Sink `Ingest` conflict path verified: same event_id + different digest → 409, no overwrite.

**Remediation**: if deterministic IDs are adopted, make the trigger key unguessable or add a sink-side "claim then require matching digest" policy; document random-UUID semantics as retry-dedup-only.

---

### S13 — LOW (residual by design): deterministic search digests enable equality/frequency analysis within a tenant (verified)

**Evidence**: `security/fieldcrypto.go` `SearchDigest` = deterministic HMAC; `protectSensitiveFields` stores `field__search_digest` inside the payload, which queries return; tenant auditors can compare digest equality across events; a write-scoped actor can compute digests of candidate values (ingest own event under a searchable schema) and dictionary-attack low-entropy fields (email, phone).

**Mitigation today**: digests are HMAC-keyed (attacker cannot compute digests for arbitrary values without the key); the remaining exposure is equality/frequency correlation by an auditor who already holds read access. Document as accepted tradeoff; future: per-tenant keyed blind indexes with server-side lookup.

---

### S14 — LOW: ingest quota is per-process memory; multi-replica bypass; quota can be disabled by tenant config (verified)

**Evidence**: `service.go` `quotas map[string]quotaWindow` in the Service struct (per instance); `createTenant` accepts arbitrary `EventsPerSecond` (negative/zero disables quota). Dev-auth platform tokens can provision unlimited quota-free tenants today (S1 makes this moot until fixed).

**Remediation**: shared quota backend for multi-replica; validate `EventsPerSecond >= 0`.

---

### S15 — INFO: unauthenticated `/metrics` exposes aggregate counters only; `X-Request-ID` reflected but header rules prevent CRLF injection; `safeDownloadName` sanitizes; body decode enforces single JSON value; `wait_for` allow-listed; page_size capped. No findings.

---

## 3. Abuse-case table

| # | Abuse case | Attack path | Status today | Mitigating control | Residual |
|---|---|---|---|---|---|
| A | **Identity spoofing** | Forge `dev:<tenant>:<role>` header | **Exploitable** (S1) | none (no signature) | Critical until default inverted |
| B | **Identity spoofing** | Forge JWT via XOR "HMAC" issuer | Not wired (S11) | stock uses Ed25519; sink verifier rejects 2-segment tokens | Latent |
| C | **Replay** (token) | Reuse captured access token | Standard bearer exposure; `exp`/`nbf` checked; sink has no jti-replay store | exp/nbf, JTI at IdP | Tokens replayable within TTL (documented bearer tradeoff) |
| D | **Replay** (event) | Retry/re-deliver same event | Absorbed: sink dedups by event_id+content digest; 202 on duplicate | digest idempotency | Random UUIDs limit dedup to same-row retries (S12) |
| E | **Cross-tenant read** | `tenant_id` query param with non-platform token | **No leak** — `tenantFor` ignores param for non-platform (verified 200 = own tenant only) | code path | Silent ignore is confusing, not a leak |
| F | **Cross-tenant read** | `dev:platform:platform-admin` + `tenant_id=<victim>` | **Exploitable** (S1; verified 200 with victim data) | none | Critical |
| G | **Proxy/header forgery** | XFF/Host/forwarded headers | Sink ignores all proxy headers; no header-derived identity (IngestPrincipal is server-derived, `sources.go` comment) | verified | none |
| H | **Log injection** | `X-Request-ID` with CRLF | Go header parser rejects control chars | stdlib | none |
| I | **Resource exhaustion** | Query/export O(ledger), no rate limit | Exploitable by any auditor/dev token (S5) | page_size cap only | Medium-high |
| J | **Resource exhaustion** | JWKS fetch amplification per request | Exploitable (S5) — each token request fetches IdP JWKS | 5s timeout, size caps | Medium-high |
| K | **Resource exhaustion** | Batch ingest | Quota per tenant (in-process) | quota | Per-process only (S14); body capped 512KiB |
| L | **Sensitive-data leakage** | 500 error bodies with storage/Vault details | Exploitable (S4) | no-store on 5xx only | High |
| M | **Sensitive-data leakage** | Unredacted L2 egress (bucket/key/metadata/actor) | Exploitable by design (S6) | static tokens; HTTPS-or-loopback | Medium |
| N | **Sensitive-data leakage** | Search-digest equality correlation | Tenant-auditor capability (S13) | HMAC-keyed digests | Low (accepted) |
| O | **Sensitive-data leakage** | Console log forgery/clearing | Trivially exploitable (S9) | none | Medium (console records worthless) |
| P | **Integrity** | Stream pollution via client StreamID | Exploitable by write-scoped actor (S8) | none | Medium |
| Q | **Integrity** | occurred_at forgery → retention/legal-hold gaming | Exploitable (S8) | none (skew contract unimplemented) | Medium |
| R | **Availability** | Event-id squatting under deterministic IDs | Not reachable with random UUIDs; becomes reachable if draft adopted (S3/S12) | none | Medium (design) |
| S | **Availability** | Poison row → vault `Ready()` false → restart loop | Exploitable via bug/deploy error (S3) | maxLag gate (misfires) | Medium-high |
| T | **Eavesdropping** | Plaintext gRPC/HTTP | Deployment-dependent (S7) | edge TLS assumed | Medium |

---

## 4. Positive controls verified

1. **Sink JWT verifier** (`internal/auth/verifier.go`): algorithm allow-list (EdDSA/ES/RS/PS256; HS256 only with explicit local opt-in); algorithm checked **before** key selection; embedded JWK/JWKS-URL and unencoded-payload headers rejected; `kid` must identify exactly one key; JWK `use=sig`, `key_ops=[verify]` enforced; private JWK material forbidden; JWKS HTTPS-only (loopback opt-in), redirects disabled, 2MiB cap; token 1MiB cap; `iss`/`aud`/`exp`/`nbf` checked; `client_id`/`azp` consistency enforced; `sub` required.
2. **Fail-closed source binding** (`domain/models.go` `AllowsClient`): no allow-list ⇒ client must equal source ID; explicit list required otherwise; tenant resolution re-checked inside the store transaction (`resolveIngestTenantFromData`).
3. **Payload hardening**: `rejectSensitive` forbids credential-named payload fields (password/token/secret/card_number/device_credential/private_key, case-insensitive); field-level AES-GCM with AAD binding `tenant/field/event_id`; export files AES-GCM sealed; search digests HMAC-keyed.
4. **Idempotency**: content-digest dedup with conflict detection (no silent overwrite); `idempotency_key` uniqueness enforced; empty `SourceDigest` fails closed in `verifyContentDigest`.
5. **Relays**: strict token-response validation on both relays (non-empty, no CRLF, `Bearer`, `0 < expires_in ≤ 24h`, scope pinned); endpoints HTTPS-or-loopback with redirects disabled; bounded response reads (64KiB); receipt validation requires matching event_id/tenant and non-conflict status; `wait_for=ledgered` synchronous ack; vault invalidates its cached token on 401; aero-id uses singleflight token fetch, rejects duplicate credentials across bindings, and checks tenant-binding coverage at startup.
6. **Secrets fail-fast** (sink `resolveSecrets`): missing/well-known-default/shared signing+encryption secrets refuse startup unless `AUDIT_ALLOW_DEV_SECRETS=true`; `-check-config` validates without opening the store or network.
7. **HTTP hygiene**: single-JSON-value body decode with size caps; per-event payload cap (256KiB); `wait_for` allow-list; page_size cap; safe export filenames; panic recovery; no-store on 5xx.
8. **Atomic self-audit of control-plane mutations** (`adminAction` inside the same `Store.Update`): tenant/source/schema/policy/export/hold/restore changes are recorded atomically with the mutation.
9. **IdP kernel invariants** (per AGENTS.md §3, spot-checked): oracle-safe grant errors, refresh-family rotation, JTI replay for receivers, alg-before-verify, trusted-proxy gating — preserved in the IdP itself; the sink does not yet match the oracle discipline (S4).

## 5. Residual risks

- Dev-auth default (S1) until remediated — treat the platform as unauthenticated until then.
- Sink architecture: single-snapshot store is O(ledger) per write/read — a hard ceiling for the draft's high-volume L1 classes; no workload evidence exists to size aggregation thresholds (per database_architect).
- Deterministic search digests and (proposed) deterministic event_ids create the enumeration/squatting surfaces in S12/S13.
- Multi-replica quota and snapshot conflict retry gaps (S5/S10/S14) assume a single-instance deployment today.
- No e2e path exists from real IdP tokens to the sink (S2) — the only working path is dev-auth.

## 6. Prioritized validation plan

| # | Item | Owner | Gate |
|---|---|---|---|
| P0 | Invert `AUDIT_ALLOW_DEV_AUTH` default; refuse dev-only config; deploy-manifest scan | sink | `make ci` + config test |
| P0 | S1 regression tests (config validation, 401 for dev tokens when disabled, manifest scan) | sink | `make ci` |
| P0 | S2: add `tenant_id`+`roles` to IdP access tokens; sink e2e from client_credentials to write+read+export | IdP + sink | e2e suite |
| P1 | S3: terminal failure classification + dead state in vault relay; decouple `Ready()` from lag | vault | relay tests + Ready test |
| P1 | S4: opaque 401/500 bodies; oracle tests | sink | httpapi tests |
| P1 | S5: query rate limits, export concurrency bound, JWKS cache | sink | load test + JWKS-count test |
| P2 | S8: server-derived stream IDs; clock-skew window | sink | ingest tests |
| P2 | S9: `audit.event.read` self-audit | sink | httpapi tests |
| P2 | S10: fsync + conflict retry; IdP audit backend default decision | sink + IdP | crash/conflict tests |
| P3 | S6/S7/S11/S13/S14 follow-ups | per repo | CI |

---

*Advisory only — no files were modified. All claims labeled Verified were confirmed against the cited code paths and executed tests on 2026-08-06; items inherited from prior reviews are marked with their source review.*
