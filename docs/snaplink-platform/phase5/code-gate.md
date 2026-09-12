## Decision

FAIL. A legacy approval-definition migration can leave the database unusable or permanently incomplete.

## Verified Code Invariants

- Workflow code uses a typed purchase port and contains no purchase-table SQL.
- Configured deployments require a bound single tenant; client tenant fields are consistency checks only.
- Critical writes require idempotency keys and expected versions.
- Purchase/workflow history, idempotency, and audit outbox writes share one `BEGIN IMMEDIATE` transaction.
- Execution, compensation, recovery, confirmation, and dispatch methods fail closed without writes.
- Workflow states use `compensation_pending`; no alternate uncertain-effect state is introduced.

## Findings

- **Blocking migration defect:** `apps/api/src/workflow/migrations.ts` retires the only malformed active `purchase-approval@v1` definition into quarantine but never installs a valid active replacement.
- On a normal upgrade where v5 is already applied, v23 quarantines and retires the malformed v1. v28 tolerates the retired row, while v29 later rejects new workflow instances because the definition is not active.
- If v5 itself handles the malformed row, v23 reprocesses the retired row and conflicts with the existing quarantine’s `original_status: active`, causing migration failure.
- The existing migration test verifies retirement but does not submit a new workflow afterward, so it misses this unusable post-migration state.

## Evidence Still Required

- Migration rehearsal from the previously shipped malformed active definition, followed by a successful new purchase submission.
- Backup, integrity check, schema fingerprint, migration ledger, and rollback evidence.
- Verified external Snaplink/aero-id authority and segregation-of-duties fixtures.
- Active audit source/schema registration, authenticated ingest, and receipt evidence. Live receipts are not required for this code-handoff decision.
- Relay retry, lease reclaim, DLQ, redaction, and atomic rollback evidence.

## Explicitly Not Approved

- Production readiness or migration closure.
- Execution, dispatch, recovery, compensation, payment, shipment, reservation, or arbitrary commands.
- Enabling local compatibility authority or direct legacy purchase writes in deployment.
- Treating local outbox state as governance-ledger evidence.

## Handoff

Do not hand off as safe. Fix the definition migration so historical malformed data is quarantined while a validated active definition remains available, then add an upgrade-and-submit regression test and rerun the gate.

VERDICT: FAIL - malformed workflow-definition migration leaves no active approval definition
