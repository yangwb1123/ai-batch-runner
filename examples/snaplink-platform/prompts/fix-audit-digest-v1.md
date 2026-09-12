You are a focused implementation engineer. Work only in the isolated worktree:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Use read,
write, edit, and bash tools as needed. Read the latest independent gate:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
and inspect the current implementations/tests in:
- apps/api/src/audit/migrations.ts
- apps/api/src/audit/sqlite-outbox.ts
- apps/api/src/audit/governance-transport.ts
- apps/api/src/audit/sqlite-outbox.test.ts
- apps/api/src/persistence/approval.test.ts

The latest gate is FAIL because audit migration v20 computes a legacy
payload_digest using a payload/provenance shape that does not exactly match the
payload reconstructed by SQLiteAuditOutboxStore.asRecord() and delivered by
toGovernanceEvent(). A valid migrated legacy approval event can therefore fail
claimPending()/relay digest validation and be dead-lettered. Fix the actual
code, not merely documentation, while preserving all existing fail-closed
requirements:

1. Make the canonical digest for a migrated legacy approval event byte-for-byte
equal to governancePayloadDigestForEvent(asRecord-equivalent event), and make
sure the persisted payload_json, authoritative columns, asRecord(), and
transmitted governance payload agree. Prefer one shared canonicalization seam;
do not duplicate subtly different digest algorithms.
2. Do not edit an already-deployed historical migration in a way that breaks
its checksum. If v20 must remain immutable, add a narrowly scoped additive
migration/reconciliation that fixes rows produced by v20 before relay. If you
edit v20, provide an explicit, auditable previous checksum bridge and explain
why it is safe. Unknown migration versions and tenant binding must remain
fail-closed.
3. Add a deterministic regression test that creates/represents a legacy
approval event with distinct human requester/approver, policy and approval
identity, runs the migration, claims it, verifies no digest error/dead-letter,
and checks that the delivered governance payload digest equals the stored
payload_digest. Preserve tests for malformed/missing provenance quarantine.
4. Do not weaken receipt validation, tenant/actor authority, idempotency,
transactional outbox, redaction, or disabled execution/compensation/recovery.
Do not invent remote authority, source registration, receipt, backup, or
production evidence.

Run at minimum from the target worktree:
- pnpm --filter @sverp/api typecheck
- focused audit/persistence tests, then pnpm --filter @sverp/api test
- pnpm check
- python /home/dwp/ai-batch-runner/quality.py --strict /home/dwp/workspace/job/sverpweb-stable-platform-workflow

Return only a concise Markdown report with exactly:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
The runner saves the report; do not write the report yourself.
