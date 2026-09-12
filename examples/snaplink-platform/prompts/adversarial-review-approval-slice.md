You are the final adversarial security/reliability reviewer for the current isolated sverpweb approval-only workflow slice. Work only in the current worktree. Do not commit, reset, clean, checkout, or modify another repository.

The implementation has already been typechecked and tested once, but review it as untrusted code. Read:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/gate-v3.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/01-persistence-design.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/02-workflow-transitions.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/03-audit-integration.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/WORKFLOW_CONTRACT_V1.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/EXECUTION_CONTEXT_V1.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/AUDIT_PROVENANCE_V1.md
Then inspect every changed file and the complete diff in the current sverpweb worktree.

Check, and fix only genuine defects within this slice:

1. The application must not start a nested or split SQLite transaction. Purchase, workflow history, idempotency and local audit outbox intent must commit or roll back together on the same connection. No network call can happen under the transaction.
2. Workflow code must not contain purchase_requests SQL or a purchase Database handle. Purchase SQL must remain in the purchase module/adapter. No direct cross-project API/database assumption may be invented.
3. Migration creation, upgrade from the legacy audit/purchase schema, checksum repeatability, foreign keys, unique keys, append-only triggers and the actor/department query paths must work on fresh and legacy databases. Check all INSERT column/value counts.
4. Tenant and actor context must be server-derived; body/query/header assertions may only be consistency checks. Missing/contradictory tenant, malformed identity, missing idempotency, requester self-approval, stale claim/version and absent authority must fail closed without partial writes.
5. Approval must be distinct from execution. All execute/confirm/dispatch/fail/compensate/recover/amend paths in this slice must return structured disabled errors without opening a transaction or writing. Use compensation_pending, never compensating.
6. Approval candidate and current membership/position authority must be rechecked at claim and decision. No user-supplied approver may become the authority. Do not label requester/create/submit events as approver provenance. Preserve initiator/executor/approver/grant/policy fields honestly.
7. Event IDs, event keys, idempotency keys, payload digests, redaction, source/schema fields, tenant scope and delivery state must be stable and bounded. Local published/sent state must not be presented as a governance ledger receipt. Enhanced and legacy outbox query filters must not reference missing columns.
8. Existing non-workflow routes, legacy direct-store tests and app fixtures must remain compatible; no unrelated behavior or secrets are changed.

Run these commands after any repairs:
  pnpm --filter @sverp/api typecheck
  pnpm --filter @sverp/api test
  pnpm check
Do not weaken tests, typechecking or build gates. If a risk requires an unavailable external authority or deployed governance evidence, retain a fail-closed seam and document it rather than fabricating evidence.

Return only a concise Markdown report with exactly:
## Review Result
## Findings
## Defects Fixed
## Tests Executed
## Remaining Evidence Gaps
## Handoff Decision
The runner will save the report; do not write it yourself.
