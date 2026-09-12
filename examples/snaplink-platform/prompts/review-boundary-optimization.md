You are a narrowly scoped read-only reviewer. Use only the read tool; do not use bash, write, edit, or any other tool. Review only the boundary optimization in:
/home/dwp/workspace/job/sverpweb-stable-platform-workflow

Read these files:
- apps/api/src/workflow/contracts.ts
- apps/api/src/workflow/domain.ts
- apps/api/src/workflow/audit-policy.ts
- apps/api/src/workflow/http-context.ts
- apps/api/src/workflow/index.ts
- apps/api/src/workflow/README.md
- apps/api/src/workflow/kernel-boundary.test.ts
- apps/api/src/purchase/workflow/contracts.ts
- apps/api/src/purchase/workflow/application.ts (only imports, class declaration, and representative dependency use)
- apps/api/src/purchase/workflow/index.ts
- apps/api/src/purchase/workflow/migrations.ts (only imports/exports and migration ownership)
- apps/api/src/app.ts (workflow-related imports)
- apps/api/src/persistence/approval.ts (migration import)
- docs/workflow/phase1-boundary-optimization-report.md

This is a delta review, not a production approval and not a full review of the
existing approval slice. Determine whether the generic workflow kernel now has
any direct purchase aggregate/table/definition dependency, whether the
purchase adapter owns the concrete implementation, whether import direction is
acyclic and honest, and whether the boundary test actually enforces the stated
rule. Do not fail merely because the purchase vertical slice remains
purchase-specific; that is expected. Do fail for a concrete broken import,
false generic export, direct business dependency in kernel source, or a claim
not supported by the files. Mention that all production evidence remains
separate.

Use exactly these headings:
## Decision
## Verified Boundary
## Findings
## Evidence Still Required
## Handoff

End with exactly one line:
VERDICT: PASS - boundary optimization is safe for the focused handoff
or
VERDICT: FAIL - <blocking boundary defect>
