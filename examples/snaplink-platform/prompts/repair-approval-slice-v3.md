You are the implementation repair engineer for the isolated sverpweb approval-only workflow slice. Work only in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Do not commit, reset, clean, checkout, or touch another repository. Use the
latest independent gate and the concurrent review reports as untrusted defect
reports, then inspect the actual source and complete diff before editing:
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase5/code-gate.md
- /home/dwp/workspace/job/sverpweb-stable-platform-workflow/docs/workflow/parallel-security-review.md
- /home/dwp/workspace/job/sverpweb-stable-platform-workflow/docs/workflow/parallel-persistence-review.md
- /home/dwp/workspace/job/sverpweb-stable-platform-workflow/docs/workflow/parallel-boundary-review.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/gate-v3.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/01-persistence-design.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/02-workflow-transitions.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/03-audit-integration.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/WORKFLOW_CONTRACT_V1.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/EXECUTION_CONTEXT_V1.md
- /home/dwp/ai-batch-runner/docs/snaplink-platform/contracts/AUDIT_PROVENANCE_V1.md

Implement every genuine blocking source defect found by the latest gate or
reviews, with focused regression tests. In particular, verify and repair all
of these when applicable to the current code (do not assume the report is
correct without reading the implementation):

1. Approval audit provenance must preserve the original initiator and the
   actual human approver in both the local event and governance payload. The
   work-item outcome event is a dedicated immutable event, not only a history
   row. Do not relabel an approver as the requester or fabricate executor,
   grant, policy, or external authority fields.
2. Legacy audit outbox rows upgraded by migrations must be reconstructed with
   the authoritative migrated tenant/source/schema/idempotency/provenance
   columns, not only their stale payload_json. A row missing required facts
   must fail closed or remain quarantined; never relay an unscoped event.
3. Exported purchase aggregate command ports/adapters must enforce the same
   configured single-tenant binding and human-approver boundary as the
   application service. An agent/service decision or a second tenant must be
   rejected before any write. Preserve external-authority fail-closed mode.
4. Preserve checksum coverage of real migration behavior and a narrowly
   auditable bridge for the immediately previous checksum algorithm. Do not
   accept arbitrary edited historical migrations. Keep unknown versions
   fail-closed before applying migrations.
5. Preserve same-connection atomic business state/history/idempotency/workflow
   and local audit outbox. No network call in the transaction. Receipt proof
   is only accepted from a validated real governance response and is persisted
   before local published state; never turn HTTP 2xx into a receipt.
6. Critical HTTP commands require server-derived tenant/actor context, CSRF,
   request/operation/trace identity, and idempotency. Legacy direct purchase
   writes stay disabled for any configured tenant. Execution, confirmation,
   dispatch, compensation, recovery, and amendment remain disabled without
   opening a transaction or writing. Use compensation_pending, never
   compensating.

Do not invent an aero-id endpoint, governance schema, source registration,
remote receipt, production migration, backup/restore result, or recovery
result. If a requirement depends on unavailable external authority, keep the
adapter fail-closed and document the evidence gap. Do not weaken existing
checks or tests. Run from the target worktree:
  pnpm --filter @sverp/api typecheck
  pnpm --filter @sverp/api test
  pnpm --filter @sverp/api build
  pnpm check
  python /home/dwp/ai-batch-runner/quality.py --strict /home/dwp/workspace/job/sverpweb-stable-platform-workflow

Return only a concise Markdown report; the runner saves it. Use exactly:
## Findings Addressed
## Files Changed
## Tests Executed
## Remaining External Evidence
## Handoff Decision
