You are the implementation repair engineer for the approval-only workflow slice. Work only in the current isolated worktree:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow
Do not commit, reset, clean, checkout, or touch another repository. Preserve unrelated dirty changes. Read these first:
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
- /home/dwp/snaplink-audit-governance/api/openapi/openapi.yaml
- /home/dwp/snaplink-audit-governance/api/proto/audit.proto
Then inspect the complete current diff and tests. Implement fixes, not merely a report.

The fresh independent code gate is FAIL. Address every finding below with focused code and regression tests while preserving the approved boundary: sverpweb-stable owns this first approval-only slice; workflow has no purchase SQL/database handle; purchase mutation is through PurchaseAggregateCommandPort; execution/confirmation/dispatch/compensation/recovery/amendment remain disabled; use compensation_pending, never compensating; no live external evidence may be fabricated.

1. Database tenant binding/isolation (blocking): bind the expanded approval SQLite file to exactly one configured tenant before migrations can write. Add a durable metadata/binding row or equivalent. On startup with a different tenant, reject before any tenant-specific migration/backfill can insert. For legacy unscoped rows, adopt them only on first explicit bind and then prevent reuse by another tenant. Scan all existing tenant-scoped tables where present and reject null/foreign tenant rows; test initialize T1 then initialize the same file as T2, plus a foreign row and an empty/reused database case. Keep checks bounded and fail closed.

2. Authority boundary (blocking): local OrgStore is a deterministic test fixture only, never a configured deployment authority. Make the app reject local-compat authority whenever a tenant-configured approval app is not an explicit test fixture with an injected compatibility authority; do not let development/staging silently authorize with OrgStore. At claim and decision time external mode must require an adapter whose authorityMode is external, otherwise fail closed. Update tests/factories as needed; do not invent an aero-id endpoint or fake external membership.

3. Critical request provenance (blocking): workflow mutating HTTP commands must not silently generate operationId/requestId/traceId/idempotency identity when required headers/context are absent. Preserve server middleware for ordinary legacy routes, but for create/submit/claim/approve/reject require and validate X-Request-Id, X-Operation-Id, X-Trace-Id (or traceparent), and Idempotency-Key. Ensure X-Operation-Id is actually copied into the operation context when unit routes lack middleware. Update the normal web page to send stable fresh request/operation/trace IDs and test missing identity/no writes.

4. Governance receipt correctness (blocking): compare all transport behavior with the real OpenAPI/proto. Active single path is POST /api/v1/events?wait_for=... with EventInput; active batch is POST /api/v1/events:batch with {events,wait_for}; old /api/v1/compat/snaplink/audit/events is not an ingest path and legacy wrapper remains explicit test-only. A response is not evidence because of HTTP 2xx. For configured wait_for, require a validated matching receipt event_id, tenant_id, idempotency_key, status at or above the requested stage, accepted_at, and the requested stage timestamp. For ledgered or later evidence require the actual receipt stream_id/hash/sequence fields present in the real response/proto (and reject missing proof); higher stages must also prove the requested lower stage. Keep receipt evidence digest/idempotency stable, persist it through recordReceipt before local published, and retry if persistence fails. Reject missing, mismatched, duplicate/conflicting receipts deterministically. Add/update transport and outbox tests. Do not require or invent timestamp fields the real contract does not return beyond what is already represented by the validated response.

5. Migration integrity (blocking): migration checksums must cover the actual migration behavior, not descriptive prose. Use a stable canonical source for every migration (including code/backfill/definition behavior) or an equivalent implementation digest. Preserve an explicit, narrowly scoped compatibility path for databases carrying the immediately previous checksum algorithm if necessary; never silently accept arbitrary changed historical migrations. Reject unknown schema_migrations versions, including newer versions, before applying anything. Add tests that mutate actual migration behavior/source and that inject an unknown version. Do not claim backup/restore evidence unless run.

6. Audit coverage: critical workflow routes must leave an auditable failure intent when validation/authority checks fail before the transactional service call, without using the generic fallback as a fake success. Add a bounded failure event helper or equivalent and avoid duplicate events when the service transaction already emitted domain events. Approval decisions must include a dedicated immutable work-item outcome audit event (not only a history row or workflow-instance event), including decision/status/approval identity and safe provenance. Add tests for pre-service failure and work-item outcome event. Keep sensitive fields out.

7. Legacy path: any configured tenant approval app must never construct or route through direct PurchaseRequestStore writes. Compatibility is allowed only for an explicit non-production demo with no configured tenant. Add/retain strict startup tests. Do not weaken CSRF, idempotency, history, optimistic versions, or transactional outbox.

After edits run from the worktree:
  pnpm --filter @sverp/api typecheck
  pnpm --filter @sverp/api test
  pnpm check
Run focused tests for migrations, audit client/outbox, workflow application and purchase workflow routes as useful. Do not modify generated or unrelated repositories. If a test suite exposes an existing environment-only dependency issue, report it honestly and keep the implementation correct.

Return only a concise Markdown report with:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
The runner saves the report; do not write the report yourself. Never claim a remote governance receipt, external authority, production migration, backup/restore, or recovery test unless actually executed.