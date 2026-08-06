Security review complete — written to `docs/snaplink-platform/v2/reviews/security-review-v2.md` (38KB). Summary of what I verified and found:

## Key correction to the record (deployment evidence)

The protocol_expert reviewed the wrong IdP tree. Deployment assets pin the **deployed IdP** as `/home/u1/workspace/demo/snaplink` (module `yangwb1123/snaplink`): the `/home/u1/sv-sso-deploy-k8s-20-sso-configmap.yaml` matches that tree's `ops/deploy/k8s-distributed/config.yaml` exactly (setup_wizard, bootstrap, sv-sso namespace, `audit: postgres+hash_chain`), and its Dockerfile builds `snaplink/sso-server:sv3`. The "Critical" protocol findings (V2-1 no-kid, V2-3 broken discovery, V2-4 unauth'd introspection, DS-06 non-atomic rotation) apply to the **non-deployed** `/home/u1/snaplink` (opensso/sso) tree. The deployed tree already stamps `kid`+`jti`, has atomic refresh rotation with family-kill, and authenticates introspection clients.

**Consequence**: batch plan B4 rows mix both trees — 3 of 5 rows name files that don't exist in the deployed tree, and "header 补 kid" is already done. Implementing per the plan ships nothing or risks deploying the weak tree (SEC-V2-3).

## Findings (16; all v1 S-series still open)

- **Critical**: dev-token auth still defaults ON (`main.go:45`, verify compose `true`, e2e uses only dev tokens) — S1; deployed IdP mints no `tenant_id`/`roles` → real-token reads/exports 403, T-1.2 blocked — S2; B4 wrong-repo planning (above).
- **High**: sink 401/500 error oracle (S4); CAS conflict → bare 500 + no fsync + readyz blind (F-01); vault retry-forever + poison row flips `Ready()` 503 (S3); aero-id silent drops — **49 `_ =` audit sites, not the 8 in the plan** + dual delivery (F5/F6).
- **New**: **unauthenticated JWKS-fetch amplification** — the verifier fetches JWKS *before* signature verification, so crafted tokens force sink→IdP fetches with no rate limiter (elevates S5); aero-id dead-letter purge runs before any replay tool exists; scope/role→platform escalation coupling (`audit:platform:cross_tenant` one mis-grant from full platform powers).

## Positives re-verified

Hardened JWKS verifier (alg-before-key, kid-unique, no embedded JWK), fail-closed `AllowsClient` binding, `rejectSensitive` + digest dedup, deployed IdP RFC 9068 mint + atomic rotation + authed introspection + scope allowlist, vault activation gate (bindings file fail-closed), deployed IdP audit on postgres+hash_chain.

14 abuse cases confirmed exploitable or latent, 17-item validation plan mapped to G1–G8 gates. Advisory only — no files modified.
