Security review complete. Output: `docs/snaplink-platform/connections/reviews/security_engineer.md` (~330 lines).

## What I did

**Grounded verification** (read `prompts/README.md` + snaplink `AGENTS.md` §3 security invariants, then independently re-verified 10 key claims in the 6 on-disk repos):

- ✅ **Verified**: `scope.go` rule ② — empty `AllowedScopes` ⇒ UNRESTRICTED (fail-open issuance)
- ✅ **Verified**: snaplink has **no Validate service** in `proto/` while aero-id defaults to gRPC validation (`configs/config.yaml:100` `jwks_url: ""`)
- ✅ **Verified**: vault `requireAdmin` implicitly allows when registry disabled (`admin.go:456`)
- ✅ **Verified**: governance `createSchema` sets `Active=true` immediately (no approval chain); ingest tenant comes from claims, body `tenant_id` ignored
- ✅ **Verified**: aero-im self-signs JWTs from PEM keys (third signing party)
- ✅ **Verified**: snaplink snapshot/restorer path (317 lines) has **zero** `audit.Record` calls
- ✅ **Verified**: console nginx full-segment passthrough of browser admin token

## Key findings (15 total, Critical/High/Medium/Low + 2 Info)

| # | Severity | Finding |
|---|---|---|
| F-1 | **Critical** | Console browser admin token direct-connect — one XSS = full platform admin (hard-gate violation) |
| F-2 | **Critical** | Scope registry missing — empty-allowlist clients issue unlimited scopes |
| F-3 | High | Governance schema single-party registration, immediately active |
| F-4 | High | Service-account privilege bloat (`cross_tenant` read + hardcoded tenantId; vault holds full admin scope) |
| F-5 | High | Vault admin gate implicitly allows when registry off |
| F-6 | High | Residual authorization (no cascade delete; department sync add-only) |
| F-7 | High | aero-id default auth path points to nonexistent gRPC service |
| F-8 | High | snaplink admin HTTP audit gaps incl. zero-audit snapshot restore path |
| F-9 | High | aero-im self-held PEM keys — third signing domain, boundary unwritten |

Each finding has evidence, exploit preconditions/steps, impact, remediation, and a concrete regression test. Includes a 24-row abuse-case table (spoofing/replay/cross-tenant/header forgery/injection/exhaustion/leakage), verified positive controls (9), residual risks, and a P0–P3 validation plan — with an explicit constraint section preserving the oracle-safe invariants (uniform `invalid_grant`, byte-identical `unsupported_provider`, admin 401 realm, anti-enumeration).
