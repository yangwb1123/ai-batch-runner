You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect the actual current code/tests:
- apps/api/src/workflow/migrations.ts
- apps/api/src/workflow/application.ts
- apps/api/src/config/env.ts
- apps/api/src/app.ts
- apps/api/src/audit/request-middleware.ts
- apps/api/src/audit/sqlite-outbox.ts
- relevant workflow/config/audit tests
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/02-workflow-transitions.md

Fix every genuine blocker in the latest gate:
1. Migration v5 must never leave an active malformed purchase-approval v1
definition (it currently has start transitions but no start/end nodes). Keep
definition immutability and checksum/previous-release compatibility honest.
Use a valid bounded declared graph for any active definition; if preserving an
old invalid historical row is necessary, make it explicitly non-active,
quarantined/read-only with an auditable additive migration rather than silently
using it for new instances. New instances must select only the validated active
version. Migration-time validation must reject malformed definitions and test
unknown/invalid graph cases. Do not weaken execution-disabled boundaries.
2. Enforce MULTI_TENANT=false at the createApp startup boundary as well as
readConfig. An injected production/staging AppConfig with multiTenant=true must
fail closed; add a regression test. Do not enable shared-instance tenancy.
3. Generic audit events emitted through request middleware/emitAuditEvent must
carry explicit safe dataClassification and retentionClass defaults before an
enhanced tenant-bound outbox sees them, so configured single-tenant generic
requests are not silently dropped. Preserve bounded redaction, failure intent,
and do not turn a swallowed network/relay error into success. Add a test using
an enhanced SQLite outbox and a generic event.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused workflow/config/audit tests and pnpm --filter @sverp/api test
- pnpm --filter @sverp/api build
- pnpm check
- python /home/dwp/ai-batch-runner/quality.py --strict /home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not invent external authority, audit receipts, source registrations,
backup/restore, or production evidence. Return only a concise Markdown report
with exactly:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
The runner saves the report; do not write the report yourself.
