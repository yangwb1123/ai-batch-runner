You are a focused implementation repair engineer. Work only in the current isolated worktree:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow
Do not commit, reset, clean, checkout, touch another repository, or weaken tests.

The independent code gate is at:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
Read it first, then inspect the current complete diff and source. The isolated
worktree currently passes its broad typecheck/test/build suite, but the gate
found six design-level defects that must be corrected with code and tests:

1. Audit lifecycle: a valid remote governance receipt currently becomes local
   `published` while the receipt-stage evidence is not persisted and the row
   is no longer claimable. Implement a coherent solution: carry enough
   validated receipt evidence through the transport/publisher to call the
   outbox receipt recorder, preserve stable event/outbox/idempotency identity,
   and never treat HTTP status or local published as a ledger receipt. Add
   tests for matching receipt, missing identity, duplicate/conflict and receipt
   persistence. Do not fabricate remote evidence.

2. Governance contract: compare the current client and configuration with the
   actual audit-governance OpenAPI/proto under
   /home/dwp/snaplink-audit-governance/api. Fix the default path/envelope or
   adapter so the configured active path is explicit and not silently the old
   `/api/v1/audit/events` wrapper when the active contract is
   `/api/v1/events` or `/api/v1/events:batch`. Preserve legacy test adapters
   only when explicitly configured. Add/update contract tests; do not invent
   fields that the receiver cannot validate.

3. Authority: local OrgStore may be used by deterministic test/demo fixtures,
   but a deployed approval configuration must not silently treat it as
   authoritative Snaplink/aero-id membership. Add an explicit authority mode
   or equivalent fail-closed production guard. At claim and decision time,
   make the required authority boundary injectable and reject missing/unverified
   external authority in production. Do not invent an aero-id endpoint; keep
   the existing local adapter clearly compatibility-only and test fail-closed
   behavior.

4. Purchase evidence: a direct caller must not be able to approve/reject a
   pending purchase with fabricated workflow/work-item/epoch/policy evidence.
   Preserve the rule that purchase SQL stays in the purchase module and
   workflow code has no purchase SQL. Reorder or add a typed verifier/commit
   boundary so the evidence is validated against the current workflow claim
   and persisted decision in the same transaction, without a circular or
   direct workflow-database dependency. Add a regression test for fabricated
   evidence and keep the real workflow path passing.

5. Concurrency: strict mutating HTTP commands must reject absent expected
   purchase/workflow/work-item/claim/approval versions rather than rereading
   current versions as defaults. Update the web purchase page/DTOs if needed
   so the normal UI supplies the versions and explicitly claims before
   deciding. Preserve If-Match as an accepted version source. Add tests for
   missing and stale versions; no latest-read fallback on a write.

6. Legacy compatibility: legacy direct PurchaseRequestStore writes may remain
   only for explicit non-production demo compatibility. Ensure any approval
   deployment with a configured tenant cannot route through the legacy router,
   and add a production/strict test. No production approval write may bypass
   idempotency, history, CSRF and transactional outbox.

Use existing libraries and conventions. Read enough source to understand the
current changes before editing. Keep execution, compensation and recovery
disabled in this slice; use `compensation_pending`, never `compensating`.
Run after repairs:
  pnpm --filter @sverp/api typecheck
  pnpm --filter @sverp/api test
  pnpm check
Do not claim live governance, Snaplink, migration or recovery evidence unless
actually executed; record missing external evidence honestly.

Return only a concise Markdown report with:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
The runner will save the report; do not write it yourself.
