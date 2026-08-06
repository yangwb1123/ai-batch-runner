Review written to `docs/snaplink-platform/reviews/distributed-systems-review.md` (30KB). I verified the design draft against the actual code in all six repos (aero-id, aero-vault, aero-im, snaplink, snaplink-sso, snaplink-audit-governance) before writing it.

## Headline results

**Verified anchors held, but the draft's "reference pattern" is not what it describes.** Both connectors exist and are wired, but the real pattern is a *synchronous `wait_for=ledgered` publisher + leased outbox*, not the §1.2 shape (batch 100 / cap 300s / 422→dead) — constants differ everywhere (aero-id batch 50, cap 1h; vault batch 32, cap 300s; no 422→dead in vault).

**Top findings (14 total, 5 High):**

1. **F1 — Sink is a whole-snapshot single-writer store.** `store.go`: file (full JSON rewrite per ingest) or one Postgres jsonb row + optimistic version. Ingest cost is O(ledger size); with "never drop" producers, the pressure valve is backlogs and vault's `Ready()` maxLag=900s health gate.
2. **F2 — Scope contract contradiction.** Draft/scope-matrix say `audit:event:ingest`; every implementation enforces `audit:event:write` (sink `require()`, aero-id `validOAuthBinding` rejects anything else, vault `RequiredScope`). Provisioning per the matrix fails at rollout.
3. **F3 — Vault has no dead-letter state.** No status column; *all* failures (incl. 409/422 poison) retry forever with 300s cap, and one poison row permanently red-flags `Ready()` → restart loops.
4. **F4 — Vault fact IDs are random UUIDs** (`uuid.NewString()` in facts.go), making the sink's `(tenant_id, event_id)` dedup and `idempotency_key` uniqueness vacuous; regenerated facts create duplicate ledger events. The draft's deterministic SHA-256 event_id exists nowhere.
5. **F5 — Aero-id silent loss window:** service-layer `Record` calls are post-commit with `_ =` discarded errors (account.go:155/184, callback.go) — crash = audit gap with no alert. Only repository paths use `AppendInTx`.

Also notable: F6 (aero-id delivers every event twice and the inline publish adds up to 5s request latency), F7 (clock-skew policy unimplemented — sink accepts any `occurred_at`), F8 (aero-id never invalidates tokens on 401, unlike vault; ~24h stall after credential rotation), F9 (dead_letter after 8 attempts ≈ 2–3 min, no replay tool), F11 (the IdP's "local audit log" L0 is an in-memory ring buffer — the P4 backfill premise is vacuous), F13 (sink self-audit writer — the draft's own P0 — doesn't exist).

The document includes the full state map (9 components), a 13-row scenario table (partition/crash/retry/clock rollback/stale cache/dependency outage/recovery), a guarantee-by-guarantee verdict table, unsupported topologies, 8 concrete validation tests (several of which will fail today by design), and residual risks. Advisory only — no files modified.
