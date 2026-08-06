# Identity-Protocol Review — Snaplink Audit Ingestion (OAuth 2.0 / OIDC surface)

> **Reviewer**: identity-protocol reviewer role (`prompts/README.md` applied).
> **Subject**: audit ingestion design draft
> (`docs/snaplink-platform/audit-ingestion-design-draft.md`, `[PROPOSED]`) vs
> current implementation in the four codebases that realize it.
> **Evidence standard**: findings are labeled Verified (code read at cited
> symbol/path), Partial, Missing, or Proposed (draft-only). Checks that ran for
> this revision: `go test ./internal/connector/auditgovernance/` (aero-id, ok),
> `go test ./cmd/server/ -run TestOIDCDiscovery` (snaplink, ok),
> `go test ./internal/auth/ ./internal/httpapi/` (sink, ok). No full CI of the
> four repositories was run; static reading covered the paths cited below.

## 1. Protocol/profile scope and authoritative references

The design draft and the four codebases that implement it (`/home/u1/snaplink`
= IdP, `/home/u1/aero-id` + `/home/u1/aero-vault` = producers,
`/home/u1/workspace/demo/snaplink-audit-governance` = sink) realize exactly one
identity-protocol surface: **OAuth 2.0 client-credentials service tokens
(RFC 6749 §4.4) with Bearer usage (RFC 6750 §2.1), RFC 8707 resource
indicators (client-side only), JWT (RFC 7519), JWKS (RFC 7517), a
client-credentials JWT verification path at the sink, an RFC 7662-style
introspection endpoint, an RFC 8628 device flow, and OIDC discovery metadata
(RFC 8414 / OIDC Discovery 1.0)**.

Out of scope — **no implementation evidence found** in any reviewed tree:
SAML, SCIM, WebAuthn, CAEP/SSF. The design's `auth.token.issue`,
`auth.login.*`, `tenant.delete` event classes touch IdP behavior but carry no
other protocol. Redirect-based flows: there is **no OAuth authorization-code
endpoint at all** (`/authorize` does not exist in `cmd/server/routes.go`), so
redirect_uri/state validation is N/A; the `/callback?provider=` route is a
social-provider callback, not OAuth.

Authoritative references (normative): RFC 6749 §§2.3.1, 3.1, 3.2, 4.4, 5.1,
5.2, 6; RFC 6750 §2.1; RFC 7662 §2; RFC 7519 §4.1; RFC 7515 §4.1; RFC 7517;
RFC 8414 §2; RFC 8707 §2; RFC 8628 §§3.1–3.2; RFC 9700 (OAuth Security BCP)
§§4.4.1, 4.7. No OIDF certification is claimed or evidenced anywhere; see §4.

## 2. Compliance matrix

Status: **V** = Verified compliant, **P** = Partial, **M** = Missing,
**D** = Deviation (implemented differently), **X** = no implementation.
"Test" names the check that ran or exists for the behavior.

| # | Spec § | Requirement | Implementation evidence | Status | Deviation / test |
|---|---|---|---|---|---|
| 1 | RFC 6749 §4.4 | client_credentials grant | `snaplink/sso.go:894` `ClientCredentials`; `cmd/server/routes.go:247` `token` handler; aero-id `oauth.go` tokenRequest; vault `token.go` via SDK | V | engine + connector tests pass (aero-id `internal/connector/auditgovernance` ok) |
| 2 | RFC 6749 §2.3.1 | client authentication — basic or post | `/token` reads `client_id`/`client_secret` form values only (`routes.go:249-254`) | P | **client_secret_post only**. Basic and private_key_jwt advertised in discovery but unimplemented (no `Authorization: Basic` parsing anywhere in `cmd/server`) |
| 3 | RFC 6749 §3.2 | token request parameters in body, `application/x-www-form-urlencoded` | `r.FormValue` in `token`/`deviceCode`/`introspect` handlers | M | Query-string parameters accepted; no Content-Type enforcement → secrets can ride in query (log leakage), ambiguous requests accepted |
| 4 | RFC 6749 §5.1 | token response: Bearer, expires_in, no-store caching | `tokenResponse` `routes.go:139`; `ClientCredentials` mints Bearer + ExpiresIn | P | **`Cache-Control: no-store` / `Pragma: no-cache` absent**; empty `"refresh_token": ""` serialized for client_credentials (§4.4.3 says SHOULD NOT issue) |
| 5 | RFC 6749 §5.2 | token error response | `tokenErrorResponse` `routes.go:149`; invalid_request 400, invalid_client 401, invalid_grant 401, unsupported_grant_type 400, RFC 8628 errors mapped | V | no tests at HTTP layer for `/token` error paths |
| 6 | RFC 6749 §4.4.3 / §3.3 | scope handling | `ClientCredentials` never reads `scope`; JWT claims have no scope | M | **AS ignores scope entirely** — no scope registry, no scope claim, no `scope` in response; `audit:event:write` is only meaningful at the sink |
| 7 | RFC 8707 §2 | resource indicator | vault `token.go:101` `resourceTransport` injects `resource=audit-governance`; aero-id `oauth.go` sends `resource` | P | **Client-side only**; IdP ignores `resource`. Token audience = `app.Audience` (per-client config), not the requested resource |
| 8 | RFC 6750 §2.1 | Bearer in Authorization header | aero-id `publisher.go` `newRequest`; vault `http.go`; sink `auth.go` `Authenticate` | V | header-injection guards on token values (CR/LF rejected) in all three producers + sink |
| 9 | RFC 7519/7515 | JWT claims / header | `snaplink/token/jwt/generator.go:52-55` header = `{alg, typ}` — **no `kid`**; `sso.go:925` claims = `{sub, aud, iat, exp, uid}` — **no `iss`, no scope/roles/perms/client_id/azp/tenant_id** | M | **JWT cannot be verified by the sink**: JWKS mode requires `kid` (`verifier.go:169`); issuer check (if configured) fails; permission claim absent → 403 (§3 F-2) |
| 10 | RFC 7517 | JWKS publication | `/.well-known/jwks.json` (`routes.go:77`, `engine.JWKSHandler`); `jwks.go` emits kid/kty/use/alg/n/e | V | keys well-formed; but signer never emits `kid` (row 9) |
| 11 | RFC 7662 §2.1/2.2 | introspection requires client auth; `active` response | `POST /introspect` `routes.go:174` requires only `token`+`client_id` form values; `IntrospectTokenResponse` has `active` | M | **Caller is not authenticated** — any caller knowing a client_id (no secret) gets token state oracle; params also accepted from query string. No HTTP-level tests for auth failure |
| 12 | RFC 8414 / OIDC Disc §4.3 | metadata accuracy; issuer | `oidcDiscovery` `routes.go:663`; `TestOIDCDiscovery` (passes, but codifies wrong values) | M | **token_endpoint→`/authenticate`** (password auth, not RFC 6749 token endpoint — the real one is `/token`); **authorization_endpoint→`/login`** with `response_types: [code]` but no `/authorize`; **revocation_endpoint→`/revoke` = 404 (no route)**; userinfo_endpoint→`/validate` (session check, non-standard claims); `client_secret_basic`/`private_key_jwt` advertised but unimplemented; `device_code` in grant_types but no `device_authorization_endpoint` metadata. Issuer derived from `r.Host` + `cfg.Port` (`routes.go:63-72`) — Host-header-driven, no allowlist, duplicate port when Host carries one |
| 13 | RFC 8628 §3.1/3.2 | device grant client auth; absolute verification_uri | `deviceCode` `routes.go:908`; `InitiateDeviceAuthorization` `sso.go:1117` | M | confidential clients **not authenticated** (client_id only); `verification_uri` is **relative** (`/device/verify`); no discovery metadata |
| 14 | RFC 9700 §4.4.1 | constant-time secret comparison | `sso.go:909` `clientSecret != app.ClientSecret` | M | plain string compare (timing side channel, local) |
| 15 | sink ingest authz | token → permission → ingest | sink `auth.go` `parseJWT` maps scope/roles/permissions claims; `httpapi/server.go:213` `require("audit:event:write")`; `sources.go:124` client allowlist | V* | verifier itself is hardened (alg allowlist, embedded-key/JWKS-URL-in-header forbidden, kid uniqueness, key material validation, restricted JWKS client — `verifier.go`); **but no IdP-issued token can satisfy it** (§3 F-2). `AllowDev` default true is the only working path (§3 F-1) |
| 16 | design §1.2 | outbox + lease/batch/backoff relay | aero-id `repository/audit_ledger.go:205` claim (FOR UPDATE SKIP LOCKED, lease 5m, batch 50), `callback_audit.go` drain + backoff, dead_letter after `max_attempts=8` (`configs`); vault `audit_governance_claim.go:35` claim + `relay.go` boundedBackoff | P | batch 50 vs draft 100; backoff cap 1h vs draft 300s; status strings vs draft 0-3 (cosmetic). **Vault relay has no attempt cap / dead state — retries forever**; only the legacy `internal/events` relay has maxAttempts=10 |
| 17 | design §1.3 | idempotency | sink `service.go:344` `Ingest` — dedup on `(tenant, event_id)` + canonical digest; duplicate→success, content conflict→409; idempotency_key unique per tenant | V | digest-based dedup is stronger than draft's `(source_service, event_id)`; aero-id `checkAuditDuplicate` pre-check in same tx |
| 18 | design §1.5 | permanent 4xx → dead immediately | sink `statusForError` `server.go:959` (400 ErrInvalid / 422 schema); aero-id retries all non-202 to max_attempts; vault retries forever | D | **422→dead immediate is not implemented**; poison events consume attempts/backoff slots but do not block batches (claim ordering by `next_attempt_at`) |
| 19 | design §1.5 / sink § | occurred_at clock-skew rejection/annotation >5min | grep of `internal/service`, `internal/domain`, `internal/httpapi`: **no skew logic found** | M | `occurred_at` required (`models.go:374`) but never compared to sink time; no test |
| 20 | scope registry | scope naming | aero-id `oauth.go:17` `auditScope = "audit:event:write"`; vault `model.go:17` `RequiredScope = "audit:event:write"`; sink permissions `audit:event:write` | D | **draft/scope-matrix name `audit:event:ingest` is stale** vs implemented `audit:event:write`; IdP has no scope registry at all |

## 3. Findings

Sorted by severity. Evidence labels per `prompts/README.md`.

### F-1 Critical — Sink accepts unverifiable dev tokens by default (auth bypass)

- **Location**: `snaplink-audit-governance/cmd/audit-api/main.go:44`
  (`allowDev := flag.Bool("allow-dev-auth", boolEnv("AUDIT_ALLOW_DEV_AUTH", true), ...)`),
  `internal/auth/auth.go:37-66` `parseDevToken`.
- **Requirement level**: OAuth 2.0 client authentication (RFC 6749 §4.4.3,
  §10); OAuth Security BCP (RFC 9700).
- **Verified**: `parseDevToken` accepts `dev:<subject>:<roles>[:client_id]`
  with **no signature verification**; roles map to permissions in
  `permissionsForRoles` (`event-writer`→`audit:event:write`;
  `compliance`/`tenant-admin`→read/export/legal-hold/restore-approve;
  `platform-admin`→`audit:platform:cross_tenant` + write). Default is **on**
  (env default true); only a log warning is emitted. The e2e and unit tests
  exercise exclusively dev tokens
  (`test/e2e/fullstack.sh:62,85`, `internal/httpapi/server_test.go:58`), so the
  production verification path has **no test coverage**.
- **Impact**: any party that can reach `POST /api/v1/events` (or any
  authenticated route) can forge `Authorization: Bearer dev:demo:compliance`
  and ingest arbitrary ledger events, read/export the ledger, manage legal
  holds, and approve restores. Verified exploit path.
- **Corrective behavior**: flip default to `false`; make `AllowDev` mutually
  exclusive with a production trust source (fail startup, not warn); CI check
  that deployment configs set `AUDIT_ALLOW_DEV_AUTH=false`; add a test that a
  `dev:` token is rejected when the flag is off. Validation: set the flag
  false, POST a dev token, expect 401.

### F-2 Critical — IdP-issued client-credentials JWT can never pass sink authorization (end-to-end break)

- **Location**: `snaplink/sso.go:894-960` + `token/jwt/generator.go:52-55`
  (minting) vs `snaplink-audit-governance/internal/auth/auth.go:74-160`
  (verification) + `httpapi/server.go:213` + `sources.go:124`.
- **Requirement level**: RFC 6749 §3.3 (scope), RFC 7519 §4.1 (`iss`),
  RFC 7515 §4.1 (`kid`), the design's own ingest contract (§0 anchors, sink
  section "publisher client_credentials JWT with audit:event:ingest").
- **Verified chain**: (1) `ClientCredentials` never reads `scope` and mints
  claims `{sub, aud, iat, exp, uid}` only — no `scope`/`roles`/`permissions`,
  no `client_id`/`azp`, no `tenant_id`, no `iss`. (2) The sink derives
  permissions **only** from those absent claims (`parseJWT`), so
  `Allows("audit:event:write")` is false → 403. (3) Even with permissions
  fixed, `claims.ClientID` is empty (no `client_id`/`azp`) → the source
  allowlist `sourceAccessAllowed` rejects. (4) In JWKS mode the sink requires a
  `kid` header (`verifier.go:169`) that the IdP never emits; with an issuer
  configured, the missing `iss` fails validation. Both producer connectors
  (aero-id `oauth.go`, vault `token.go`) are correctly built and pass their
  tests — the break is entirely in the IdP claim contract.
- **Impact**: the design's rollout order (§2, phases 1–6) cannot proceed past
  phase 1 with production trust: every relay POST yields 403 → retry → DLQ.
  The only currently working path is the F-1 dev-token bypass. Release blocker
  for the whole audit pipeline.
- **Corrective behavior**: IdP must mint service tokens with `kid` header,
  `iss` (configured issuer), `aud` = resource/audience, and authorization
  claims (`scope` = granted platform scope, e.g. `audit:event:write`,
  `client_id`/`azp`, optional `tenant_id`) — i.e., implement scope validation
  at the token endpoint per RFC 6749 §3.3 and grant only registered scopes
  (scope-matrix Flow 3). Validation: mint a token via `POST /token`, verify
  claims at the sink end-to-end without `AllowDev`.

### F-3 High — Discovery metadata advertises endpoints that do not exist or do not implement the advertised semantics

- **Location**: `snaplink/cmd/server/routes.go:63-77, 663-684`;
  `cmd/server/routes_test.go:41` `TestOIDCDiscovery` (passes, codifying the
  wrong contract).
- **Requirement level**: RFC 8414 §2; OIDC Discovery 1.0 §4 (issuer,
  metadata accuracy).
- **Verified**: `token_endpoint` = `/authenticate` (password credential auth
  via `engine.Authenticate`, `app.go:222`) while the actual RFC 6749 token
  endpoint is `/token`; `authorization_endpoint` = `/login` with
  `response_types_supported: ["code"]` and `grant_types_supported:
  ["authorization_code"]` but **no `/authorize` route exists**; `revocation_
  endpoint` = `/revoke` which **404s**; `userinfo_endpoint` = `/validate`
  (returns `{user_id, provider}` from a session cookie — not OIDC userinfo);
  `token_endpoint_auth_methods_supported` includes `client_secret_basic` and
  `private_key_jwt` which are unimplemented (F-4); device grant is supported
  but `device_authorization_endpoint` is not advertised. Issuer is built from
  the **Host header** plus a port flag (`routes.go:63-72`) — Host-derived,
  no allowlist; if `Host` already carries a port the issuer becomes
  `https://host:port:port`.
- **Impact**: any standards-driven client (SDK, OIDC library) will call
  `/authenticate` for tokens and fail; a `/.well-known` consumer cannot
  discover the real surface. Interoperability failure + issuer confusion
  surface.
- **Corrective behavior**: point `token_endpoint` at `/token`; remove
  `authorization_endpoint`/`response_types`/`authorization_code` until an
  authorize endpoint exists or implement it; implement or drop `/revoke`;
  advertise only implemented auth methods; add `device_authorization_endpoint`;
  derive issuer from a configured allowlist (never raw `Host`). Validation:
  fetch discovery, then GET/POST each advertised endpoint and assert 2xx with
  the advertised semantics; extend `TestOIDCDiscovery` accordingly.

### F-4 High — `/token` implements only client_secret_post while advertising three methods

- **Location**: `snaplink/cmd/server/routes.go:247-300`; `sso.go:894`.
- **Requirement level**: RFC 6749 §2.3.1; RFC 8414 §2 (metadata truthfulness).
- **Verified**: handler reads `client_id`/`client_secret` from
  `r.FormValue` only; no `Authorization: Basic` parsing and no
  `private_key_jwt` (`client_assertion`) path exist anywhere in
  `cmd/server`. Discovery advertises `client_secret_basic`,
  `client_secret_post`, `private_key_jwt`.
- **Impact**: clients choosing Basic per metadata get 401 (`invalid_client`
  shape is actually returned as a plain error for missing form secret →
  `invalid_request`/`invalid_client` confusion); metadata-driven onboarding
  breaks.
- **Corrective behavior**: implement Basic auth parsing (constant-time
  compare, F-7) and/or trim metadata to `client_secret_post`.
  Validation: HTTP test sending Basic credentials to `/token` — either 200 or
  metadata must exclude it.

### F-5 High — Introspection endpoint is an unauthenticated token oracle

- **Location**: `snaplink/cmd/server/routes.go:174-218`; `app.go:655`
  `IntrospectToken`.
- **Requirement level**: RFC 7662 §2.1 (caller MUST authenticate); RFC 9700.
- **Verified**: the handler takes `token` + `client_id` form values; it never
  verifies a client secret or any caller credential. Knowing any registered
  `client_id` (public value) is sufficient to introspect tokens minted for
  that client and learn `active/sub/exp/iat/aud`. Parameters are accepted from
  the query string as well.
- **Impact**: confidentiality of token state (session/jwt metadata) exposed to
  unauthenticated callers; the endpoint doubles as an oracle for token
  validity. Also deviates from RFC 7662 wire behavior expected by clients.
- **Corrective behavior**: require client authentication (same methods as the
  token endpoint, F-4) before serving introspection; reject query-string
  parameters (body only). Validation: introspect without credentials → 401.

### F-6 Medium — Token request binding: query-string parameters accepted, no Content-Type enforcement

- **Location**: `snaplink/cmd/server/routes.go:247-300` (and
  `deviceCode`, `introspect`).
- **Requirement level**: RFC 6749 §3.2.
- **Verified**: `r.FormValue` merges URL query and form body; a POST to
  `/token` with `client_id`/`client_secret`/`grant_type` in the query string
  succeeds.
- **Impact**: client secrets can land in access logs / proxies / referrers;
  ambiguous requests accepted where strict parsing should reject; caching
  intermediaries may treat query-bearing requests as cacheable.
- **Corrective behavior**: parse the body only when
  `Content-Type: application/x-www-form-urlencoded` (reject otherwise), never
  merge query params into token requests. Validation: POST with secret in
  query → 400 `invalid_request`.

### F-7 Medium — Non-constant-time client secret comparison

- **Location**: `snaplink/sso.go:909`.
- **Requirement level**: RFC 6749 §10.12; RFC 9700 §4.4.1.
- **Verified**: `clientSecret != app.ClientSecret` plain string compare.
- **Impact**: timing side channel for online guessing of client secrets
  (local, requires network position; low practical risk but trivially
  fixable).
- **Corrective behavior**: `crypto/subtle.ConstantTimeCompare` (hash both
  sides first to equalize length). Validation: unit test with
  constant-time-aware assertions.

### F-8 Medium — RFC 8628 device flow: confidential clients not authenticated; relative verification_uri

- **Location**: `snaplink/cmd/server/routes.go:908-921`; `sso.go:1117-1159`.
- **Requirement level**: RFC 8628 §3.1 (confidential clients MUST
  authenticate), §3.2 (`verification_uri` absolute).
- **Verified**: `deviceCode` handler passes only `client_id`;
  `InitiateDeviceAuthorization` checks app existence/enabled but never a
  secret. `VerificationURI = "/device/verify"` (relative). No
  `device_authorization_endpoint` in discovery despite the grant being
  advertised.
- **Impact**: device-flow phishing surface; clients cannot resolve the
  verification URI from the response; discovery-driven device clients fail.
- **Corrective behavior**: authenticate confidential clients at
  `/device/code`; emit absolute `verification_uri` (derived from issuer);
  advertise the endpoint. Validation: confidential client without secret →
  401; response `verification_uri` parses absolute.

### F-9 Medium — Sink clock-skew contract from the design is unimplemented

- **Location**: `snaplink-audit-governance` (`internal/service`,
  `internal/domain`, `internal/httpapi` — grep: no `occurred_at` vs sink-time
  comparison; `models.go:360-384` requires presence only).
- **Requirement level**: design draft §1.5 ("Sink clock skew → reject/annotate
  skew >5min") and sink section contract — `[PROPOSED]` in the draft but
  listed as part of the ingest contract producers are told to rely on.
- **Verified**: no skew logic and no test exists; a producer with a wrong
  clock can ledger events with future/past timestamps that ordering and
  legal-hold windows will later mis-read.
- **Corrective behavior**: implement the >5min future-reject / past-annotate
  rule in `Ingest` with explicit status codes; add conformance tests. Note the
  draft marks this Proposed — decide and promote or drop deliberately.

### F-10 Medium — 422→dead immediate mapping not implemented (DLQ semantics drift)

- **Location**: sink `httpapi/server.go:959` `statusForError` (400 for
  ErrInvalid, 422 for ErrSchemaNotFound); aero-id `callback_audit.go`
  `publicationOutcome` (max_attempts=8 → `dead_letter`); aero-vault
  `auditgovernance/relay.go` `retryFact` (**no attempt cap, no dead state —
  retries forever**; legacy `internal/events/event_outbox_relay.go` has
  maxAttempts=10).
- **Requirement level**: design draft §1.4.
- **Verified**: permanent failures (schema/validation) are retried with full
  backoff instead of going dead immediately; the vault's new relay has no
  terminal state at all, so poison events are retried indefinitely and are
  invisible to the draft's DLQ alerting thresholds.
- **Impact**: DLQ alerting and replay tooling from §1.4 cannot work as
  specified; forever-retrying rows keep consuming claim slots (bounded by
  batch, but unbounded over time).
- **Corrective behavior**: classify 4xx-422 as terminal → dead status at each
  relay (aero-id: map to dead immediately; vault: add max-attempts/dead state
  to `audit_governance_outbox`), keep 5xx/429 retrying. Validation: inject a
  422 at the sink, assert the producer row goes dead without backoff.

### F-11 Medium — aero-vault has two parallel audit egress paths (second-truth risk)

- **Location**: `/home/u1/aero-vault/internal/events/audit_sink.go` +
  `event_outbox_relay.go` (static bearer, maxAttempts=10) vs
  `internal/auditgovernance/` (client_credentials, forever-retry).
- **Verified**: both adapters exist; the design's aero-vault section and
  `04-composition.md` reference only the auditgovernance path. Whether the
  legacy sink is still wired in production (`cmd/server/audit_governance.go`
  wires the new runtime) is **Partial** — I did not confirm the legacy relay's
  activation state.
- **Impact**: two egress channels with different auth, retry and DLQ
  semantics double-deliver or silently diverge; the design's "no second
  truth" principle is at risk.
- **Corrective behavior**: confirm and document which path is active;
  retire the legacy sink or migrate it onto the shared connector.
  Validation: config audit + trace of one delete event through both queues.

### F-12 Medium — Scope naming drift and no IdP scope registry

- **Location**: draft `audit-ingestion-design-draft.md:64,201,280,305` and
  `scope-matrix-draft.md` use `audit:event:ingest`; implementation everywhere
  uses `audit:event:write` (aero-id `oauth.go:17`, vault `model.go:17`, sink
  `auth.go` role mapping, `server.go:213`).
- **Verified**: the draft's contract name matches nothing in code; the IdP
  additionally ignores `scope` completely (F-2), so the scope registry that
  the design relies on (scope-matrix Flow 3 "client-bound scopes") does not
  exist at the AS.
- **Impact**: misconfiguration risk during rollout (a binding configured with
  the draft name fails at the strict vault token-source scope check,
  `token.go:153`); scope-matrix grants are unenforceable today.
- **Corrective behavior**: rename the draft/scope-matrix to
  `audit:event:write` (or implement `audit:event:ingest` and change code);
  add scope validation + a scope registry to the IdP. Validation: `pi-batch.py
  rules --check`-style registry diff plus a token-endpoint test asserting
  unregistered scopes are rejected.

### F-13 Low — Empty `refresh_token` field in client_credentials response; no cache directives

- **Location**: `snaplink/cmd/server/routes.go:139-147`.
- **Requirement level**: RFC 6749 §5.1 (no-store; refresh_token MUST NOT be
  included for §4.4.3).
- **Verified**: response always serializes `"refresh_token": ""`.
- **Impact**: harmless today but confuses clients that see a refresh_token
  key; absent cache directives allow intermediaries to cache token responses.
- **Corrective behavior**: omit `refresh_token` for client_credentials; add
  `Cache-Control: no-store, Pragma: no-cache`. Validation: HTTP test on
  headers/body.

### F-14 Low — OpenAPI does not document the OAuth/OIDC surface

- **Location**: `/home/u1/snaplink/api/openapi/snaplink-api.swagger.json` —
  paths cover `/v1/admin/*`, `/v1/audit/*`, `/v1/network/*`; **no** `/token`,
  `/authenticate`, `/.well-known/*`, `/introspect`, `/device/*`, `/validate`;
  `securitySchemes` empty.
- **Impact**: the declared API contract (per `prompts/README.md` canonical
  references) omits the entire identity surface; discovery is the only
  machine-readable contract and it is wrong (F-3).
- **Corrective behavior**: document the token/introspection/device/discovery
  endpoints with an `oauth2` security scheme; wire a CI check that swagger
  paths ⊆ actual routes.

### F-15 Info — Producer connectors: client-side OAuth hygiene is strong

- **Verified** (aero-id `internal/connector/auditgovernance/`): per-tenant
  credentials with reuse rejection (`oauth.go:32-56`), `singleflight` token
  fetch, refresh skew (default 30s, capped at lifetime/2), strict token
  response validation (JSON media type, Bearer-only, `expires_in>0`, size
  bound, CR/LF rejection), no redirects, https-or-loopback endpoint
  enforcement, receipt validation (202 + `ledgered/indexed/archived` +
  event/tenant match + non-conflict). Vault `token.go`: SDK client with RFC
  8707 resource shim, same response validation, scope pinning.
  `publisher_test.go` covers tenant binding, credential reuse, tenantless
  facts, derived source, receipt matching — all pass.
- **Gaps** (Partial): no tests for 401/403/429/5xx classification at the
  connector (design §1.5 rate-class backoff is relay-level), no 422→dead
  test, no token-refresh-failure test.
- **Info only**: bearer tokens are not bound to the relay (no mTLS/token
  binding, RFC 9700 §4.3) — the design's `client_credentials` shared-token
  model relies on TLS in transit; document as accepted risk.

## 4. Priority conformance tests, declared unsupported features, certification evidence

### Priority conformance tests (ordered)

1. **Sink reject-dev-token**: `AUDIT_ALLOW_DEV_AUTH=false` → `dev:demo:compliance`
   bearer on `POST /api/v1/events` returns 401 (F-1).
2. **End-to-end production token**: `POST /token` (client_credentials,
   `audit:event:write`) → sink `POST /api/v1/events` returns 202 with
   `receipt.status=ledgered`, no `AllowDev` (F-2).
3. **Discovery conformance sweep**: every advertised endpoint exists and
   behaves per metadata; `/revoke` either 405→implemented or removed;
   `token_endpoint` = `/token` (F-3).
4. **Token endpoint binding**: Basic-auth attempt; query-string credentials;
   wrong Content-Type; unregistered scope — each rejected or metadata
   trimmed (F-2/F-4/F-6).
5. **Introspection auth**: no-credential call → 401 (F-5).
6. **Skew**: `occurred_at` +6min → rejected; −6min → annotated (F-9).
7. **DLQ classification**: 422 → dead immediately; 503 → backoff retry;
   vault row reaches a terminal dead state (F-10).
8. **Idempotency oracle**: same `event_id`, different canonical content →
   409 conflict; same content → 202 duplicate; `idempotency_key` reuse across
   events → 409 (already implemented — regression test only).
9. **Device flow**: confidential client without secret → 401; absolute
   `verification_uri` (F-8).
10. **Constant time**: secret-compare unit test (F-7).

### Declared unsupported features

- SAML, SCIM, WebAuthn, CAEP/SSF: **not implemented** in any reviewed
  codebase; do not advertise them.
- OAuth authorization-code flow / OIDC `id_token`: **not implemented**
  (no `/authorize`); discovery must stop advertising them until built (F-3).
- `client_secret_basic`, `private_key_jwt`, `/revoke`: not implemented;
  either implement or de-advertise.
- Platform scope registry (`audit:event:ingest` etc. per scope-matrix):
  not implemented at the IdP; only the sink's role→permission mapping exists.

### Certification evidence

- **No OIDF/OAuth certification claimed or evidenced** in any repository
  (no conformance-suite results, no certification listings). This review
  makes no certification claim. If certification is later pursued, the F-3
  discovery inaccuracies and F-4/F-5 deviations are immediate blockers.
- Remaining evidence gaps: no HTTP-level tests for `/token`
  client_credentials success/error paths; no sink test with a
  JWKS-verified token from the real IdP; vault legacy-vs-new audit egress
  activation unconfirmed (F-11); aero-id relay startup wiring of
  `drainPublications` (verified at `callback.go:267`, but max-attempts
  defaults come from `configs/*.yaml:202-203` — `max_attempts: 8`,
  `retry_base: 1s` — confirm per environment).

## 5. Bottom line

The **connectors (aero-id, aero-vault) and the sink's verifier are the
strong parts** of this system: hardened token-response validation, lease-based
outbox relays, digest idempotency, strict JWS verification. The
**IdP (snaplink) is the weak link**: it mints tokens that its own discovery
mis-describes and that the sink cannot accept, authenticates no
introspection callers, ignores scope, and omits `kid`/`iss`. Until F-1 (dev
auth default) and F-2 (claim contract) are fixed, the audit pipeline only
functions on forged tokens; F-3–F-5 must be fixed before any standards-driven
client is pointed at the IdP. The design draft's open decision #5 ("connector
alignment") should be re-scoped: the connectors largely conform; the IdP and
sink contract need the alignment instead.
