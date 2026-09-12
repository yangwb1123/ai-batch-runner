You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect:
- apps/api/src/workflow/migrations.ts
- apps/api/src/workflow/application.ts
- apps/api/src/workflow/sqlite-repository.ts
- apps/api/src/persistence/approval.ts
- relevant migration/application tests
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/02-workflow-transitions.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/WORKFLOW_CONTRACT_V1.md

Fix the blocking migration defect: an upgrade containing the previously
shipped malformed active purchase-approval@v1 currently quarantines/retires
that only row but does not install a valid active replacement, so v29 rejects
new workflow instances. The first slice contract must use one validated
immutable purchase-approval@v1 for new instances.

Implement a safe, honest migration strategy. Preserve the malformed historical
bytes in a read-only quarantine fact. On an upgrade with no running instances
that reference the malformed key, install the validated current v1 definition
as the sole active row after the old invalid row is quarantined (or use an
explicit equivalent that keeps new instances on validated v1). If existing
instances make replacement unsafe, fail closed with an explicit quarantine /
operator-reconciliation state rather than silently changing their meaning; do
not create an unusable normal deployment. Ensure fresh databases and repeated
upgrades both have a valid active v1, no active v2, and the immutable trigger /
single-active index remain correct. Do not edit old event bytes or invent
history. Add an upgrade regression that starts with the malformed v1,
initializes, verifies quarantine plus a valid active v1, and submits a new
workflow through the normal service when its other test authorities/policy are
injected. Keep checksums/previous-release compatibility honest and add any
needed migration-time validation.

Preserve tenant binding, route-policy snapshot, actor/SoD, idempotency,
transactional outbox, audit policy, trace normalization, and all disabled
execution/compensation/recovery boundaries. Do not invent external authority,
receipts, backups, or production evidence.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused workflow/persistence tests and pnpm --filter @sverp/api test
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
