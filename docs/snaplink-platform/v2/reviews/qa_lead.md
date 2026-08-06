QA review complete. Full report: **`docs/snaplink-platform/v2/reviews/qa-review-v2.md`** (19KB), summary: `reviews/qa_lead.md`. All claims below are from commands I actually ran this revision.

## 1. Test inventory & commands run (measured)

| Repo @ HEAD | Command | Result |
|---|---|---|
| sink @ fb4fd97 | `go test ./...`, `cli.py ci`, `go test -race` (auth/httpapi) | PASS (150 tests) |
| sink | PG-gated store tests vs real Postgres (fresh `audit_test` DB, migrations 001+004) | **PASS incl. `-race`** (optimistic-lock conflict, concurrent-reads race) |
| IdP 部署仓 @ ac5d367 | `go test ./cmd/server/ ./token/jwt/`, `go test ./...` | PASS (1557 tests) — **`TestOIDCDiscovery`/`TestOIDCDiscoveryEndpoint` both PASS, codifying the wrong contract** (`/authenticate`, `8080:0` port bug) |
| aero-vault @ 35ff4ce | `go test ./internal/auditgovernance/...` | PASS (12 tests) |
| aero-id @ 756b65b | `go test ./internal/connector/auditgovernance/... ./internal/audit/...` | PASS (7 tests) |
| aero-im @ fbff3d5 | default audit suite | 7 passed; 30 ignored |
| aero-im | ignored audit suite vs **fresh-migrated PG (238 migrations)** | **30/30 PASS** |
| aero-im | same vs stale local `aero` DB (migration 176) | **18/22 FAIL** — fixture staleness only |
| console | static inventory (113 test files) | audit tests cover local ring only |

## 2. Requirement→test matrix (12 groups)

**Green (measured):** sink dedup unit (202 Duplicate/409), sink PG store layer, aero-im 37/37 baseline (fresh fixture), vault strict-scope test, aero-id binding coverage.
**Partial:** T-6 — store level green; HTTP-level concurrent-ingest test, readyz store probe, and **any sink CI workflow** missing.
**Red (no test AND no implementation):** T-1.1 route sweep + manifest scan, T-1.2 e2e (deployed IdP mints `{sub,aud,iat,exp,uid}`, no `kid`; sink `verifier.go:167` requires kid), T-2 sweep, T-3 dead state (migration 0039 has no status column; `retryFact` has no permanent class), T-4 (vault `uuid.NewString()` ×3, aero-id `idgen.NewID()` ×3), T-5 (8 `_ =` sites; metric exists with 0 call sites), T-8(a–e), T-9 (introspect unauthenticated; `r.FormValue` accepts query creds), T-10.1/10.2, T-11, T-12 (read self-audit absent), D1 (`Ready()` flips 503 at maxLag), L0, parity.

## 3. Key findings (severity / exact test to add)

1. **Critical** — G1 untestable end-to-end. Add: `TestClientCredentialsJWTHeaderAndClaims` (kid/iss/scope/client_id/tenant_id/roles), `scripts/mint-token.sh` + dev-token-free `fullstack.sh`, and a dual-repo attribution test — `buildAccessPayload` lives only in the SDK tree, `sso.go`/`generator.go` only in the deployed IdP (V2-5 confirmed by my greps).
2. **Critical** — S1 triple missing (default `true` at `main.go:45`, `ValidateConfiguration` passes dev-only when `AllowDev=true`, `docker-compose.verify.yml` sets true). Add: HTTP-level `TestDevToken401OnEveryRoute`, `TestValidateConfigurationRejectsDevOnlyTrust`, manifest scan check.
3. **High** — aero-im 37/37 is fixture-dependent (measured both directions). Add documented `make audit-pg` fixture script.
4. **High** — T-6: add `TestHTTPConcurrentIngestSameDigest`/`DiffDigest`, `TestReadyzStoreProbe` (PG down → 503), sink `.github/workflows/ci.yml`.
5. **High** — T-12: add `TestQueryAppendsReadSelfAudit`; blocks B6.
6. **High** — vault F3/H1: add `TestPermanentFailureReachesDeadState`, `TestReadyzStays200WithBacklogAndPoison`, clock-injected D1 drill.
7. **Medium** — B4-3 must **rewrite/delete** the two discovery tests, not just add a sweep (they're defect regression locks, both PASS today).
8. **Medium** — aero-id F5/F6/F8/F9: four tests specified (mock-failure counter, single-delivery p99 drill, 401-refresh count assertion, replay redelivery).
9. **Medium** — console path drift (`lib/services/portal_api.dart` doesn't exist; actual `lib/api/portal_api.dart`); add server-sourced audit widget test with localStorage-forgery scenario.

## 4. Prioritized scenarios

23 scenarios across batches (full table in report): P0 = G1 trust path (claims/e2e/401 sweep), T-6 concurrency + readyz, T-12, T-3/D1, T-5; P1 = discovery sweep, scope registry, replay/401, L0 persistence, moderation priority, console read path; plus race (dual-writer digest), recovery (crash between send/mark-sent, power loss fsync), boundary (dead-row exclusion, moderation vs backlog), bench (`BenchmarkIngest` envelope).

## 5. CI gaps, flake risks, exit criteria

- **Gaps:** sink has **no CI workflow** (PG tests have nowhere to run); vault/aero-id CI lack PG service and `-race`; aero-im CI PG job is the only model (migration chain smoke + rolling-upgrade regression + full `--ignored` run).
- **Flake risks:** stale local fixtures (measured 18/22 fail on migration-176 DB); sink PG tests share one snapshot row (correctly non-parallel, but need per-job DBs); `Ready()` tests need injected clock.
- **Exit criteria:** per-gate (G1–G8) test lists with acceptance assertions in the report; G1/G2/G3/G4/G5/G7 cannot pass today; G6's 37/37 is the only measured-green baseline, contingent on fixture hygiene.
