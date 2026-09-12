You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect:
- apps/api/src/audit/migrations.ts
- apps/api/src/audit/authoritative-event.ts
- apps/api/src/audit/governance-transport.ts
- apps/api/src/audit/sqlite-outbox.ts
- apps/api/src/persistence/approval.test.ts
- relevant audit/workflow tests

Fix both genuine blockers:
1. Legacy audit reconstruction must never preserve stale/forged
`metadata.authority` or let it influence approval-route policy provenance. The
migrated authority columns are authoritative. Strip legacy authority metadata
and, when appropriate, rebuild only the minimal bounded authority policy
object from the migrated policy_id/policy_version columns; ensure normal new
approval events still map the real approval-route policy while audit
classification/retention policy remains separate. Add/retain a regression that
poisons legacy metadata.authority and proves relay uses columns or fails closed.
Keep payload digest canonicalization consistent.
2. Every legacy audit quarantine insertion path, especially v16
`reconstructLegacyAuditOutbox`, must compare an existing row's immutable facts
(tenant/outbox/event identity, reason code, payload digest as applicable) and
fail closed on conflict. Do not use INSERT OR IGNORE as a substitute for
comparison. Identical facts may be idempotently retained. Add a regression
that creates a conflicting existing quarantine row and proves migration
rejects it; preserve malformed/sensitive quarantine behavior.

Preserve tenant binding, redaction, idempotency, receipt validation,
transaction/outbox atomicity, trace normalization, and disabled execution /
compensation / recovery. Do not invent external authority, receipts, source
registrations, or production evidence. Keep historical migrations checksum-
aware; use an additive migration if changing an already applied migration is
unsafe.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused audit/persistence/workflow tests and pnpm --filter @sverp/api test
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
