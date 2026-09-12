You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect:
- apps/api/src/audit/migrations.ts
- apps/api/src/audit/authoritative-event.ts
- apps/api/src/audit/sqlite-outbox.ts
- apps/api/src/workflow/application.ts
- apps/api/src/workflow/http-context.ts
- apps/api/src/workflow/contracts.ts
- relevant audit/persistence/workflow tests

Fix both blockers:
1. All legacy audit reconstruction migrations (v16/v17/v19/v20 and any
additive compatibility passes) must not rewrite payload_json or payload_digest
when audit_outbox_receipts already contains a receipt for that outbox row.
Receipt-backed event bytes are immutable evidence. Preserve state and bytes;
if required authoritative columns are unavailable, fail closed/quarantine
without changing the event. Add a deterministic test that snapshots a
receipt-backed legacy row's payload/digest, reruns the reconstruction boundary,
and proves bytes/digest remain identical and no fake receipt is created.
2. For claim and approve/reject, the service must replace the HTTP actor-as-
initiator placeholder with the original initiator loaded from the persisted
workflow instance before any canClaim/canDecide authority lookup or commit
fence. The external authority must receive the real requester and the actual
human approver remains principal. Do not accept client-provided initiator.
Preserve idempotency replay, same-key race handling, SoD, tenant binding,
transaction/outbox atomicity, and all disabled execution paths. Add a negative
or spy regression proving authority sees requester != approver in claim and
decision paths.

Do not invent external endpoints or production/receipt/backup evidence. Keep
historical migration checksums honest; use additive migration only if needed.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused tests plus pnpm --filter @sverp/api test
- pnpm --filter @sverp/api build
- pnpm check
- python /home/dwp/ai-batch-runner/quality.py --strict /home/dwp/workspace/job/sverpweb-stable-platform-workflow

Return only a concise Markdown report with exactly:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
The runner saves the report; do not write it yourself.
