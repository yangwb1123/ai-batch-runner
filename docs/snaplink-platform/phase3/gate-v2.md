# Architecture Gate Artifact

## Scope decision

- **Primary project:** `sverpweb-stable` — the only selected implementation project and a real entry in `platform-map.yaml`.
- **Workflow repository:** none. Workflow facts remain in `sverpweb-stable`.
- **Current tenancy:** sverpweb remains single-tenant; tenant-scoped identifiers are boundary controls, not a shared-instance multi-tenancy claim.

Collaborators are limited to existing authorities:

| Concern | Authority |
|---|---|
| Authentication, tenant context, permissions, grants, revocation | `snaplink` |
| Organization, departments, memberships | `aero-id` |
| Assets, versions, ACLs, sharing | `aero-vault` |
| Notifications | `aero-im` |
| Audit ledger, WORM, legal hold, governance receipts | `snaplink-audit-governance` |
| Purchase aggregate and workflow state | `sverpweb-stable` |
| Management/view only | `snaplink-console`, `iris-ui` |

## Verification

| # | Result | Finding |
|---|---|---|
| 1 | PASS | Exactly one primary project is selected: `sverpweb-stable`. |
| 2 | PASS | Workflow definition/instance/work-item/decision/execution/compensation state is separate from identity, asset ACL, audit ledger, and purchase state. |
| 3 | PASS | No shared-instance multi-tenancy is claimed for sverpweb. |
| 4 | PASS | Tenant context is server-derived; department identity is `tenant_id + department_id`; names and client-selected tenant switches are not authoritative. |
| 5 | PASS | Approval is distinct from execution; pre-side-effect cancellation is distinct from post-side-effect compensation; irreversible commands must be explicit. |
| 6 | PASS | Human, service, automation, agent, and system provenance is retained. Agent capability is an intersection of delegator, profile, grant, ACL, and tenant policy; grants are bounded, short-lived, revocable, and cannot self-approve or self-elevate. |
| 7 | **BLOCKED** | The required atomic business-state/local-outbox design is specified, but the evidence states that the sverpweb outbox target is not yet connected to audit-governance. Contract comparison and end-to-end receipt evidence are missing. |
| 8 | PASS | The boundaries prohibit direct cross-project database access, ACL dual-write, arbitrary runtime plugins, and long-lived human tokens for agents. |
| 9 | **BLOCKED** | Plugin constraints exist, but concrete first-slice profile, deployment, compatibility, failure, rollback, recovery, migration, and test acceptance artifacts are not supplied. |
| 10 | OPEN | Irrelevant findings are rejected below; relevant gaps require the bounded change plan below. |

## Required bounded change plan

1. **Persistence gate — owner: `sverpweb-stable`**
   - Produce the persistence design before creating workflow tables.
   - Define DTO/domain/persistence/read-model separation, aggregate and transaction boundaries, tenant-scoped keys, idempotency keys, state history, optimistic concurrency, indexes, deletion, and Expand–Migrate–Contract compatibility.
   - Do not alter purchase tables from workflow code.

2. **Workflow contract — owner: `sverpweb-stable`**
   - Define typed commands for submit, approve, reject, return, cancel, execute, fail, compensate, and recovery.
   - Define the complete transition table, idempotency behavior, side-effect boundary, irreversible-command confirmation, timeout, retry, and compensation semantics.
   - Workflow may call business commands but may not write purchase tables.

3. **Authorization and provenance — owners: `snaplink`, `aero-id`, `sverpweb-stable`**
   - Validate server-derived tenant context and department membership on every relevant command.
   - Persist initiator, executor, `on_behalf_of`, approver, actor type, grant ID, and policy decision.
   - Ensure agent grants are Snaplink-owned, resource/action scoped, expiring, revocable, quota-bounded, and rejected for self-approval or self-elevation.

4. **Audit integration — owners: `sverpweb-stable`, `snaplink-audit-governance`**
   - Complete the three-part contract comparison: ingest path, required fields, and active schema.
   - Commit business change and local durable audit outbox atomically.
   - Prove `accepted → ledgered → indexed → archived` receipts, retry/DLQ behavior, idempotency, redaction, and volume policy.
   - Keep audit governance as evidence authority, never business-state authority; OTel/logs/metrics remain non-audit telemetry.

5. **Restore, cancellation, and compensation — owners: `snaplink`, `sverpweb-stable`, audit governance**
   - Snaplink owns revocation.
   - sverpweb owns business cancellation, execution, compensation, reconciliation, and business-state recovery commands.
   - Audit governance owns restore approval, immutable evidence, and replay of audit facts, but must not mutate business state directly.
   - Document that deployment rollback cannot undo irreversible side effects; compensation is the explicit business recovery path.

6. **Profiles and plugins — owner: platform architecture with `sverpweb-stable`**
   - First slice uses a statically compiled trusted module and typed Workflow Contract only.
   - No new plugin repository, Go `.so` ABI, arbitrary runtime code, or arbitrary JSON mutation.
   - Any later external profile must use a declared manifest, contract/version compatibility, digest/signature/provenance/SBOM, startup gates, resource limits, and typed Unix-socket gRPC or TLS/mTLS transport.

7. **Deployment, failure, migration, rollback, recovery, and tests**
   - Add bounded runbooks for timeout, duplicate delivery, outbox outage, audit DLQ, notification degradation, agent revocation, worker restart, schema migration, compatibility rollback, and disaster recovery.
   - Test state transitions, tenant isolation, fail-closed authorization, provenance, grant expiry/revocation, self-approval rejection, atomic outbox behavior, audit receipts, idempotency, cancellation versus compensation, contract compatibility, migration rollback, and plugin startup rejection.

## Review triage disposition

- **Accepted and incorporated:** workflow/ACL/audit separation, persistence gate, tenant-scoped identity, agent least privilege and provenance, durable outbox, explicit compensation.
- **Rejected with evidence:** ai-batch-runner internals are outside the selected platform scope; agent grants belong to `snaplink`, not `aero-im`; unrelated `order_item` and football findings have no requirement or project-map evidence.
- **Not rejected:** audit integration, restore ownership, persistence, and operational plans remain relevant and are covered by the required change plan.

VERDICT: FAIL - The bounded architecture is coherent, but human approval is blocked until audit-governance integration evidence, explicit business-restore ownership, the persistence design, and concrete failure/deployment/rollback/recovery/test acceptance artifacts are supplied.
