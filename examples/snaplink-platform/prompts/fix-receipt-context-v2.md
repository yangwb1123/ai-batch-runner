You are a narrowly scoped repair engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest gate at /home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md.
Inspect only the actual files needed:
- apps/api/src/audit/migrations.ts
- apps/api/src/audit/authoritative-event.ts
- apps/api/src/audit/sqlite-outbox.ts
- apps/api/src/workflow/application.ts
- apps/api/src/workflow/contracts.ts
- apps/api/src/workflow/authority.ts
- focused tests

Fix exactly these two current blockers, without broad refactoring:
1. Every legacy audit reconstruction pass v16/v17/v19/v20 must detect an
existing audit_outbox_receipts row before any quarantine or reconstruction
write. Receipt-backed payload_json and payload_digest are immutable. If the
row lacks required authority columns or has contradictory delivery state,
retain a bounded quarantine fact without changing payload_json/payload_digest
(or fail the migration closed). Valid receipt-backed rows must be left byte
for byte unchanged. Add/adjust a regression covering all relevant migration
passes, including missing authority.
2. Claim and approve/reject authority preflight and commit-fence calls must
receive the original initiator loaded from the persisted workflow instance,
not the HTTP actor placeholder. Ensure claim and decision paths consistently
use that derived context, while the actual current human remains principal.
Do not accept client initiator. Add a spy regression if needed.

Do not change execution/compensation/recovery boundaries, tenant binding,
idempotency, policy, or receipt validation except as required for these two
fixes. Do not invent external evidence.

Run focused tests and typecheck; if time permits full API tests/build/check.
Return only:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
The runner saves the report; do not write it yourself.
