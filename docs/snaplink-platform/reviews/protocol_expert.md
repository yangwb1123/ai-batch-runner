Review complete — written to `docs/snaplink-platform/reviews/identity-protocol-review.md`.

## What I did

1. **Applied `prompts/README.md`**: current behavior established from executable code in the four codebases that realize the draft — `/home/u1/snaplink` (IdP), `/home/u1/aero-id` + `/home/u1/aero-vault` (producers), `/home/u1/workspace/demo/snaplink-audit-governance` (sink). Ran the targeted test suites (aero-id connector, snaplink discovery, sink auth/httpapi — all pass).
2. **Scope**: only OAuth 2.0 client_credentials + Bearer + JWT/JWKS + RFC 8707 (client-side) + RFC 7662 introspection + RFC 8628 device flow + OIDC discovery are in scope. SAML/SCIM/WebAuthn/CAEP-SSF: no implementation evidence anywhere — declared unsupported.

## Headline findings

- **Critical F-1**: Sink dev-token auth (`dev:tenant:roles`) is **on by default** (`AUDIT_ALLOW_DEV_AUTH` defaults true) — no signature, full read/export/legal-hold/cross-tenant powers. Only path exercised by e2e tests.
- **Critical F-2**: End-to-end break — the IdP's client_credentials JWT has no `scope`/`roles`/`client_id`/`tenant_id` claims, no `iss`, and no `kid` header; the sink's (hardened) verifier requires all of these → every production token gets 403. The pipeline only works on forged dev tokens today.
- **High F-3**: Discovery metadata lies — `token_endpoint` → `/authenticate` (real token endpoint is `/token`), `authorization_endpoint` → `/login` with `code` response type but **no `/authorize` route**, `revocation_endpoint` → `/revoke` (404), issuer built from the Host header.
- **High F-4/F-5**: Advertises `client_secret_basic`/`private_key_jwt` but implements only `client_secret_post`; introspection endpoint authenticates no caller (RFC 7662 §2.1 oracle).
- **Medium F-8..F-12**: device flow client auth + relative `verification_uri`; sink clock-skew contract unimplemented; 422→dead not mapped (vault relay retries forever); vault has dual audit egress paths; draft's `audit:event:ingest` scope name is stale vs implemented `audit:event:write`.
- **Info F-15**: connectors + sink verifier are the strong parts (per-tenant creds, singleflight, strict token-response validation, digest idempotency).

Includes the full 20-row compliance matrix (spec § / evidence / status / deviation / test), 10 priority conformance tests, and an explicit statement that **no OIDF certification is claimed or evidenced** anywhere.
