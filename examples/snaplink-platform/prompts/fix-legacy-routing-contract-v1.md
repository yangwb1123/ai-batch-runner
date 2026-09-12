You are a focused implementation engineer. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Read the
latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect the actual current code/tests before editing.

Fix every genuine blocker in that latest gate:
1. Remove the reachable HTTP legacy direct-write compatibility router from the
normal application/API boundary. The approval slice must not expose
createLegacyPurchaseRouter or route strict=false to direct PurchaseRequestStore
writes. Preserve only store-level migration/unit helpers if needed; they must
not be an HTTP deployment path. Update app wiring/docs/tests honestly and do
not weaken the typed workflow, idempotency, optimistic versions, CSRF, SoD or
transactional outbox rules.
2. Separate immutable event identity from internal outbox identity in legacy
compatibility upgrades. New legacy rows must not use event.id as the internal
row/outbox identity. Existing migrated rows that have conflated identity need
an additive, checksum-covered deterministic repair that keeps event_id and
payload bytes stable and keeps audit attempt/receipt/quarantine references
consistent with the schema. Check all claim/mark/receipt/reconciliation paths;
do not create a foreign-key mismatch. Add a regression proving
outboxId !== eventId and receipt/attempt lookup still works after restart.
3. Reconcile definition versioning with the contract. Use a validated,
immutable purchase-approval@v1 for this first approval-only slice, or if an
additive v2 is strictly necessary, explicitly deprecate/quarantine the old
version and document/update all contracts and instance persistence so there is
only one selected active definition for new instances. Do not leave two
active versions ambiguously selectable. Keep full graph validation and all
execution/compensation/recovery boundaries disabled.

Also preserve the already implemented tenant binding, wildcard rejection,
route-policy snapshot/quarantine, receipt-backed migration immutability,
original initiator context, audit policy separation, trace normalization,
external-authority fail-closed behavior, and redaction. Do not invent external
endpoints, receipts, backups, or production evidence.

Run at minimum:
- pnpm --filter @sverp/api typecheck
- focused migration/audit/workflow/route tests and pnpm --filter @sverp/api test
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
