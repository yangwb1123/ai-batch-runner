You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect:
- apps/api/src/workflow/migrations.ts
- apps/api/src/workflow/application.ts
- apps/api/src/workflow/authority.ts
- apps/api/src/workflow/application.test.ts
- apps/api/src/persistence/approval.test.ts
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/02-workflow-transitions.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/WORKFLOW_CONTRACT_V1.md

Fix every genuine blocker in the latest gate:
1. The active purchase approval definition currently has transitions from
`start` but no declared start node and lacks the phase-4 terminal graph
boundary. Make the definition structurally valid and aligned with the
approval-only boundary: explicitly declare valid start/end and required graph
nodes (including disabled confirmation/execution/audit-completion boundaries as
appropriate), keep execution/confirmation/dispatch/compensation/recovery
unavailable, and add a bounded migration-time definition validator. Validate
node IDs/types, transition endpoints, exactly one start/end, no duplicate nodes,
and no unsafe/free-form transitions. Preserve definition immutability and
checksum integrity; do not silently edit an already applied historical
migration. If a new additive definition version/migration is needed, keep
existing instances readable and make the service use only a validated active
version.
2. Workflow submit must fail closed when both routing policy and authority
return a null/blank department. A waiting approval instance must never be
created without an authoritative department context. Add a focused negative
test, while retaining route mismatch and finance-input fail-closed behavior.

Do not invent external authority endpoints or production evidence. Do not
weaken tenant binding, actor/SoD, idempotency, transaction boundaries,
audit/outbox, or disabled execution rules.

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
The runner saves the report; do not write the report yourself.
