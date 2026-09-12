You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect:
- apps/api/src/persistence/approval.ts
- apps/api/src/persistence/approval.test.ts
- apps/api/src/persistence/migrations.ts
- apps/api/src/audit/migrations.ts

Fix the genuine blocking defect: approval.ts commits the immutable
approval_database_binding before runSQLiteMigrations(). If a later migration
fails while legacy purchase_requests/audit_outbox are still unscoped, restart
sees an existing binding and strict preflight rejects those legacy tables before
migration retry, stranding the database permanently.

Implement a fail-closed, recoverable ordering. The first binding must still be
created before migration code that needs the binding, must never be reusable by
another tenant, and must not allow application use after a failed/incomplete
migration. A failed migration/restart for the same tenant must be able to retry
recognized legacy unscoped tables; once migrations complete, the final strict
isolation check must reject any remaining unscoped/null/foreign rows. Do not
allow unknown tables/tenants, do not weaken bounded preflight, and do not move
network calls into transactions.

Add a deterministic regression test that forces a migration failure after the
binding is persisted, confirms the binding survives, then retries the same
file/tenant after the failure condition is removed and succeeds; also verify a
different tenant remains rejected. Keep existing unknown-version and tenant
isolation tests. Do not fake production/backup/authority evidence.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused persistence tests and pnpm --filter @sverp/api test
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
