Security review complete: **`docs/snaplink-platform/reviews/security-review.md`** (31KB, advisory only, no files modified).

## What I did

Applied `prompts/README.md` evidence rules: every finding traced to executable code, plus an **empirical exploit harness** run against the real sink (bare config accepted, `dev:platform:platform-admin` → platform=true + cross-tenant read 200, 1h clock-skew event accepted). Re-ran sink auth/httpapi test suites (pass). No memory history retrieved — current code was the only reliable evidence.

## Headline findings (15 total, 2 Critical / 4 High)

1. **S1 Critical — dev-token auth on by default, no trust source required** (`AUDIT_ALLOW_DEV_AUTH` defaults true; `ValidateConfiguration` passes with dev-auth alone). Verified exploit: any unauthenticated caller mints `dev:platform:platform-admin` and reads/exports/legal-holds **any tenant's** audit data, approves restores, rewrites retention. The pipeline works *only* on forged tokens today.
2. **S2 High — IdP tokens can't establish tenant context at the sink**: access payload has `iss/aud/scope/client_id/jti` but **no `tenant_id`/`roles`** → every read/export/legal-hold path 403s with real tokens (correcting protocol_expert's F-2 claim detail: iss/kid/scope *do* exist; the operative gaps are tenant_id/roles).
3. **S3 High — vault relay has no terminal state**: 409/422 poison events retry forever and one row permanently flips `Ready()` → restart loops; plus the draft's deterministic event_id design would enable **event-id squatting** (availability attack).
4. **S4 High — sink leaks raw error text on 500s (S3/Vault paths, bucket names) and auth failures are an oracle** — violates the platform's own oracle-safe discipline.
5. **S5 Med-High — no read-path rate limits, O(ledger) scans, uncached JWKS fetch per request** (DDoS amplifier on the IdP).

Also: dual vault egress with static unredacted L2 tokens (S6), plaintext gRPC (S7), stream pollution + forged `occurred_at` (S8), missing read self-audit + forgeable console log (S9), volatile-by-default IdP audit + fsync-less sink store (S10), non-cryptographic XOR "HMAC" in `defaulttoken.JWTIssuer` (S11, latent).

Positive controls verified (9 areas): sink JWT verifier hardening, fail-closed source binding, sensitive-field rejection + AAD-bound field encryption, digest idempotency, strict relay token/receipt validation, secrets fail-fast, atomic admin self-audit.

Each finding carries evidence, exploit steps, impact, remediation, and a regression test; the report includes the full asset/trust-boundary map, a 20-row abuse-case table (spoofing/replay/cross-tenant/proxy-header/resource-exhaustion/leakage), residual risks, and a P0–P3 validation plan.
