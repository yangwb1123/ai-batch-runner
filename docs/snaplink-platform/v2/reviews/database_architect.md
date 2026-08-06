Review complete. Written to **`docs/snaplink-platform/v2/reviews/persistence-review-v2.md`** (33.6KB).

## What I did

Applied `prompts/README.md` (evidence standard, severity scale, "stock binary wiring" discipline), then inspected every store in the platform at pinned HEADs: sink (`snaplink-audit-governance`), aero-vault, aero-id, aero-im, the active IdP (`workspace/demo/snaplink`), console, plus the legacy monolith.

## Key results

**Store inventory (7 stores):** all durable-by-design except two explicitly hot (IdP stock audit ring `backend: memory`, console `localStorage` ring) — plus the sink's default file backend with a real durability gap (no fsync).

**Critical findings:**
- **F-1** Sink's real persistence is a whole-snapshot JSON blob rewritten per event; the relational ledger (`migrations/001` `ledger_events`/`ledger_streams`/`ledger_segments`) is **dead schema — zero Go references**. Ingest = O(snapshot) write + O(all events) idempotency scan; `QueryEvents` is O(ledger).
- **F-2** Stock `audit-api` defaults `AUDIT_ALLOW_DEV_AUTH=true` → `dev:platform:platform-admin` is a full auth bypass on a default deployment (verified in `main.go` + `auth.go`).
- **F-3** File backend `os.WriteFile`+`os.Rename` with no `Sync()` — snapshot can silently revert on power loss (its own WORM archive does fsync; the state store doesn't).

**High:** aero-id dual delivery path + swallowed `_ =` publish errors; aero-vault no dead-letter (permanent 422s retry forever) and Ready() 503s → restart loop; stock IdP config ships memory for identity/oauth/audit (loses all state on restart); sink `ErrSnapshotConflict` → 500 with no retry.

**Verified-good:** aero-id advisory-lock serialized hash-chain append + append-only trigger + same-tx publication outbox; aero-vault `SKIP LOCKED` lease claims; aero-im trigger-enqueued outbox with `UNIQUE(destination, idempotency_key)`; IdP Redis Lua-atomic token rotation.

**Correction to the batch plan (F-13):** "snaplink (IdP)" rows mix two codebases (`cmd/server/routes.go`/`token/jwt/generator.go` are legacy; `issue_payload.go` is active) — B4 must be re-pinned to the active repo. Also verified the active IdP **already stamps `kid`**; only `tenant_id`/`roles`/scope-registry/iss-allowlist are missing (adjusted F-9 accordingly).

The review also contains the query/index/transaction analysis, a 7-step safe migration sequence with compatibility windows and validation queries per gate (T-1.1…T-12, D1), and the required unknowns section: no volume evidence (a `BenchmarkIngest` envelope is the first B1-3 deliverable), unverified retention defaults, and missing backup/restore drills.
