# Architecture Gate Evidence Bundle (bounded)

> This is a bounded, human-curated evidence digest for independent gate review. It is not a replacement for the source documents.

## Requirement and Scope

- Build a scalable, elastic, high-cohesion/low-coupling platform assembled from contracts, static modules and controlled external plugins.
- First slice: generic Workflow Contract plus sverpweb purchase approval; do not create a new workflow repository yet.
- Include identity/tenant/org, shared assets, permissions, audit, telemetry, automation and Agent provenance.
- Keep current sverpweb single-tenant; no claim of shared-instance multi-tenancy.

## Boundaries

- Snaplink: identity, session, tenant context, roles, permission mapping and revocation.
- aero-id: organization, department, stable department_id and membership (target authority).
- sverpweb-stable: purchase aggregate and first Workflow module/instance/task/decision/execution/compensation state.
- aero-vault: file/media/object/version/ACL/share source of truth; sverpweb stores asset_id reference only.
- aero-im: notification delivery only.
- snaplink-audit-governance: immutable audit facts, operation timeline, hash/WORM, legal hold, restore approval and replay; never business state.
- snaplink-console/iris-ui: management/view and UI primitives; no final authorization.
- OTel/metrics/logs: technical observability; not business audit truth.

## First Slice Lifecycle

```text
PurchaseRequest draft
  -> submitted / pending approval
  -> department approval
  -> optional finance approval
  -> approved (decision only)
  -> execution_pending -> executing
  -> executed/completed
```

Other paths: rejected, returned, expired, cancelled, failed, compensating, compensated.

- `ApprovalDecision` is not `Execution`.
- Cancellation before side effects is not compensation after side effects.
- Business domain owns purchase amount, supplier, budget and actual confirmation command.
- Workflow calls typed commands and does not write purchase tables directly.

## Principal and Permission Rules

- Actors are human, service, automation, agent or system; record actual executor separately from initiator/on_behalf_of/approver.
- Tenant context is server-derived; client tenant fields cannot switch context.
- Department identity is `tenant_id + department_id`; department names are display-only.
- Authorization is backend-enforced and fail-closed for tenant, approval, asset and high-risk commands.
- Agent permission is the intersection of delegator permission, agent profile, task grant, resource ACL and tenant policy.
- Agent grants are short-lived, resource/action scoped, bounded by operations/bytes/cost and revocable; Agent cannot self-approve, self-elevate or use a human long-lived token.

## Audit, Telemetry and Reliability

- Every important request/decision/execution/revocation/recovery has operation_id, correlation_id, causation_id, trace_id, tenant_id, actor provenance and outcome.
- Business change and local audit outbox commit atomically; remote delivery is durable_async with retry/DLQ.
- Authorization is fail_closed; local audit outbox write is fail_closed; notifications are best_effort_async; non-critical Agent/telemetry may degrade.
- Audit events are append-only and immutable. Correction/reversal uses a new event, not deletion.
- audit-governance `accepted -> ledgered -> indexed -> archived` receipts and WORM are evidence states.
- OTel spans/metrics/logs correlate by IDs but cannot replace the audit event; `audit != telemetry`.
- Read/download/preview/denied events follow an explicit volume policy; secrets, tokens, raw prompts and raw file bodies are not audit payloads.

## Plugin and Elasticity Rules

- Kernel contains tenant context, operation context, PEP/capability validation, idempotency, outbox, lifecycle and health; it has no purchase/file business rules.
- Plugins own their domain facts, schema/migrations, API/RPC, events, workers, metrics and tests.
- No cross-project direct DB access, shared ORM entities, ACL dual-write, audit callback into business or arbitrary JSON mutation.
- Trusted modules are statically compiled; heavy/untrusted extensions use typed Unix-socket gRPC, remote production uses TLS/mTLS. No Go .so as the general plugin ABI.
- Manifest declares id/version/contract/profiles/provides/requires/failure policy/security; production requires digest/signature/provenance/SBOM and real startup gates.
- APIs are stateless; workflow/audit/notification workers scale independently; tenant/aggregate partitions, queues, backpressure, timeout, retry, circuit breaker, bulkhead and budgets are required.

## Source Evidence Already Checked

- `docs/snaplink-platform/PLATFORM_ARCHITECTURE.md`
- `docs/snaplink-platform/BUILD_PROFILES_PLUGIN_ARCHITECTURE.md`
- `docs/snaplink-platform/COMBINATION_MODEL.md`
- `docs/snaplink-platform/decisions/01-intake.md` through `05-changes.md`
- `examples/snaplink-platform/platform-map.yaml`
- `snaplink-audit-governance/README.md` and API contracts
- `aero-vault/internal/access` authorization model
- sverpweb audit and purchase route modules

## Review Triage

- Relevant findings accepted: workflow/ACL/audit separation; persistence design gate before new tables; tenant-scoped IDs; agent provenance/least privilege; durable outbox and explicit compensation.
- Rejected: review content that drifted to ai-batch-runner internals; ownership that incorrectly assigned Agent grants to aero-im; unrelated/hallucinated order_item or football content.
- The raw role outputs remain untrusted proposals; insufficient evidence stays Proposed/Missing.

## Gate Questions

1. Is the first implementation bounded to sverpweb plus necessary existing contracts?
2. Are all ownership and collaboration boundaries non-circular?
3. Are workflow, business state, ACL, audit and telemetry distinct?
4. Are tenant, department, Agent and approval permissions fail-closed?
5. Can all accepted/rejected/automated/Agent operations be traced and audited?
6. Are revoke, cancel, compensation and restore distinct and executable by the correct owner?
7. Are plugin and Profile rules real without arbitrary dynamic code?
8. Are failure, migration, compatibility, rollback and test paths sufficient for human approval?
