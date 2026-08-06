Approval note produced: `docs/snaplink-platform/decisions/APPROVAL.md` (84 lines).

**Contents, all sourced from the decision trail (01–05 → summary.json → gate.md):**

1. **change_id**: `COMPOSE-2026-018` — year from current date; 017 already used by the example manifest, so no collision. Includes requirement, interpreted goal, P0+P1 scope with P2/P3 exclusions, primary project, and evidence chain.

2. **Affected repositories** (verified against summary.json ownership + gate's zero-overlap ruling):
   - **4 with implementation changes**: aero-vault (core: delete + tombstone + outbox + 2 events + 3 admin APIs), snaplink (permission seed), aero-im (template seed), snaplink-console (BFF + file management page)
   - **1 zero-code but must verify**: snaplink-audit-governance (pure reuse; staging ingest/query/timeline validation before release)
   - **Explicitly unaffected**: aero-id (read-only consumer, uid reference only) — noted with "被调用 ≠ 受影响" per the gate's finding on the 04 draft

3. **Deployment order**: snaplink authorization → aero-vault (migration + deploy) → aero-im template (before first notification) → console → audit-governance staging verification, plus rollback principle (deletions irreversible, ledger events never rolled back).

4. **PROPOSAL declaration**: plan is a proposal only — human approval required before any repository implementation starts; the change manifest must pass `check-change-manifest.py` (with the exact command, the audited fields, and the required `audit: durable_async` / `notification: best_effort_async` failure policies) before scoped per-repo implementation; open risks carried forward honestly.

Ends with an approval record table (approver/date left blank for the human sign-off).
