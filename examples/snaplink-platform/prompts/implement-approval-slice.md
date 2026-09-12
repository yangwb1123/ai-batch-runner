You are the implementation engineer for the approved design-to-code handoff. Work only in the current target worktree. Do not call git reset, git clean, git checkout, or modify files outside this worktree. Do not commit. Do not change another repository.

Before editing, read these bounded source-of-truth documents with the read tool:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/gate-v3.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/01-persistence-design.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/02-workflow-transitions.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/03-audit-integration.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/04-operations-acceptance.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/WORKFLOW_CONTRACT_V1.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/EXECUTION_CONTEXT_V1.md
/home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/AUDIT_PROVENANCE_V1.md
/home/dwp/ai-batch-runner/examples/snaplink-platform/platform-map.yaml

Inspect the existing sverpweb-stable source, package scripts, API wiring, SQLite initialization, authentication context, purchase routes/store, audit contracts/client, and tests before changing anything. Use only the current repository's established libraries and conventions.

Implement ONLY the first approval-only vertical slice in this clean isolated worktree. Keep the current Bun/SQLite and single-tenant deployment model. Do not implement payment, inventory, shipment, external side effects, execution dispatch, compensation, restore, shared-instance multi-tenancy, a new repository, a runtime plugin host, or a direct cross-project integration.

Required implementation boundaries:

1. Add a small module-local Workflow application/domain layer with typed commands for create/submit/claim/approve/reject and a centralized transition table. Use the canonical compensation_pending vocabulary for future states; execution, compensation and recovery commands must be explicitly disabled with a structured error and no database writes in this slice. Workflow code must not contain purchase_requests SQL or receive a purchase database handle.

2. Add versioned, idempotent, checksummed SQLite migrations for the workflow-owned tables needed by this slice: immutable definition version, instance, work item/candidates, approval decision, append-only workflow history, command idempotency and local audit outbox/attempt/receipt state as appropriate. Do not hide new schema changes in a constructor. Preserve legacy purchase data and make migration/backfill behavior explicit and safe.

3. Keep purchase aggregate writes in the purchase module behind a typed PurchaseAggregateCommandPort/adapter. The workflow layer may call that port; it may not update purchase tables itself. Refactor the existing purchase approval route only as far as needed to use the application boundary while preserving compatible API behavior. Add optimistic versions and immutable purchase history where required by the design, with safe compatibility for existing rows.

4. Couple each critical purchase/workflow mutation and its redacted audit_outbox intent in the same local SQLite transaction. Preserve stable event_id, operation_id, event type, schema version, idempotency key and bounded provenance. Never make an HTTP or network call inside that transaction. Do not claim remote audit-governance delivery; retain a clear adapter/relay seam and leave remote receipt state pending until a later integration. Ordinary notifications and OTel must not be used as audit proof.

5. Enforce server-side context. Do not trust tenant_id, department, approver or actor switches supplied by a client. Preserve the current single-tenant behavior through an explicit configured server tenant/context contract; reject missing or contradictory context rather than silently guessing. Approval candidates and membership/position checks must be revalidated at claim and decision time using the repository's existing authority boundary. Requester cannot approve; department/finance separation and stale approval epoch/version are fail-closed. Do not invent an aero-id API that is not present in this repository.

6. Add focused tests for transition legality, disabled execution, idempotency, optimistic concurrency, requester/self-approval, stale claim, tenant/context mismatch, amendment/reapproval if supported by existing API, and atomic rollback proving that a failed outbox insert leaves no business/workflow/idempotency state. Add migration upgrade/repeat and legacy compatibility tests. Keep existing tests passing.

Prefer a small complete implementation over broad scaffolding. If a requirement cannot be implemented honestly because an authority contract or existing runtime seam is missing, fail closed, document the exact gap in the implementation report, and add a test for the safe behavior instead of inventing a fallback.

Run the narrowest relevant typecheck and tests while iterating, then run the repository's documented API checks if they are affordable. Do not weaken existing lint/type/test gates. At the end, return a concise Markdown implementation report containing:
## Changed Files
## Implemented Behavior
## Intentionally Disabled
## Tests Executed
## Remaining Evidence Gaps
## Review Risks
The runner will save the report; do not write it yourself.
