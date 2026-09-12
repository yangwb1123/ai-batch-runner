You are a focused repair engineer. The current worktree contains a partial implementation of the sverpweb approval-only workflow slice from an interrupted agent run. Work only in the current worktree. Do not commit, reset, clean, checkout, or touch another repository.

Read the current git status and diff, then run the API typecheck first:
  pnpm --filter @sverp/api typecheck
Fix the existing implementation rather than adding broad new features. Resolve every TypeScript error, broken import, incorrect Hono handler type, missing shared SQLite transaction method, and dependency wiring issue. Then run the narrowest relevant Bun tests, followed by the API build if affordable.

Use the design-to-code invariants from this file when reviewing:
/home/dwp/ai-batch-runner/docs/snaplink-platform/phase4/gate-v3.md

The first slice is approval-only and remains single-tenant. Preserve these hard rules:
- workflow code has no purchase_requests SQL and no purchase Database handle;
- purchase writes stay behind the purchase adapter/port;
- approved is not executed;
- execution, compensation and recovery are disabled with no writes;
- use compensation_pending, never compensating;
- tenant/context and approval authority are server-derived and fail closed;
- critical business/workflow state and local redacted audit outbox intent share one SQLite transaction;
- no network call occurs inside that transaction;
- no invented aero-id API or remote receipt claim;
- no weakening existing type, lint or test gates.

Pay attention to compatibility with existing purchase-request store tests and app test fixtures. If the current architecture cannot safely satisfy an invariant, make the behavior fail closed and add or preserve a focused test. Do not remove existing functionality unrelated to this slice.

After repairs run:
  pnpm --filter @sverp/api typecheck
  pnpm --filter @sverp/api test
  pnpm --filter @sverp/api build
If a command is too expensive or fails for an environment dependency, record the exact command and failure without fabricating success. Inspect the final diff for generated files, secrets, direct cross-project access, and accidental changes outside the slice.

Return only a concise Markdown report with exactly:
## Review Result
## Defects Fixed
## Tests Executed
## Remaining Failures
## Evidence Still Required
## Handoff Decision
The runner will save the report; do not write it yourself.
