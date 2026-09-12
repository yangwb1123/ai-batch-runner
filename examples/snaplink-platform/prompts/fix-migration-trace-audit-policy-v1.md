You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect:
- apps/api/src/purchase/migrations.ts
- apps/api/src/persistence/approval.ts
- apps/api/src/workflow/http-context.ts
- apps/api/src/audit/request-middleware.ts
- apps/api/src/audit/governance-transport.ts
- apps/api/src/workflow/application.ts and contracts/tests
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/EXECUTION_CONTEXT_V1.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/AUDIT_PROVENANCE_V1.md

Fix every genuine blocker in the latest gate:
1. Legacy purchase migration must not accept a row whose existing aggregate
version is greater than the one-entry legacy history. Preserve the existing
version only when the historical chain is proven consistent; otherwise fail
closed/quarantine deterministically before making a usable workflow. Never
invent missing historical transitions. Add a test with version 5 and only
version-1 history (or another inconsistent chain) proving initialization does
not succeed silently; retain valid legacy backfill behavior.
2. Legacy purchase quarantine inserts must be conflict-checked. If a
(tenant,aggregate) quarantine row already exists, compare all immutable facts
(status, reason_code, source_marker and identity); conflicting facts must fail
closed, not be skipped by INSERT OR IGNORE. Add a regression test.
3. Normalize W3C trace context. If `traceparent` is supplied, validate the
exact W3C shape and put only its 32-hex trace-id into OperationContext.traceId
(reject malformed/all-zero IDs); do not forward the full header as trace_id.
Apply this consistently in workflow/http-context and audit request middleware,
including critical identity markers. Add tests for valid and malformed
traceparent and X-Trace-Id behavior. Do not invent span storage.
4. Critical workflow audit events must not silently hardcode an unverified
classification/retention policy. Introduce a small explicit policy resolver
or equivalent approved boundary in workflow dependencies; the configured app
must fail closed when no policy authority is supplied, while test fixtures may
inject a deterministic compatibility policy. Persist bounded policy identity
and version if the current contract supports it; do not use arbitrary client
input. Keep generic legacy events' safe defaults only where they are not
critical workflow facts. Add positive injected-policy and negative missing-
policy tests. Do not invent external endpoints or claim external evidence.

Preserve tenant binding, actor/SoD, idempotency, transaction/outbox atomicity,
receipt validation, redaction, and all disabled execution/compensation rules.

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
