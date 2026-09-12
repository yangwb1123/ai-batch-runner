## Decision

**PASS for design-to-code handoff only.** `sverpweb-stable` is the sole primary project. The documents define clear ownership, server-side security boundaries, approval-only scope, transactional outbox intent, and concrete implementation gaps. The `BLOCKED` statuses describe missing implementation and production evidence, not a design contradiction.

The execution graph in phase 4 is treated as a later, separately enabled target. This handoff does not authorize execution.

## Scope Approved for Code

- Implement only in `sverpweb-stable`.
- Preserve the current Bun/SQLite, single-tenant deployment model.
- Implement draft creation, submission, human work-item claim/approval, approve/reject decisions, immutable histories, optimistic concurrency, and command idempotency.
- Workflow may write only workflow-owned facts and must invoke `PurchaseAggregateCommandPort` for purchase mutations.
- Implement the local `audit_outbox` in the same SQLite transaction as each critical business transition.
- Replace the current best-effort critical audit path; remote relay and receipt claims remain gated by active governance contracts.
- Missing or ambiguous routing authority, tenant context, department membership, or provenance must reject the operation.

## Non-negotiable Invariants

- `sverpweb-stable` remains the only primary project; no workflow repository, shared database, or service extraction is introduced.
- Workflow code has no purchase-table SQL, purchase database handle, identity-table access, ACL-table access, or audit-ledger access.
- `approved` means approval completed only. It never implies `executable`, `executing`, `executed`, or `completed`.
- Execution confirmation, dispatch, execution, compensation, and recovery are disabled in this slice.
- Tenant, department, actor, initiator, delegation, policy, and provenance are resolved or checked server-side and fail closed.
- Agent capability is bounded by principal, profile, tenant, resource, action, policy, expiry, budget, and revocation state. Agents cannot approve, self-elevate, or alter grants.
- The local outbox is transactionally coupled to business state, histories, and idempotency completion. Remote outage leaves `pending`; it cannot fabricate `accepted`, `ledgered`, `indexed`, or `archived`.
- `compensation_pending` is the only uncertain-effect state vocabulary; `compensating` is prohibited.
- Cancellation requires verified no effect. An uncertain or completed effect requires reconciliation or compensation.
- Audit replay, restore, telemetry, or deployment rollback cannot directly mutate business state.

## Required Implementation Tasks

| Owner | Task | Required result |
|---|---|---|
| `sverpweb-stable`, platform architecture | Freeze the slice contract | Reconcile `WORKFLOW_CONTRACT_V1` with phase-4 states, add explicit `executable` semantics for the future path, and reject all disabled execution/compensation commands without writes. |
| `sverpweb-stable` | Normalize persistence design | Reconcile proposed schemas, including `schema_id`, `sverpweb-stable` source identity, audit idempotency uniqueness, delivery states, and tenant-scoped constraints. |
| `sverpweb-stable` | Implement migrations and ownership boundaries | Add checksum migrations, baseline/backfill/quarantine steps, four-model separation, histories, versions, idempotency, explicit projections, and removal of direct `setStatus` writes. |
| `sverpweb-stable` | Implement approval workflow | Add typed purchase port calls, centralized transitions, claim/version checks, immutable decisions, approval-epoch handling, and segregation-of-duties enforcement. |
| `snaplink`, `aero-id`, `sverpweb-stable` | Implement security context | Derive the single tenant server-side; verify membership, department, actor provenance, delegation, policy, and grant status at claim and decision time. |
| `sverpweb-stable`, `snaplink-audit-governance` | Implement audit adapter | Compare and register the active source/schema/auth contracts; enqueue redacted canonical events atomically; relay post-commit with stable event identity, retry, lease reclaim, DLQ, and receipt validation. |
| `sverpweb-stable`, operations, audit governance | Implement recovery controls | Produce backup/restore, migration rollback, compatibility, outbox reconciliation, retention/legal-hold, and incident runbooks with explicit ownership. |
| `sverpweb-stable`, authority projects, operations | Execute acceptance tests | Cover tenant isolation, ownership scans, concurrency, idempotency, SoD, outbox rollback, relay failures, redaction, migration, restore, artifact rejection, and disabled-command behavior. |

## Evidence Still Required

- Actual SQLite path, schema fingerprint, row counts, status counts, migration ledger, backup, and integrity-check evidence.
- Executed proof that workflow has no forbidden database or ACL dependency.
- Server-side tenant extraction and authoritative `aero-id` membership/department contracts.
- Snaplink authorization, grant, revocation, provenance, and SoD fixtures.
- Active governance source registration, signed client binding, event schemas, retention policy, ingest authentication, and receipt contract.
- Failure-injection proof that business state, history, idempotency, and outbox commit or roll back together.
- Relay evidence for unchanged event IDs, retries, DLQ handling, duplicate delivery, and verified receipt stages.
- Executed migration, rollback, backup/restore, orphan-reconciliation, and legacy-pending quarantine reports.
- Executed test reports marked honestly as `executed`, `passed`, `failed`, or `not_executed` with reasons.
- Signed/locked artifact manifest, digest, signature, provenance, SBOM, inventory, and negative startup-test evidence.
- Approved quantity/routing semantics and retention, privacy-erasure, and legal-hold policies.

## Explicitly Not Approved

- Production readiness or closure of the phase-3 gate.
- Payment, shipment, inventory reservation, arbitrary procurement commands, execution confirmation, dispatch, execution, compensation, or recovery in this slice.
- Any direct cross-project database access, shared ORM entity, direct ACL mutation, or client-selected tenant/department.
- Runtime plugins, arbitrary scripts, arbitrary JSON transitions, or unbounded Agent privileges.
- Treating local outbox insertion, HTTP `202`, logs, OTel, or local `published` state as governance ledger evidence.
- Using `compensating`, collapsing approval into execution, or using restore/rollback to undo external effects.
- Destructive migration, physical deletion of histories, or automatic purge before policy approval.

## Handoff

Implementation may begin with contract normalization, baseline capture, and authority wiring, followed by the approval workflow and same-database outbox. The readiness summary must remain `BLOCKED` for production until the listed evidence is executed and attached. Any future execution enablement requires a new scoped gate proving the `approved → executable → execution_pending → executing` boundary and compensation contract.

VERDICT: PASS - bounded design-to-code handoff is ready
