## Verified Current Interfaces

Evidence is classified as **Verified**, **Proposed**, or **Missing**. The current implementation must not be inferred from the proposed phase-4 tables.

| Boundary | Verified interface or fact | Status |
|---|---|---|
| Gate-v2 | `docs/snaplink-platform/phase3/gate-v2.md` explicitly blocks audit integration because the local outbox is not connected to governance and receipt evidence is missing. | Verified blocker |
| Business state | `sverpweb-stable` owns purchase and workflow state. Audit governance must not mutate it. | Verified |
| Current audit model | `AuditEvent` uses local `schemaVersion: 1`, UUID `id`, numeric millisecond timestamps, `OperationContext`, `before`, `after`, and arbitrary `metadata`. | Verified |
| Current request path | `auditRequestMiddleware` calls `next()` first. Domain handlers may call `emitAuditEvent`; otherwise a generic HTTP event is appended after the response. Sink errors are logged and swallowed. | Verified |
| Current sink API | `AuditEventSink.append()` and `appendMany()` have no caller-owned SQLite transaction parameter. | Verified |
| Current governance client | `AuditGovernanceClient` only calls `GET /api/v1/events?...`; it does not implement event ingestion or receipt retrieval. | Verified |
| Governance REST ingest | `POST /api/v1/events?wait_for=accepted|ledgered|indexed|archived` returns `202` with a receipt. | Verified |
| Governance batch ingest | `POST /api/v1/events:batch`, maximum 500 events. | Verified |
| Governance RPC ingest | `snaplink.audit.v1.Ingest.Write`, `WriteBatch`, and `WriteStream`. | Verified |
| Governance receipt | `GET /api/v1/events/{eventId}/receipt?tenant_id=...` returns status, timestamps, stream, sequence, hash, and duplicate/conflict indicators. | Verified |
| Governance authority | Governance stores immutable ledger, hash-chain, projection, WORM, retention, and legal-hold evidence. It is not the business-state authority. | Verified |
| OTel | OTel carries technical trace correlation only. It is not an audit record or recovery source. | Verified |
| Local outbox | Phase-4 `audit_outbox`, attempt, and receipt tables are target designs, not evidence of the currently deployed sverpweb schema. | Proposed |
| Source registration | Governance requires a tenant-scoped source registration and exact signed OAuth `client_id` binding. The active registration for sverpweb was not supplied. | Missing |

The current `AuditEventSink` path is therefore a best-effort emitter. It is not evidence of a transactional durable outbox, even if one implementation behind the interface happens to use SQLite.

## Contract Comparison

**Part 1 — ingest path**

| Concern | Current `sverpweb-stable` | Active governance | Finding |
|---|---|---|---|
| Transport | No POST/RPC ingestion implementation is shown. The supplied client performs only `GET /api/v1/events`. | REST, batch REST, and gRPC ingest are active. | **Gap** |
| Transaction coupling | `emitAuditEvent()` is called from the request path and catches append failures. Generic fallback runs after `next()`. | Governance expects a producer-side Transactional Outbox. | **Gap** |
| Wire object | `AuditIngestRequest` contains `source`, numeric `sentAt`, and `events: unknown[]`. | `EventInput` requires a governance `EventEnvelope`. | **Incompatible without an adapter** |
| Relay storage | Proposed sverp storage is Bun SQLite. | Reference `audit-outbox-relay` requires a PostgreSQL `audit_outbox` DSN and PostgreSQL JSONB schema. | The Go relay cannot be assumed to drain sverp SQLite atomically. |
| Selected path | None currently connected. | Use `POST /api/v1/events?wait_for=ledgered` from a post-commit Bun relay. | Smallest viable path |

The selected design is **SQLite business transaction → SQLite audit outbox → Bun relay → HTTPS governance ingest**. A second PostgreSQL outbox would break the required local atomicity unless a distributed transaction is introduced, which is explicitly out of scope.

**Part 2 — required fields and schema**

| Governance field | Current sverp field | Result |
|---|---|---|
| `event_id` | `AuditEvent.id` | Present, but stability across retries is not proven. Persist it before commit. |
| `tenant_id` | No `OperationContext.tenantId` | Missing. Must be server-derived and persisted locally. |
| `source_system` | `OperationContext.source`: `web`, `worker`, `system` | Not a registered governance source identifier. Use a fixed registered value such as `sverpweb-stable`. |
| `event_type` | `AuditEvent.eventType` | Present, but current middleware uses `http.request.completed.v1`; target convention is `domain.resource.action@vN`. |
| `schema_id` | None | Missing. |
| `schema_version` | Local numeric `1` | Present, but local version `1` is not automatically governance schema version `1`. |
| `occurred_at` | `Date.now()` milliseconds | Unit mismatch. Governance requires UTC RFC3339 timestamp text. |
| `actor` | `actorUserId` and optional department snapshot | Partial. Actor type and non-null identity are not guaranteed. |
| `initiator` / `executor` / `approver` | None | Missing from the current event model. |
| `operation_id` | `operation.operationId` | Present, but currently randomly generated per request and not transactionally persisted. |
| `causation_id` / `correlation_id` | None | Missing. |
| `trace_id` | `traceId` | Partial. `traceparent` is currently copied without demonstrated W3C parsing. |
| `aggregate_type/id/version` | Present | Compatible after stable-version enforcement. |
| `action` | Present | Compatible. |
| `outcome` | Currently in `metadata` | Must be promoted to the envelope. |
| `reason` | No safe reason-code field | Add a bounded code/digest; do not send raw rejection text by default. |
| `changed_fields` | `string[]` | Shape mismatch. Governance expects an object such as `{status:{before,after}}`. |
| `payload` / `payload_ref` | `before`, `after`, `metadata` | Can be adapted, but current arbitrary JSON is not redaction- or size-enforced. |
| `data_classification` | None | Missing. |
| `retention_class` | None | Missing. |
| `idempotency_key` | Nullable | Active governance requires it. Critical commands must reject its absence. |
| `workflow_instance_id` | None in current context | Required for workflow events. |
| `execution_run_id` | None | Required when execution is enabled. |
| `received_at`, `stream_id`, `sequence`, `prev_hash`, `hash` | None | Correctly server-generated; sverp must not fabricate them. |

The active OpenAPI `EventInput` requires:

```text
event_id
source_system
event_type
schema_id
schema_version
occurred_at
actor
action
outcome
data_classification
retention_class
idempotency_key
```

The active AsyncAPI envelope additionally requires `payload` or `payload_ref`. Sverp will always provide a bounded `payload`.

**Part 3 — active schema and compatibility gap**

The draft `AUDIT_PROVENANCE_V1.md` and `EXECUTION_CONTEXT_V1.md` are not proof that the active governance wire schema contains all proposed provenance fields.

The active REST schema and `audit.proto` do not expose top-level fields for:

```text
initiator
executor
approver
delegation_id
capability_grant_id
policy_id
policy_version
approval_id
payload_digest
```

The active envelope does expose `payload`, `workflow_instance_id`, and `execution_run_id`. The smallest compatibility change is therefore:

1. Register a tenant-scoped source for `sverpweb-stable`.
2. Register one event schema per event type.
3. Put the missing provenance and `payload_digest` under a versioned `payload.provenance` object.
4. Add contract tests proving that the registered schema accepts the complete payload and rejects missing high-risk provenance.

If the governance schema registry cannot enforce nested payload requirements, governance must make an additive compatibility change: introduce optional `provenance` and `payload_digest` fields in the REST/OpenAPI/AsyncAPI envelope and protobuf, then register a new event schema version. Unknown top-level fields must not be silently relied upon.

## Event and Provenance Mapping

The following identifiers are proposed wire values, not currently registered evidence:

```text
source_system: sverpweb-stable
schema_id:     sverp.purchase.request.approved
schema_version: 1
```

The source identifier must be registered exactly; `web` is only a local source category.

| Source context | Governance mapping |
|---|---|
| Authenticated user subject | `actor.id`; never a local username |
| Actual actor type | `actor.type`: `human`, `service`, `automation`, `agent`, or `system` |
| Original requester | `payload.provenance.initiator` |
| Actual service/worker performing the command | `payload.provenance.executor` |
| Human approval subject | `payload.provenance.approver` |
| Delegation | `payload.provenance.delegation_id` |
| Capability grant | `payload.provenance.capability_grant_id` |
| Policy decision | `payload.provenance.policy_id` and `policy_version` |
| Workflow approval decision | `payload.provenance.approval_id` |
| Domain operation | `operation_id` |
| Direct prior event | `causation_id` |
| Business chain | `correlation_id` |
| Technical trace | `trace_id`; telemetry only |
| Server tenant context | `tenant_id`; request body/header is never authoritative |
| Local redacted payload digest | `payload.provenance.payload_digest` |
| Governance source digest and chain hash | Server-returned `source_digest` and receipt `hash` |

A proposed approval event, after adaptation, is:

```json
{
  "event_id": "evt-approval-01",
  "tenant_id": "sverp",
  "source_system": "sverpweb-stable",
  "event_type": "purchase.request.approved@v1",
  "schema_id": "sverp.purchase.request.approved",
  "schema_version": 1,
  "occurred_at": "2026-08-29T10:20:30.123Z",
  "operation_id": "op-approval-01",
  "causation_id": "evt-approval-recorded-01",
  "correlation_id": "corr-purchase-01",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "actor": {
    "id": "user-approver",
    "type": "human",
    "department": "department-42"
  },
  "targets": [
    {
      "type": "purchase_request",
      "id": "purchase-01"
    }
  ],
  "aggregate_type": "purchase_request",
  "aggregate_id": "purchase-01",
  "aggregate_version": 3,
  "workflow_instance_id": "workflow-01",
  "action": "approve",
  "outcome": "success",
  "reason": "approval.completed",
  "changed_fields": {
    "status": {
      "before": "pending_approval",
      "after": "approved"
    }
  },
  "payload": {
    "provenance": {
      "initiator": {
        "type": "human",
        "id": "user-requester"
      },
      "executor": {
        "type": "service",
        "id": "sverpweb-stable"
      },
      "approver": {
        "type": "human",
        "id": "user-approver"
      },
      "delegation_id": null,
      "capability_grant_id": null,
      "policy_id": "purchase-approval",
      "policy_version": "1",
      "approval_id": "decision-01",
      "payload_digest": "sha256:redacted-payload-digest"
    },
    "safe_state": {
      "purchase_version_before": 2,
      "purchase_version_after": 3
    }
  },
  "data_classification": "internal",
  "retention_class": "standard",
  "idempotency_key": "audit:op-approval-01:purchase.request.approved:v3"
}
```

A rejection uses the same envelope with:

```text
event_type: purchase.request.rejected@v1
action: reject
outcome: denied
reason: approval.policy_denied
```

The raw rejection explanation remains in the business history according to business policy; it is not copied into the audit payload by default.

`received_at`, `stream_id`, `sequence`, `prev_hash`, `hash`, and `source_digest` are omitted from the submitted event. They are governance evidence and must come only from the receiving service.

## Atomic Outbox Design

The smallest implementation change is a versioned SQLite migration in `sverpweb-stable` plus a transaction-aware audit port.

The local table must be in the same SQLite database and transaction as the purchase/workflow mutation:

```text
audit_outbox
  outbox_id
  tenant_id
  event_id
  event_key
  source_system
  event_type
  schema_id
  schema_version
  occurred_at
  operation_id
  correlation_id
  causation_id
  aggregate_type
  aggregate_id
  aggregate_version
  workflow_instance_id
  execution_run_id
  actor_type
  actor_id
  idempotency_key
  payload_json
  payload_digest
  data_classification
  retention_class
  delivery_state
  attempt_count
  next_attempt_at
  lease_until
  last_error_code
  last_error_digest
  remote_event_id
  created_at
  accepted_at
  ledgered_at
  indexed_at
  archived_at
```

Required uniqueness:

```text
UNIQUE (tenant_id, event_id)
UNIQUE (tenant_id, event_key)
UNIQUE (tenant_id, idempotency_key)
```

Additional append-only tables record delivery attempts and verified receipts:

```text
audit_outbox_attempts:
  attempt_id, outbox_id, attempt_no, started_at, completed_at,
  result, remote_status_code, remote_receipt_digest,
  error_code, error_digest

audit_outbox_receipts:
  receipt_id, outbox_id, stage, remote_receipt_id,
  receipt_digest, received_at
```

The current `AuditEventSink.append()` must be replaced or augmented by a caller-owned transaction API, for example:

```text
appendInTransaction(sqliteTransaction, event)
```

The first-slice approval transaction is:

```text
BEGIN IMMEDIATE

validate server-derived tenant, actor, policy and idempotency key
insert operation_idempotency
  - duplicate same digest: replay prior result
  - duplicate different digest: rollback with conflict

update purchase_requests with expected status/version
insert purchase_request_status_history

update workflow instance/work item and insert workflow history
insert approval decision where applicable

insert one or more redacted audit_outbox rows
mark operation_idempotency succeeded

COMMIT
```

Any failure before commit rolls back business state, history, idempotency completion, and audit outbox rows together.

No HTTP, gRPC, identity, organization, notification, or OTel call occurs inside this transaction.

After commit, a Bun relay claims the outbox row and sends:

```http
POST https://<audit-governance>/api/v1/events?wait_for=ledgered
Authorization: Bearer <service-token>
Content-Type: application/json
```

The existing post-handler `emitAuditEvent()` path must not be used as the durability mechanism for purchase/workflow transitions. A generic HTTP fallback may be retained only as separately classified diagnostic telemetry; it cannot satisfy the audit gate.

## Delivery State Machine

The local and remote states are distinct:

```text
pending
  → sending
      → accepted
          → ledgered
              → indexed
                  → archived

sending --retryable failure--> pending
sending --permanent failure--> dead_letter
sending --lease expiry------> pending
```

`failed` in the current TypeScript type is insufficiently precise. It should become an attempt result or be migrated to an explicit retryable state; `dead_letter` is the terminal quarantine state.

| Transition | Evidence required | Local action |
|---|---|---|
| `pending → sending` | Lease acquired with compare-and-set | Increment attempt, set lease |
| `sending → accepted` | Actual governance receipt with matching `event_id` and `accepted_at` | Store receipt and accepted stage |
| `accepted → ledgered` | `ledgered_at` plus non-empty `hash`, `stream_id`, and `sequence` | Store receipt and ledgered stage |
| `ledgered → indexed` | Receipt contains `indexed_at` or status `indexed` | Store indexed stage |
| `indexed → archived` | Receipt contains `archived_at` or status `archived` | Store archived stage |
| `sending → pending` | Network, timeout, 429, 5xx, or unverified 2xx | Preserve event bytes and retry time |
| `sending → dead_letter` | Permanent validation/auth/schema/conflict failure or exhausted retries | Preserve bounded error code/digest and stop automatic delivery |
| Any leased state → `pending` | Lease expired without a terminal receipt | Reclaim exactly once by compare-and-set |

The selected HTTP relay uses `wait_for=ledgered`. A `202` status alone is not evidence of ledgering. The relay must validate the receipt body before advancing local state.

A duplicate governance response is safe only when the receipt has the same event ID, tenant, idempotency key, and compatible content. The relay never changes an event ID or idempotency key between attempts.

## Retry DLQ and Idempotency

Command idempotency and event-delivery idempotency are separate:

| Key | Purpose |
|---|---|
| Business command idempotency key | Prevents duplicate purchase/workflow transitions |
| `event_id` | Identifies one immutable audit fact |
| Audit `idempotency_key` | Prevents duplicate governance ledger facts |
| `operation_id` | Correlates multiple facts in one business operation |

Multiple events may share an `operation_id`, but each event must have a distinct audit idempotency key. A suitable deterministic form is:

```text
audit:<operation_id>:<event_type>:<aggregate_version>
```

The same event is retried with the same event ID, canonical payload, and idempotency key. A duplicate command with the same request digest returns the original business and audit references without inserting another event.

Delivery classification:

| Failure | Classification | Behavior |
|---|---|---|
| Missing tenant, actor, schema, classification, retention, or idempotency key | Permanent local validation | Roll back the business transaction; do not enqueue |
| DNS, connection reset, network unavailable, timeout | Retryable | Exponential backoff with jitter |
| HTTP `429` | Retryable | Backoff using server throttling information when available |
| HTTP `5xx` | Retryable | Backoff |
| HTTP `401` | Retryable but alerting | Retry boundedly for credential rotation; auth-blocked backlog requires operator action |
| HTTP `3xx` or malformed/unverifiable `2xx` receipt | Retryable | Never treat as accepted or ledgered |
| HTTP `400` invalid envelope | Permanent | Dead-letter |
| HTTP `403` authorization/source permission failure | Permanent until configuration change | Dead-letter and alert |
| HTTP `404` missing source/schema/resource | Permanent until configuration change | Dead-letter |
| HTTP `409` conflicting event or idempotency key | Permanent | Dead-letter; never overwrite |
| HTTP `422` schema or time-range failure | Permanent | Dead-letter |
| HTTP `422 tenant_mismatch` | Scope-blocked | Pause and alert; never retag the event or change its tenant |
| Local relay database failure | Retryable | Keep the row pending; do not drop it |
| Maximum attempts exhausted | Terminal | Move to `dead_letter` with bounded diagnostic evidence |

The reference governance relay uses bounded retries and dead-lettering; its HTTP implementation treats `401` and `429` as retryable, most other 4xx responses as permanent, and verifies the event ID, ledgered status, ledger timestamp, and hash before marking delivery successful.

DLQ replay must resend the original canonical event, not reconstruct a new event. Replay is allowed only after the underlying permanent cause is corrected. An audit DLQ replay cannot mutate purchase or workflow state.

## Receipt and Integrity Evidence

A valid HTTP receipt is:

```json
{
  "receipt": {
    "event_id": "evt-approval-01",
    "tenant_id": "sverp",
    "idempotency_key": "audit:op-approval-01:purchase.request.approved:v3",
    "status": "ledgered",
    "accepted_at": "2026-08-29T10:20:30.200Z",
    "ledgered_at": "2026-08-29T10:20:30.240Z",
    "indexed_at": null,
    "archived_at": null,
    "stream_id": "sverp-purchase-purchase-01",
    "sequence": 42,
    "hash": "sha256:ledger-chain-hash",
    "duplicate": false,
    "conflict": false
  },
  "receipt_url": "/api/v1/events/evt-approval-01/receipt"
}
```

The relay records only bounded receipt fields and a digest of the canonical receipt. It must not retain bearer tokens or unbounded response bodies.

Evidence levels:

| Evidence | What it proves | What it does not prove |
|---|---|---|
| Local outbox committed | Business mutation and durable local audit intent committed atomically | Governance accepted or ledgered |
| `accepted_at` and matching event ID | Governance accepted the event | Hash-chain ledgering |
| `ledgered_at`, stream, sequence, and hash | Event entered the immutable governance ledger | Projection or WORM archive |
| `indexed_at` | Query projection is available | Immutable authority; projection is rebuildable |
| `archived_at` | WORM archival completed | Business state changed |
| `GET /api/v1/events/{eventId}/receipt` | Current server receipt can be rechecked | Business state authority |
| `GET /api/v1/operations/{operationId}/timeline` | Governance operation correlation | Complete business state reconstruction |
| `POST /api/v1/integrity/verify?tenant_id=...` | Chain integrity verification result | Authorization to mutate sverp |

The following verification sequence is required:

```text
POST /api/v1/events?wait_for=ledgered
→ validate receipt.event_id == local event_id
→ validate receipt.idempotency_key
→ validate ledgered_at, stream_id, sequence, hash
→ GET /api/v1/events/{eventId}/receipt
→ GET /api/v1/operations/{operationId}/timeline
→ GET /api/v1/events/{eventId}
→ POST /api/v1/integrity/verify?tenant_id=...
```

The local payload digest, governance `source_digest`, and governance chain `hash` are different values and must not be conflated.

## Redaction and Retention

The current `before`, `after`, and arbitrary `metadata` fields are not sufficient redaction controls. The adapter must use an allowlist.

Allowed by default:

```text
tenant ID
source system
aggregate/workflow/execution IDs
versions
status transition
safe reason code
policy ID/version
approval ID
delegation/grant IDs
payload digest
bounded department code
```

Forbidden:

```text
passwords
access/refresh tokens
authorization headers
session contents
raw request bodies
raw Agent prompts or model context
private keys and credentials
raw capability grants
file contents
unbounded rejection text
```

For a status change, send:

```json
{
  "status": {
    "before": "pending_approval",
    "after": "approved"
  }
}
```

Do not send the entire purchase row unless a separately approved encrypted `payload_ref` contract exists. Local and governance payload limits must be enforced before insertion; a 64 KiB local payload limit is a safe first-slice bound even though governance supports larger events.

Retention rules:

- Business history and audit facts are not physically deleted by ordinary cleanup.
- Local outbox rows remain until a verified `ledgered` receipt plus an approved operational grace period.
- Unledgered and dead-letter rows are never automatically purged.
- Legal hold prevents local cleanup and governance retention actions.
- Governance WORM retention, archive configuration, and legal hold remain the long-term evidence authority.
- `retention_class` must come from an approved tenant policy; `standard` in an example is not proof that sverp has an approved policy.
- The governance retention policy is configured through `PUT /api/v1/policies/retention?tenant_id=...`.

## High-risk Fail-closed Policy

The following conditions block a critical business transition before commit:

```text
missing or ambiguous server tenant
missing authenticated actor or initiator
tenant/body/header mismatch
missing idempotency key
missing operation ID
missing aggregate version
missing required workflow or approval provenance
missing source/schema registration
redaction or canonicalization failure
audit_outbox insert failure
outbox uniqueness conflict
invalid event schema or size
```

Critical first-slice transitions are:

```text
purchase create
purchase submit
workflow approval
purchase approve
purchase reject
workflow cancellation
```

For these transitions:

- A local outbox failure rolls back the business transaction.
- A remote governance outage after local commit leaves the business result committed with `audit_delivery_state=pending`; it must not be falsely reported as ledgered.
- A workflow transition requiring completion evidence, especially `executed → completed`, is blocked until the required governance receipt is verified.
- A remote 4xx does not cause an audit callback to rewrite business state.
- Governance replay, export, restore preview, and integrity verification never directly mutate sverp state.
- OTel exporter failure degrades telemetry only; it does not authorize, deny, commit, or reconstruct a business fact.
- A trace ID is a correlation aid, never a substitute for `operation_id`, audit receipt, or ledger hash.
- The audit ledger is immutable evidence authority, never current business-state authority.

## Compatibility and Rollout

The local TypeScript `schemaVersion: 1`, governance OpenAPI contract version `1`, protobuf package `snaplink.audit.v1`, and event `schema_version: 1` are separate version namespaces. The adapter must send an explicit `schema_id` and must not assume that local version `1` is compatible with every governance schema version `1`.

Required receiving configuration:

```http
POST /api/v1/sources?tenant_id=<configured-tenant>
```

```json
{
  "id": "sverpweb-stable",
  "name": "sverpweb-stable business service",
  "allowed_client_ids": ["<signed-oauth-client-id>"],
  "active": true
}
```

Register each event type through:

```http
POST /api/v1/schemas?tenant_id=<configured-tenant>
```

For example:

```json
{
  "schema_id": "sverp.purchase.request.approved",
  "version": 1,
  "event_type": "purchase.request.approved@v1",
  "required_fields": [
    "event_id",
    "source_system",
    "event_type",
    "schema_id",
    "schema_version",
    "occurred_at",
    "operation_id",
    "actor",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "action",
    "outcome",
    "payload",
    "data_classification",
    "retention_class",
    "idempotency_key"
  ],
  "allowed_fields": [
    "tenant_id",
    "causation_id",
    "correlation_id",
    "trace_id",
    "targets",
    "workflow_instance_id",
    "execution_run_id",
    "reason",
    "changed_fields",
    "payload"
  ],
  "classification": "internal",
  "active": true
}
```

The governance source must resolve tenant from signed identity/source binding. A body `tenant_id` is only a selector or consistency assertion; it is never an authority override.

The rollout sequence is:

1. Register source, client binding, event schemas, retention policy, HTTPS/JWT or mTLS configuration.
2. Add versioned SQLite migrations for outbox, attempts, receipts, and command idempotency.
3. Add a canonical mapper from the local event model to active `EventInput`.
4. Add a Bun relay that reads SQLite after commit and validates receipts.
5. Run contract tests against governance using a disposable tenant.
6. Switch create/submit/approve/reject to the transaction-aware outbox port.
7. Disable the current best-effort emitter for critical events.
8. Monitor pending, retry, permanent, and dead-letter counts.
9. Verify operation timelines and ledger integrity before enabling execution completion.

Rollback must retain the additive outbox schema. A previous binary that bypasses the outbox is not rollback-safe for critical writes; rollback must use a compatible binary or stop those writes. Already ledgered events are immutable and are not deleted during rollback.

## Missing Evidence and Owners

| Missing evidence | Owner | Required proof |
|---|---|---|
| Actual sverp SQLite path, schema, migration ledger, backup and restore procedure | `sverpweb-stable` | Schema fingerprint, migration checksum, restore rehearsal |
| Actual implementation behind `AuditEventSink` | `sverpweb-stable` | Code review and transaction-injection test |
| Proof that current sink is transactionally coupled | `sverpweb-stable` | Failure-before-outbox rollback test; current supplied files do not provide it |
| Production tenant ID and source registration | Audit governance/platform operations | Source row and exact tenant/client binding |
| Signed OAuth `client_id`, issuer, audience, JWKS/mTLS configuration | Snaplink and operations | Successful authenticated ingest and negative tenant tests |
| Active per-tenant event schemas | Audit governance | `GET /api/v1/schemas?tenant_id=...` evidence and schema validation tests |
| Nested `payload.provenance` enforcement | Audit governance | Missing-provenance rejection test, or additive envelope compatibility release |
| Authoritative actor, initiator, executor, delegation, grant and policy values | Snaplink, `aero-id`, `sverpweb-stable` | Context construction and approval decision fixtures |
| Current department/membership authority | `aero-id` | Decision-time membership and position verification |
| Exact retention class and legal-hold policy | Audit governance, security, business owner | Approved policy and hold test |
| Outbox relay deployment against SQLite | `sverpweb-stable` | Crash/restart, lease reclaim, retry and DLQ evidence |
| End-to-end ledger receipt | `sverpweb-stable` and audit governance | Matching event ID, idempotency key, ledger timestamp, stream, sequence, and hash |
| Archive/WORM receipt | Audit governance and operations | `archived_at`, object digest, integrity verification |
| Recovery ownership | `sverpweb-stable` plus audit governance | Business recovery uses sverp commands; audit replay never mutates business state |
| Current generic HTTP event policy | `sverpweb-stable` | Explicit decision to disable, classify, or separately route generic events |

## Audit Acceptance Matrix

| ID | Acceptance condition | Evidence required | Status |
|---|---|---|---|
| A-01 | Ingest path is explicit | `POST /api/v1/events?wait_for=ledgered` or `Ingest.Write` contract test | Design complete; current path not connected |
| A-02 | Current query client is not treated as an emitter | Code inspection showing it only calls `GET /api/v1/events` | Verified |
| A-03 | Business mutation and local outbox commit atomically | Injected outbox failure rolls back business state, history, idempotency, and outbox | Required |
| A-04 | Local relay does not use a second non-transactional database | Relay reads the same sverp SQLite outbox | Required |
| A-05 | Active required fields are supplied | Wire validation covers all governance required fields and payload/ref | Required |
| A-06 | Tenant is server-derived | Body/header tenant mismatch is rejected; source/client binding is verified | Required |
| A-07 | Provenance is complete | Initiator, executor, actor, approver, policy, delegation, and grant mapping test | Required |
| A-08 | Schema compatibility is real | Registered source/schema evidence; missing nested provenance is rejected | Required |
| A-09 | Event versioning is unambiguous | `schema_id` and numeric `schema_version` are distinct from local and transport versions | Required |
| A-10 | Retry preserves identity | Repeated delivery sends identical event ID, canonical payload, and idempotency key | Required |
| A-11 | Permanent failures reach DLQ | 400/403/409/schema 422 tests produce terminal quarantine | Required |
| A-12 | Retryable failures remain retryable | Network, 401, 429, 5xx, timeout, and invalid receipt tests | Required |
| A-13 | Receipts are not fabricated | Local state advances only from matching governance receipts | Required |
| A-14 | Ledger integrity is evidenced | Receipt hash/sequence plus event, timeline, and integrity API checks | Required |
| A-15 | Redaction is enforced | Payload inspection proves absence of tokens, credentials, prompts, raw bodies, and grants | Required |
| A-16 | OTel remains telemetry only | OTel outage does not alter authorization or business/audit facts | Required |
| A-17 | Audit cannot mutate business state | Replay, restore, and correction tests use normal sverp commands only | Required |
| A-18 | Rollback is safe | Old binaries cannot bypass critical outbox writes; additive migration remains readable | Required |
| A-19 | Full end-to-end path works | SQLite transaction → relay → governance ledger → receipt → query/integrity/archive | Required |
| A-20 | Gate-v2 blocker disposition | All A-01–A-19 evidence attached; no claim based solely on local `published` status or OTel traces | **Currently blocked; design closes the ambiguity, implementation evidence remains outstanding** |
