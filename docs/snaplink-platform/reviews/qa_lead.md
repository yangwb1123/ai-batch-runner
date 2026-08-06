QA lead review complete — written to `docs/snaplink-platform/reviews/qa-lead-review.md` (387 lines). Summary of what I did and found:

## What I ran (measured, this revision)
- **sink** (`snaplink-audit-governance`): `go test ./...` → **157 pass**; `-race` on outbox/service → pass; all Postgres-gated tests **SKIP** (no `AUDIT_TEST_POSTGRES_DSN`)
- **aero-id**: `go test ./...` → **926 pass** (48 pkgs)
- **aero-vault**: `go test ./...` → **1519 pass**; `-race` on auditgovernance/events → pass; Postgres lease-recovery integration test **SKIP**s
- **snaplink**: `go test ./...` → **1558 pass** (incl. `TestOIDCDiscovery`, which asserts the wrong endpoints)
- **aero-im**: `cargo test -p aero-storage audit` → **7 pass, 30 ignored** (need live PG)
- **Not run**: sink `fullstack.sh` e2e (manual docker, dev-token-only), benches, load/chaos suites — none exist in any CI

## Key QA findings (13, in the report)
- **T-1 Critical**: no test exercises the production trust path — all 157 sink tests + e2e run on forged `dev:` tokens while `AUDIT_ALLOW_DEV_AUTH` defaults **true**; the F-2 end-to-end break (IdP JWT can never pass sink verification) is invisible to every suite. Cross-repo e2e test specified — fails today.
- **T-2 High**: `TestOIDCDiscovery` is a regression lock on the defect (asserts `token_endpoint=/authenticate`).
- **T-3/T-4 High**: vault poison-event→dead and event_id determinism untested (no dead state exists; random UUIDs make sink dedup vacuous on regeneration).
- **T-5 High**: aero-id's 8 post-commit `_ = s.audit.Record(...)` discard sites — silent-loss window untested.
- **T-6 High**: Postgres concurrency invariants (single-writer snapshot conflicts) skip in every CI.
- **T-7..T-13**: clock skew, `/token` HTTP surface (zero tests), introspection oracle, aero-id 401 rotation, aero-im gap, read self-audit, capacity/load.

Each finding includes the exact test to add and its acceptance assertion; 20-row requirement-to-test matrix, 15-scenario prioritized list, CI gaps, flake risks, fixtures, and exit criteria. All labels Verified (executed)/Inspected (read)/Cited/Missing per the evidence standard. Advisory only — no files modified.
