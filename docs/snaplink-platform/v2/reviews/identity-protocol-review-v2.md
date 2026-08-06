# Identity-Protocol Review v2 — IdP OAuth 2.0 / OIDC surface vs v2 contract

> **Reviewer**: identity-protocol reviewer role (`prompts/README.md` applied).
> **Subject**: the v2 contract (`docs/snaplink-platform/v2/audit-ingestion-design-v2.md`,
> `scope-matrix-v2.md`, `implementation-batches.md`, `implementation-gate.md`) vs the
> **current code** of the four identity-protocol-relevant repositories, one review
> round after v1 (`docs/snaplink-platform/reviews/identity-protocol-review.md`).
> **Evidence standard**: Verified (code read at cited symbol/path), Partial,
> Missing, Proposed (plan-only). Checks that ran for this revision:
> `go test ./cmd/server/ -run TestOIDCDiscovery` (IdP, ok — test still codifies the
> v1-wrong contract), `go test ./internal/auth/` (sink, ok); `git diff` inspection of
> the IdP working tree confirmed the uncommitted changes (permission/session/admin
> surface) do not touch the findings below. No full CI of any repository was run;
> static reading covered every path cited.

## 1. Protocol/profile scope and authoritative references

The platform realizes exactly one identity-protocol surface, unchanged from v1:
**OAuth 2.0 client-credentials service tokens (RFC 6749 §4.4) carried as Bearer
(RFC 6750 §2.1), JWT (RFC 7519) / JWS (RFC 7515), JWKS (RFC 7517), RFC 7662
introspection, RFC 8628 device flow, and RFC 8414 / OIDC Discovery 1.0 metadata**,
plus the v2-pinned access-token claim contract (`kid`/`iss`/`aud`/`scope`/
`client_id`/`azp`/`tenant_id`/`roles`). SAML, SCIM, WebAuthn, CAEP/SSF: **no
implementation evidence in any reviewed tree**; nothing in v2 changes that.

**Repo identity (critical to get right for the batch plan)**. Two distinct
"snapLink" codebases exist:

| Repo | Module | Role in platform | Protocol surface |
|---|---|---|---|
| `/home/u1/snaplink` | `github.com/opensso/sso` | **Deployed IdP** (per v1 review + F-3 fix location `cmd/server/routes.go`) | `cmd/server/routes.go`, `sso.go:894-960`, `token/jwt/generator.go` |
| `/home/u1/workspace/demo/snaplink` | `github.com/yangwb1123/snaplink` | **SDK / sso-server wiring** (separate tree; the *published* module `v0.0.0-20260804051506` is a snapshot of this) | `interfaces/sso/*`, `protocols/oauth/*`, `infrastructure/defaultimpl/issue_payload.go` (`buildAccessPayload`) |

The v2 batches mix the two: `implementation-batches.md` row 1 (B4-1) and
`audit-ingestion-design-v2.md` §snaplink list cite
`infrastructure/defaultimpl/issue_payload.go buildAccessPayload` **together with**
`token/jwt/generator.go:52-55` and `sso.go:894-960` — but `buildAccessPayload`
exists **only** in the SDK tree, while `sso.go:894-960` and
`cmd/server/routes.go` exist **only** in the deployed IdP. The deployed IdP mints
tokens in `sso.go ClientCredentials` via `token/jwt/generator.go`; it has no
`buildAccessPayload`. Finding V2-5.

Authoritative references (normative): RFC 6749 §§2.3.1, 3.2, 3.3, 4.4.3, 5.1,
5.2, 6, 10.12; RFC 6750 §2.1; RFC 7515 §4.1; RFC 7519 §4.1; RFC 7517; RFC 7662
§2.1–2.2; RFC 8414 §2; RFC 8628 §§3.1–3.2; RFC 9700 (OAuth Security BCP)
§§4.4.1, 4.7; v2 contract: `audit-ingestion-design-v2.md` §1.7 (production trust
posture), §1.2 (claim contract), `scope-matrix-v2.md` Flow 2 (IdP endpoint
contract). **No OIDF certification is claimed or evidenced anywhere; this review
makes none.**

## 2. Compliance matrix

Status: **V** = Verified compliant, **P** = Partial, **M** = Missing,
**D** = Deviation, **X** = no implementation. "Batch" = v2 batch that must close
the gap; "Test" = check that exists or must be added.

| # | Spec § / contract | Requirement | Implementation evidence (current) | Status | Deviation / test |
|---|---|---|---|---|---|
| 1 | RFC 6749 §4.4 | client_credentials grant | IdP `sso.go:894` `ClientCredentials`; `/token` handler `routes.go:249` | V | engine-level; no HTTP test (row 12) |
| 2 | RFC 6749 §2.3.1 / v2 F-4 | client auth: post only until Basic implemented | `/token` reads `FormValue("client_secret")` only; **no `Authorization: Basic` parsing anywhere in `cmd/`**; discovery advertises `client_secret_basic` + `private_key_jwt` (`routes.go:676`) | M | over-advertisement; B4-3/B4-4 |
| 3 | RFC 6749 §3.2 | body-only params, `application/x-www-form-urlencoded` | `r.FormValue` on all of `/token`, `/introspect`, `/device/code` — merges query string; no Content-Type check | M | B4-4 (T-8b/c) |
| 4 | RFC 6749 §3.3 / v2 S2 | `scope` parameter read + registry | `token` handler never reads `scope`; no scope registry in IdP; `scopes_supported` advertises openid/profile/email/phone that the AS does not process | M | B4-1/B4-2 (T-8a/d) |
| 5 | RFC 6749 §5.1 | no-store; no refresh_token for cc | `tokenResponse` `routes.go:139-147`: **no Cache-Control/Pragma**; always serializes `"refresh_token": ""` | M | B4-4 (T-8e) |
| 6 | RFC 6749 §5.2 | token error codes | `tokenErrorResponse` `routes.go:149`; invalid_request/invalid_client/unsupported_grant_type + device error mapping (`authorization_pending`/`access_denied`/`expired_token`) | V | no HTTP tests (row 12) |
| 7 | RFC 7519 §4.1 / RFC 7515 §4.1 / v2 claim contract | JWT claims `iss`/`client_id`/`azp`/`tenant_id`/`roles`/`scope`; header `kid` | IdP `sso.go:925-936` mints `{sub, aud, iat, exp, uid}` only (`Issuer` field exists in `token/jwt/generator.go:23` but is never set by `ClientCredentials`); header `{alg, typ}` only (`generator.go:52-55`) — **no kid** | M | B4-1 (T-8a). Sink consumes the contract already (`auth.go:106-141`: client_id/azp consistency, tenant_id, roles) and **JWKS mode requires kid** (`verifier.go:167`) |
| 8 | RFC 7517 | JWKS publication | `GET /.well-known/jwks.json` → `engine.JWKSHandler()`; v1 verified keys carry kid | P | keys have kid but minted JWTs don't reference one (row 7) |
| 9 | RFC 7662 §2.1 | introspection caller MUST authenticate | `routes.go:174-192`: `token` + `client_id` form values only; **no secret/assertion verified** — any caller knowing a public client_id gets an oracle | M | B4-4 (T-9). **Note**: the SDK tree's `protocols/oauth/handle_introspect.go` already implements full caller auth — see V2-11 |
| 10 | RFC 7662 §2.2 | `{active:false}` on invalid; no extra metadata | `IntrospectTokenResponse` active-only on failure (engine) | V | engine-level |
| 11 | RFC 8414 §2 / OIDC Disc / v2 F-3 | metadata truthfulness; issuer from config allowlist | `routes.go:63-77,663-684`: `token_endpoint=/authenticate` (password endpoint, not the RFC 6749 `/token`); `authorization_endpoint=/login` + `response_types:["code"]` with **no `/authorize` route**; `revocation_endpoint=/revoke` → **404 (no route)**; `userinfo_endpoint=/validate` (session-cookie check, not OIDC userinfo); issuer = `h.issuer` when configured, else **Host-header-derived** with port bug (`fmt.Sprintf("%s://%s:%d", scheme, r.Host, cfg.Port)` — duplicates port when Host carries one) | M | B4-3 (T-2). `TestOIDCDiscovery` (`routes_test.go:41-56`) and `TestOIDCDiscoveryEndpoint` (`:497-524`) pass, **codifying the wrong contract incl. the port bug** (`http://localhost:8080:0/login`, `:516`) |
| 12 | RFC 8628 §3.1/3.2 | confidential client auth at /device/code; absolute verification_uri; advertise endpoint | `routes.go:908-921`: `client_id` only, no secret; `sso.go:1150` `verificationURI := "/device/verify"` (relative); **no `device_authorization_endpoint` in discovery** (grant advertised at `:672`) | M | B4-3/B4-4 (T-8c; v1 F-8) |
| 13 | RFC 6749 §10.12 / RFC 9700 §4.4.1 | constant-time secret compare | `sso.go:908` `clientSecret == "" \|\| clientSecret != app.ClientSecret` — plain string compare | M | B4-4 |
| 14 | v2 §1.7 (S1) | `AUDIT_ALLOW_DEV_AUTH` default **false**; dev-only config refuses startup; manifest scan | sink `cmd/audit-api/main.go:45` `boolEnv("AUDIT_ALLOW_DEV_AUTH", true)` — **default still true**; `parseDevToken` (`auth.go:58-79`) accepts `dev:<subject>:<roles>` unsigned; no fail-startup check | M | B1-1 (T-1.1). `go test ./internal/auth/` passes (no reject-dev test exists) |
| 15 | v2 claim contract (S2) | tokens lacking `tenant_id`/`roles` rejected at sink | sink `server.go:850` tenant-consistency gate; `auth.go:110-114` consumes claims | V | contract enforced sink-side; IdP cannot yet satisfy it (row 7) |
| 16 | v2 Flow 3 scope name | `audit:event:write` enforced | sink `httpapi/server.go:213,248` `require("audit:event:write")`; vault `internal/auditgovernance/model.go:17` + strict check `token.go:153`; aero-id `connector/auditgovernance/oauth.go:21` | V | **scope-matrix-v2 "唯一 Verified" claim confirmed** (B3-5 "no code change" is accurate) |
| 17 | v2 F-8 | 401 → token invalidation at connectors | aero-id connector: **no 401/unauthorized classification** found in `internal/connector/auditgovernance/` (grep: only scope constant) | M | B2-3 (T-10.1) |
| 18 | v2 F-01 / B1-2 | snapshot-conflict bounded retry; readyz probes store | sink `internal/store/postgres.go:101,114` `ErrSnapshotConflict` exists; **HTTP layer has no retry** (`statusForError` `httpapi/server.go:959-978` maps domain errors, conflict not wired); `readyz` (`:161-178`) probes **archive only**, never the store | P | B1-2 (T-6) |
| 19 | v2 B1-3 | relational ledger (migration-001) wired or capacity envelope | `migrations/001_control_plane.sql` defines tenants/sources/event_schemas; **no Go code references it** (grep of `internal/`, `cmd/` empty) | M | B1-3 |
| 20 | v2 B1-6 | single supported inbound path; orphan components | `cmd/audit-outbox-relay/` and `cmd/audit-kafka-consumer/` both import `internal/outbox` with real deliverers — built entry points, no declared topology | P | B1-6 |
| 21 | v2 F-06/F13 | read self-audit rows | sink: not present (v2 lists as B1-4/B1-5) | M | B1-4 (T-12) |
| 22 | OpenAPI (canonical contract) | identity surface documented | `/home/u1/snaplink/api/openapi/snaplink-api.swagger.json`: paths are `/v1/admin/*`, `/v1/audit/*` etc.; **no `/token`, `/authenticate`, `/.well-known/*`, `/introspect`, `/device/*`**; `securitySchemes` absent | M | v1 F-14, still open |

## 3. Findings

Sorted by severity. "Batch" = the v2 batch item that must close it; evidence
labels per `prompts/README.md`.

### V2-1 Critical — IdP still mints JWTs the sink cannot accept (v1 F-2, batch B4-1, all open)

- **Location**: `/home/u1/snaplink/sso.go:925-936` (`ClientCredentials`),
  `token/jwt/generator.go:18-28,52-55` (claims struct + header); sink
  `internal/auth/auth.go:106-141`, `internal/auth/verifier.go:167,198`.
- **Requirement level**: v2 §1.2 claim contract (kid/iss/aud/scope/client_id/
  azp/tenant_id/roles) — pinned, `[v2]`; RFC 7515 §4.1; RFC 7519 §4.1.
- **Verified**: current minted claims = `{sub, aud, iat, exp, uid}`; header =
  `{alg, typ}` (no kid); `Issuer` claim field exists but is never populated on
  this path; no scope/client_id/azp/tenant_id/roles. The sink's `parseJWT`
  requires `client_id` (strict identity claim with azp consistency), consumes
  `tenant_id` (empty → 422 at `server.go:850` for write scope), `roles`; JWKS
  verification rejects any token without `kid` (`verifier.go:167`). Therefore
  **every production trust path still 403/422s end-to-end**; `AUDIT_ALLOW_DEV_AUTH`
  remains the only working path (V2-2).
- **Corrective behavior**: in the **deployed IdP tree** (`opensso/sso`):
  populate `Issuer` from config, add `scope`, `client_id`, `azp`, `tenant_id`
  (client-bound), `roles`; emit `kid` in the header matching the JWKS key; per
  contract, tokens lacking `tenant_id`/`roles` must not be issued for
  audit-bound clients. Validation: T-8(a) HTTP test + T-1.2 cross-repo e2e.

### V2-2 Critical — Dev-token auth still default-ON (v1 F-1, batch B1-1, open)

- **Location**: sink `cmd/audit-api/main.go:45`; `internal/auth/auth.go:52-79`.
- **Requirement level**: v2 §1.7 (S1) — default false, dev-only config refused,
  manifest scan; RFC 6749 §10 (auth bypass).
- **Verified**: flag default remains `true`; `parseDevToken` is unsigned;
  `permissionsForRoles` grants `dev:...:compliance` full read/export/legal-hold
  powers. No startup rejection of dev-only configuration exists; no CI manifest
  scan exists; `test/e2e/fullstack.sh` still drives dev tokens.
- **Corrective behavior**: per batch B1-1 (flip default, fail-closed startup,
  manifest scan, T-1.1 reject test). This and V2-1 are the joint P0 trust-path
  gate G1.

### V2-3 High — Discovery still advertises a non-existent surface (v1 F-3, batch B4-3, open)

- **Location**: `/home/u1/snaplink/cmd/server/routes.go:63-77,663-684`;
  `routes_test.go:41-56,497-524`.
- **Requirement level**: RFC 8414 §2; OIDC Discovery 1.0 §4; v2 IdP endpoint
  contract (`scope-matrix-v2.md` Flow 2).
- **Verified**: `token_endpoint=/authenticate` (real RFC 6749 endpoint is
  `/token`); `authorization_endpoint=/login` with `response_types:["code"]`
  but no `/authorize` route; `revocation_endpoint=/revoke` 404s; `userinfo_
  endpoint=/validate` is a session-cookie check; auth methods over-advertised
  (V2-4 row 2); device grant advertised without `device_authorization_endpoint`;
  issuer Host-derived with **port-duplication bug codified by the passing test**
  (`TestOIDCDiscoveryEndpoint` asserts `http://localhost:8080:0/login`).
- **Corrective behavior**: per B4-3: token_endpoint=/token; drop
  authorization/response_types/authorization_code until a real authorize flow
  exists; implement or remove /revoke; advertise only `client_secret_post`
  until Basic exists; add `device_authorization_endpoint`; issuer from config
  allowlist with no Host fallback; rewrite both tests. Validation: T-2 sweep —
  GET each advertised endpoint, assert 2xx/405, never 404.

### V2-4 High — Introspection is still an unauthenticated token oracle (v1 F-5, batch B4-4, open)

- **Location**: `/home/u1/snaplink/cmd/server/routes.go:174-192`.
- **Requirement level**: RFC 7662 §2.1; RFC 9700.
- **Verified**: handler requires only `token` + `client_id` form values; no
  secret/assertion verification; query-string parameters accepted. Any party
  with a public client_id learns active/sub/exp/iat/aud of tokens.
- **Corrective behavior**: require client auth (post, then Basic per B4-4);
  body-only parsing. Validation: T-9 — no-credential introspect → 401.
- **Note (V2-11)**: the SDK tree already implements this correctly
  (`protocols/oauth/handle_introspect.go` `authenticateIntrospectClient`,
  oracle-collapsed 401s, JWT-assertion branch) — a migration path exists.

### V2-5 High — Batch plan mixes two different "snaplink" codebases (plan defect)

- **Location**: `implementation-batches.md` rows B4-1/B4-2/B4-3/B4-4;
  `audit-ingestion-design-v2.md` §snaplink task table; `implementation-gate.md`.
- **Requirement level**: plan executability (G1/G5 gates depend on it).
- **Verified**: `infrastructure/defaultimpl/issue_payload.go buildAccessPayload`
  exists only in `/home/u1/workspace/demo/snaplink` (module
  `github.com/yangwb1123/snaplink`, SDK); `cmd/server/routes.go`, `sso.go:894-960`,
  `token/jwt/generator.go:52-55` exist only in `/home/u1/snaplink` (module
  `github.com/opensso/sso`, deployed IdP). The deployed IdP has **no**
  `buildAccessPayload`; the SDK has **no** `cmd/server/routes.go` or
  `sso.go:894-960`. An implementer following the cited paths will edit the wrong
  tree or declare the task done on a tree that is not the deployed IdP.
- **Corrective behavior**: pin the deployed IdP repo (module `opensso/sso`) as
  the B4 target with its real paths (`sso.go` `ClientCredentials`,
  `token/jwt/generator.go`, `cmd/server/routes.go`); either drop the
  `issue_payload.go` reference or mark it as the SDK-side equivalent if the
  platform plans a migration to the SDK-based `sso-server`. Validation: batch
  review — every cited path resolves in the target tree.

### V2-6 Medium — `/token` binding: query-string credentials, no Content-Type, scope ignored (v1 F-6, batch B4-1/B4-4, open)

- **Location**: `/home/u1/snaplink/cmd/server/routes.go:247-300`.
- **Requirement level**: RFC 6749 §3.2/§3.3; RFC 9700.
- **Verified**: `r.FormValue` merges query and body; no Content-Type check; the
  handler never reads `scope` (B4-2 scope registry has no enforcement point).
- **Corrective behavior**: body-only parse with Content-Type enforcement; read
  and validate `scope` against the registry (400 `invalid_scope` for
  unregistered values). Validation: T-8(b)/(c)/(d).

### V2-7 Medium — cc response: no no-store, empty `refresh_token` emitted (v1 F-13, batch B4-4, open)

- **Location**: `/home/u1/snaplink/cmd/server/routes.go:139-147`.
- **Requirement level**: RFC 6749 §5.1 / §4.4.3.
- **Verified**: `tokenResponse` sets only Content-Type; always serializes
  `"refresh_token": ""` for client_credentials.
- **Corrective behavior**: `Cache-Control: no-store, Pragma: no-cache` before
  body write; omit refresh_token for cc. Validation: T-8(e).

### V2-8 Medium — Device flow: unauthenticated confidential clients, relative verification_uri, no metadata (v1 F-8, batch B4-3/B4-4, open)

- **Location**: `/home/u1/snaplink/cmd/server/routes.go:908-921`;
  `sso.go:1117-1159`.
- **Requirement level**: RFC 8628 §3.1/§3.2; RFC 8414 §2.
- **Verified**: `client_id` only; `verificationURI := "/device/verify"`;
  discovery omits `device_authorization_endpoint` while advertising the grant.
- **Corrective behavior**: authenticate confidential clients; absolute
  verification_uri; advertise the endpoint. Validation: T-8(c) + metadata sweep.

### V2-9 Medium — Sink resilience items unimplemented (v1 F-9/F-10 partial, batches B1-2/B1-3, open)

- **Location**: sink `internal/httpapi/server.go:161-178` (readyz archive-only),
  `:959-978` (statusForError, no conflict retry); `migrations/001_control_plane.sql`
  (unwired).
- **Requirement level**: v2 F-01 (bounded jitter retry on `ErrSnapshotConflict`;
  readyz must probe store), B1-3 (relational ledger or capacity envelope).
- **Verified**: `ErrSnapshotConflict` exists store-side (`postgres.go:101,114`)
  but no retry path at the HTTP layer; readyz never probes the store (PG dead →
  still 200); migration-001 has no Go wiring (dead migration).
- **Corrective behavior**: per B1-2/B1-3. Validation: T-6 (PG CI), stop-PG →
  readyz 503 drill.

### V2-10 Medium — No IdP scope registry (v1 F-12, batch B4-2, open)

- **Location**: `/home/u1/snaplink` (no scope handling anywhere in
  `cmd/server/` or `sso.go`).
- **Requirement level**: RFC 6749 §3.3; scope-matrix-v2 Flow 2/3.
- **Verified**: `scopes_supported` advertises openid/profile/email/phone which
  the AS ignores; no `invalid_scope` path. (v1's `audit:event:ingest` name drift
  is resolved contract-side — everything now enforces `audit:event:write`,
  confirmed at sink `server.go:213,248`, vault `model.go:17`/`token.go:153`,
  aero-id `oauth.go:21`.)
- **Corrective behavior**: per B4-2 (registry of matrix v2 table; unregistered →
  400). Validation: T-8(d).

### V2-11 Info — SDK tree already implements several B4 items (migration option)

- **Verified**: `protocols/oauth/handle_introspect.go` (full caller auth,
  RFC 7521 assertion branch, oracle-collapsed 401), `handle_revoke.go`
  (RFC 7009), truthful discovery base (`server_discovery_config.go`:
  token_endpoint=/token, store-conditioned grants), DPoP/mTLS sender
  constraints, constant-time paths. Gaps in the SDK too: `buildAccessPayload`
  lacks tenant_id/roles (B4-1 pending), no scope registry (`invalid_scope`
  unused), **`device_authorization_endpoint` still not advertised**
  (`cmd/sso-minimal/surface.go:20` even pins its absence in a test), issuer
  still request-derived unless `WithIssuer`.
- **Recommendation**: if the deployed IdP is scheduled to move onto the
  SDK-based `sso-server`, B4 should be scoped there once; otherwise B4 must
  target the deployed IdP tree only (V2-5).

### V2-12 Info — aero-vault B3-5 "no code change" claim is accurate; aero-id B2-3 still open

- **Verified**: vault `RequiredScope` strict enforcement (`token.go:153`:
  exactly `audit:event:write`) + `model.go:17`; aero-id connector scope name
  correct but no 401→token-invalidation classification in
  `internal/connector/auditgovernance/` (T-10.1 pending).

### V2-13 Info — OpenAPI still omits the identity surface (v1 F-14)

- **Verified**: `api/openapi/snaplink-api.swagger.json` has no `/token`,
  `/authenticate`, `/.well-known/*`, `/introspect`, `/device/*`; no security
  schemes. The only machine-readable contract for the OAuth surface remains
  the discovery document, which is wrong (V2-3). Add to the B4 batch (or a
  docs task): document endpoints + `oauth2` scheme; CI check swagger ⊆ routes.

## 4. Priority conformance tests, declared unsupported features, certification evidence

### Priority conformance tests (ordered by gate)

1. **T-1.1** (G1): `AUDIT_ALLOW_DEV_AUTH=false` → `dev:platform:platform-admin`
   → 401 on all sink routes (V2-2).
2. **T-1.2** (G1): cross-repo e2e — IdP `/token` cc with claim contract →
   sink ingest 202 `ledgered` → read 200 → export 202, no dev auth (V2-1).
3. **T-8(a)** (G1): `/token` response header kid + claims
   iss/aud/scope/client_id/tenant_id/roles (V2-1).
4. **T-2** (G5): discovery sweep — every advertised endpoint 2xx/405, never
   404; `token_endpoint == "/token"`; issuer not Host-derived (V2-3).
5. **T-9** (G5): introspect without credentials → 401; with wrong secret → 401
   (V2-4).
6. **T-8(b/c/d/e)** (G5): query-string credentials → 400; wrong Content-Type →
   400; unregistered scope → 400 `invalid_scope`; cc response has no
   refresh_token + no-store (V2-6/V2-7/V2-10).
7. **T-6** (G2): PG concurrent ingest → no 500; stop PG → readyz 503 (V2-9).
8. **T-10.1** (G3): revoke connector token → refresh on next call, ≤24h
   staleness eliminated (V2-12).
9. **Device flow** (from v1 §4): confidential client without secret → 401;
   absolute verification_uri; `device_authorization_endpoint` advertised
   (V2-8).
10. **Constant time**: secret-compare unit test (V2-1 row 13).

### Declared unsupported features (unchanged from v1, still accurate)

- SAML, SCIM, WebAuthn, CAEP/SSF: not implemented in any reviewed tree; do not
  advertise.
- OAuth authorization-code / OIDC id_token flow: **no `/authorize`**; discovery
  must stop advertising `authorization_code`/`code` until a real flow exists
  (V2-3).
- `client_secret_basic`, `private_key_jwt`, `/revoke`: unimplemented at the
  deployed IdP; implement or de-advertise (V2-3/V2-4).

### Certification evidence

- **No OIDF/OAuth certification claimed or evidenced** in any repository (no
  conformance-suite results, no certification listing, current or historical).
  This review makes no certification claim. If certification is ever pursued,
  V2-3 (discovery accuracy) and V2-4 (introspection auth) are immediate
  blockers, and the RFC 8628/RFC 7662/RFC 6749 HTTP-level test surface must
  exist first (it does not today).
- Remaining evidence gaps: no HTTP-level `/token` success/error tests anywhere
  in the deployed IdP (only engine-level `core/service/token_test.go`); no
  sink test verifying a real IdP-issued JWKS token (all e2e uses dev tokens);
  orphan relay components' activation state (B1-6) unconfirmed at runtime;
  `h.issuer` config wiring (`cmd/server/config.go`) read but not exercised in
  tests.

## 5. Bottom line

The v2 contract (claim contract, S1 posture, T-1.1/T-1.2/T-2/T-8/T-9 gates) is a
faithful, testable pin of everything v1 found broken — and the sink/producers
side of it is already verified correct (`audit:event:write` enforcement, claim
consumption, kid-requiring verifier). **Nothing in the B4 IdP batch is
implemented yet**: every v1 F-2..F-8/F-13 defect is still present in the
deployed IdP working tree, and the two IdP discovery tests still codify the
wrong contract (they pass). The batch plan itself has one plan-level defect
that will misdirect implementers (V2-5: two distinct "snaplink" codebases
conflated). G1 cannot pass until V2-1 + V2-2 are closed; G5 until V2-3/V2-4 and
the T-8 rows. The SDK tree is the bright spot: it already implements most of
B4-4 (introspection auth, revocation, truthful discovery) — if the platform is
headed there, B4 should be re-scoped to one tree, not both.
