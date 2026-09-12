# Phase 5 — sverpweb Approval Slice

**State: IMPLEMENTED_IN_ISOLATED_WORKTREE / LOCAL_CI_GREEN / GATE_REVIEW_PENDING / NOT_MERGED**

## Worktree

```text
repository: /home/dwp/workspace/job/sverpweb-stable
branch:     platform/workflow-phase1
worktree:   /home/dwp/workspace/job/sverpweb-stable-platform-workflow
```

The main sverpweb worktree was not changed or merged. The branch was created
from its clean `d9f1933` HEAD; existing main-worktree changes remain separate.

## Execution through ai-batch-runner

The implementation and repair prompts were run with:

```text
model:    gpt-5.6-luna
provider: openai-codex
thinking: max
```

The implementation task initially timed out after creating a partial slice.
A focused repair run completed the code path. The later adversarial run also
produced additional hardening changes but its supervisor was interrupted
before its report was written; the worktree was then inspected and validated
locally. The runner's rejected-worktree evidence is retained under the target
worktree `.pi-batch/rejected/`; no report is treated as a gate PASS.

Pipeline definitions and prompts are kept in:

```text
examples/snaplink-platform/platform-implementation-pipeline.yaml
examples/snaplink-platform/platform-repair-pipeline.yaml
examples/snaplink-platform/platform-adversarial-review-pipeline.yaml
examples/snaplink-platform/prompts/
```

## Implemented scope

- Module-local `workflow` application/domain/repository layer.
- Versioned, checksummed SQLite migrations and legacy quarantine.
- Purchase aggregate command port; workflow contains no purchase SQL.
- Immutable workflow/approval/purchase history and optimistic versions.
- Claim/release/approve/reject HTTP boundary with CSRF, idempotency,
  request identity and server-side tenant/context checks.
- Requester/self-approval, persisted initiator context, authority fencing,
  expired-claim revalidation, and optimistic version rejection.
- Same-connection transactional redacted audit outbox with validated receipt
  persistence, restart recovery, bounded retry/DLQ and stable identities.
- Versioned migration chain with tenant binding, route-policy snapshots,
  legacy quarantine, receipt-backed immutability, and definition repair.
- Active purchase-approval v1 graph validation; normal HTTP legacy direct-write
  compatibility route removed.
- Execution, compensation, recovery, confirmation and dispatch remain
  explicitly disabled without writes.

## Verification evidence

Executed in the isolated worktree:

```text
pnpm --filter @sverp/api typecheck  PASS
pnpm --filter @sverp/api test       498 pass, 1 skipped, 0 fail
pnpm --filter @sverp/api build      PASS
pnpm check                          9 tasks successful
python /home/dwp/ai-batch-runner/quality.py --strict <worktree>  PASS
```

The single skipped test is the existing cross-repository Snaplink E2E test.
The build still emits the existing Vite chunk-size/dynamic-import warnings;
no warning was promoted to a failure. The latest full code-gate rerun was
interrupted before producing a new report; `phase5/code-gate.md` therefore
still contains the prior FAIL report and must not be treated as approval.

## Not enabled / not evidenced

- No production database migration, backup/restore rehearsal or deployment.
- No live Snaplink/aero-id authority contract fixture.
- No live audit-governance source/schema registration or verified remote
  ledger/index/WORM receipt.
- No shared-instance multi-tenancy.
- No payment, inventory, shipment, execution side effect or compensation.
- No automatic merge into `main`.

A focused implementation pass and local checks are complete. The branch is
not merged and remains blocked from production activation pending a successful
current independent code gate and the external evidence below.
