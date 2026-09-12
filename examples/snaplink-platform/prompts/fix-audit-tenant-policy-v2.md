You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect the actual current code/tests:
- apps/api/src/routes/integrations.ts
- apps/api/src/config/env.ts and relevant app wiring
- apps/api/src/audit/migrations.ts
- apps/api/src/audit/authoritative-event.ts
- apps/api/src/audit/sqlite-outbox.ts
- apps/api/src/audit/governance-transport.ts
- apps/api/src/workflow/application.ts and audit policy contracts
- relevant tests, especially persistence/audit/workflow/integrations

Fix every genuine blocker in the latest gate:
1. The integrations audit query must use the server-configured deployment
tenant and audit source (`serverTenantId`/`workflowTenantId` and audit.source),
never hard-coded `sverp`, `sverp-web`, or a cross-tenant scope. Keep query
permissions tenant-safe and fail closed when required tenant/source config is
missing. Add a regression test if the existing test layout permits it.
2. Legacy audit migrations/reconstruction (notably migrations 17/19 and later
compatibility passes) must preserve existing permanent/dead-letter delivery
state. A previously permanent failure must never be reset to pending or made
claimable merely by a restart/migration. Only explicitly unverified,
non-permanent rows may be normalized to pending; conflicting/malformed rows
remain quarantined. Add a test for a permanent legacy row.
3. Approval audit events must report the actual approval-route policy and
version in governance provenance, not the separate audit classification/
retention policy. Keep the audit policy identity explicit elsewhere in bounded
metadata if needed. For approval events, `authority.policyId/policyVersion`
must be the provenance policy; for non-approval events the resolved audit
policy may be used. Add a regression test with different policy IDs proving the
wire payload maps correctly. Do not weaken policy resolver fail-closed behavior.

Preserve tenant binding, actor/SoD, idempotency, transaction/outbox atomicity,
receipt validation, redaction, trace normalization, and disabled execution /
compensation / recovery. Do not invent external receipts, source registration,
authority endpoints, backup evidence, or production readiness.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused tests and pnpm --filter @sverp/api test
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
