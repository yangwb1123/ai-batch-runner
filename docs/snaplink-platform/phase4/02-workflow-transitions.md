## Definition Versioning

This is the normative target contract for the first executable vertical slice. It does not claim that the current `sverpweb-stable` implementation has already satisfied it. The current deployment remains single-tenant, with workflow facts stored locally in `sverpweb-stable`.

Ownership is fixed:

| Concern | Owner | Workflow may do |
|---|---|---|
| Purchase aggregate and purchase history | `sverpweb-stable` purchase module | Call typed purchase ports |
| Workflow definitions, instances, work items, candidates, decisions, executions and compensations | `sverpweb-stable` workflow module | Read/write through its own adapter |
| Identity, tenant context, roles, permissions, grants and revocation | `snaplink` | Consume authorization decisions |
| Departments, memberships and positions | `aero-id` | Consume bounded organization references |
| File ACLs and asset ownership | `aero-vault` | Store only asset references |
| Audit ledger, WORM, legal hold and governance receipts | `snaplink-audit-governance` | Receive events asynchronously |
| Local audit outbox | `sverpweb-stable` audit adapter | Write durable outbox rows |

Workflow has no purchase-table SQL, no purchase database handle, no identity tables, no ACL tables and no audit-ledger access.

The first definition is:

```text
definition_id: purchase-approval
definition_version: 1
definition_digest: sha256:<canonical-definition>
owner_service: sverpweb-stable
```

Its fixed graph is:

```text
start
  → department_approval
  → finance_approval       (conditional)
  → approval_join
  → command_confirmation
  → execution
  → audit_completion
  → end
```

`finance_approval` is present only when the versioned policy returns `finance_required=true`. If the required policy input is missing or ambiguous, routing fails closed. The transitional `qty TEXT` field is never used for arithmetic or finance thresholds.

Versioned contracts:

| Contract | Versioning rule |
|---|---|
| `execution-context.v1` | Security-critical fields cannot be silently removed |
| `purchase-approval` | Definition is immutable after activation |
| `purchase.approval.route` | Policy ID and policy version are snapshotted per instance |
| Business command schemas | Command name, schema version and payload digest are stored |
| Events | Event names use `domain.resource.action@vN` |
| Persistence | Changes use `schema_migrations` with checksum validation |

The workflow instance stores the exact definition version, policy version, route input digest, approval epoch and context digest used at creation. A new approval rule creates a new policy or definition version; it never mutates a running instance.

The status check for `workflow_instances` must add the canonical states `executable` and `compensation_pending`. `compensating` must not be used as an ambiguous synonym. Existing proposed schemas require an additive migration before activation.

A definition may contain only declared node types and bounded conditions. Arbitrary scripts, free-form expressions, Agent-generated transitions and unrestricted JSON mutations are prohibited.

## State Machines

The state machines are deliberately separate.

```text
PurchaseRequest:
  draft → pending_approval → approved
                         └→ rejected

WorkflowInstance:
  waiting
    → approved
    → rejected
    → expired
    → cancelled

  approved
    → executable
    → cancelled

  executable
    → execution_pending
    → cancelled

  execution_pending
    → executing
    → failed
    → cancelled

  executing
    → executed
    → failed
    → compensation_pending
    → cancelled

  executed
    → completed
    → compensation_pending

  failed
    → cancelled
    → compensation_pending

  compensation_pending
    → compensated
    → failed

  compensated → completed
  cancelled   → completed
  rejected    → completed
  expired     → completed
```

State meanings:

| State | Meaning |
|---|---|
| `waiting` | At least one approval work item is unresolved |
| `approved` | All required approvals completed; no business command has been confirmed |
| `executable` | The exact business command, payload digest, versions and authority have been explicitly confirmed; no side effect has started |
| `execution_pending` | A confirmed command is durably queued for a worker |
| `executing` | A worker has leased the execution and the effect may be in progress |
| `executed` | The purchase domain returned a verified success receipt |
| `failed` | A terminal failure is recorded and effect certainty is `none`; an uncertain effect must use `compensation_pending` |
| `cancelled` | Cancellation completed with verified absence of a business side effect |
| `compensation_pending` | A side effect occurred or may have occurred and compensation or reconciliation is required |
| `compensated` | The purchase domain returned a verified compensation receipt; the original execution fact remains |
| `completed` | Required terminal audit receipt and workflow closure conditions are satisfied |
| `rejected` | An approval decision rejected the request |
| `expired` | An approval or execution deadline elapsed without an effect |

`approved` never implies `executable`, `executing`, `executed` or `completed`. `executed` never implies `compensated`.

Every accepted transition must:

1. Validate the server-derived tenant and execution context.
2. Load the current version.
3. Validate the centralized transition table.
4. Apply an expected-version update.
5. Insert exactly one immutable history row for the new version.
6. Insert the corresponding local audit outbox event in the same local transaction.
7. Complete the command idempotency record.
8. Commit without network calls.

A failed local transaction leaves no state, history, idempotency completion or outbox row. An external side effect is never rolled back by restoring SQLite.

**Workflow instance transition matrix**

| Transition | Preconditions; actor and capability | Persisted changes | Events | Idempotency | Failure result |
|---|---|---|---|---|---|
| `∅ → waiting` | Purchase is `draft`; route policy resolves; requester or authorized proxy has `purchase.request.submit` | Purchase `draft → pending_approval`; create instance, approval epoch, department work item, optional finance work item, candidates, histories and operation record | `purchase.request.submitted@v1`, `workflow.instance.created@v1`, `workflow.instance.waiting@v1`, `workflow.workitem.created@v1` | Scope `workflow.submit`; same digest replays purchase/workflow references; different digest returns conflict | Missing route input, no candidate, tenant, version, authorization or outbox failure leaves purchase as `draft` |
| `waiting → waiting` for non-final approval | Eligible human has `workflow.approval.decide`; work item is claimed and finance remains pending | Insert immutable approval decision; mark department work item approved; increment workflow and work-item versions; purchase remains `pending_approval` | `workflow.approval.recorded@v1`, `workflow.workitem.approved@v1`, `workflow.instance.waiting@v1` | Scope `workflow.approval.decide`; replay returns the original decision | Unauthorized, stale claim/version, SoD violation or duplicate decision; no new decision |
| `waiting → approved` | All required human approvals exist in the current approval epoch; purchase version and workflow version match | Insert final decision; call purchase `approve`; purchase becomes `approved`; update workflow and work item; record approval evidence | `workflow.approval.recorded@v1`, `purchase.request.approved@v1`, `workflow.instance.approved@v1` | Same approval key replays all versions; another key after final approval returns invalid state | Purchase, workflow, history, idempotency and outbox changes roll back together |
| `waiting → rejected` | A current department or finance approver rejects with a required reason code | Call purchase `reject`; purchase becomes `rejected`; cancel remaining work items; record decision and histories | `workflow.approval.recorded@v1`, `purchase.request.rejected@v1`, `workflow.instance.rejected@v1`, `workflow.workitem.cancelled@v1` | Same decision key replays rejection; later approval keys return invalid state | No execution is created; transaction rolls back on local failure |
| `waiting → waiting` for key-field reapproval | Request is still waiting; authorized requester uses `purchase.request.amend`; key-field patch passes schema and expected version | Purchase amendment increments version; increment approval epoch; preserve old decisions as historical but inactive; cancel old work items; create a new route snapshot and work items | `purchase.request.amended@v1`, `workflow.approval.invalidated@v1`, `workflow.instance.waiting@v1`, `workflow.workitem.cancelled@v1`, `workflow.workitem.created@v1` | Scope `purchase.amend`; same digest replays amendment; a new digest requires the current version | Invalid patch, stale version or outbox failure leaves the old approval epoch active |
| `waiting → expired` | Due time passed; no final approval; automation has `workflow.timeout.expire` | Expire unresolved work items; update instance/history; revoke task grants; purchase remains non-executable | `workflow.workitem.expired@v1`, `workflow.instance.expired@v1` | Deterministic key from instance, node and deadline; repeated expiry is a replay | Lease/version conflict leaves the item unchanged for reconciliation |
| `waiting → cancelled` | Authorized requester or operator has `workflow.execution.cancel`; no execution effect exists | Call purchase `cancel` if a purchase cancellation fact is required; cancel work items and grants; update instance/history | `purchase.request.cancelled@v1`, `workflow.workitem.cancelled@v1`, `workflow.instance.cancelled@v1` | Scope `workflow.cancel`; same key replays cancellation | Cancellation is rejected if an execution lease or effect is detected |
| `approved → executable` | All approvals current; purchase remains approved; exact typed command, payload digest, approval epoch and expected version match; independent confirmer has `workflow.execution.confirm` | Create workflow-owned execution intent with confirmation provenance; status becomes `executable`; no purchase side effect | `workflow.execution.confirmed@v1`, `workflow.instance.executable@v1` | Scope `workflow.confirm`; same digest replays execution intent; changed command digest conflicts | No execution intent or state change on stale approval, key change, authorization, SoD or outbox failure |
| `approved → cancelled` | No execution intent has started; authorized cancellation passes current-version check | Cancel workflow and any pending intent; append purchase cancellation fact if required | `purchase.request.cancelled@v1`, `workflow.instance.cancelled@v1` | Scope `workflow.cancel`; replay returns cancelled state | If an execution has started, return `workflow.cancellation-too-late` |
| `executable → execution_pending` | Confirmed execution intent is active; dispatcher has capacity and valid grant | Create or activate execution record; set workflow to queued; create attempt budget | `workflow.execution.created@v1`, `workflow.instance.execution-pending@v1` | Scope `workflow.dispatch`; execution ID and command key are stable | Transient capacity failure leaves `executable`; permanent adapter absence returns `workflow.command-not-enabled` |
| `executable → cancelled` | Intent exists but no worker has started and no effect is possible | Cancel execution intent, revoke grant, update workflow/history | `workflow.execution.cancelled@v1`, `workflow.instance.cancelled@v1` | Scope `workflow.cancel`; same key replays | Lease race returns version conflict; state is not overwritten |
| `execution_pending → executing` | Worker is authorized; execution record is pending; lease and fencing token are acquired | Set execution attempt running; set workflow executing; persist worker and lease data | `workflow.execution.started@v1`, `workflow.instance.executing@v1` | Stable execution ID; a duplicate start is a replay or fencing conflict | Expired/revoked grant or lease conflict prevents start |
| `execution_pending → failed` | Dispatch failed permanently before any domain call | Mark execution failed with `effect_certainty=none`; update workflow/history | `workflow.execution.failed@v1`, `workflow.instance.failed@v1` | Same execution key returns the recorded failure; no new execution is created | Transient failures retain `execution_pending` and retry under policy |
| `execution_pending → cancelled` | Queue entry is not leased and cancellation is verified | Cancel execution record and workflow; revoke grant | `workflow.execution.cancelled@v1`, `workflow.instance.cancelled@v1` | Scope `workflow.cancel`; replay is safe | Lease acquisition race returns conflict |
| `executing → executed` | Purchase port returns verified `succeeded` with stable effect receipt and aggregate version | Record execution success, result digest, aggregate versions, workflow `executed`, history and outbox | `purchase.request.executed@v1`, `workflow.execution.succeeded@v1`, `workflow.instance.executed@v1` | Same execution ID and command key return the original receipt; never execute again | Finalization failure leaves execution open for reconciliation; no blind re-execution |
| `executing → failed` | Purchase port proves `failed_before_effect` | Mark attempt and execution failed; set effect certainty to `none`; update workflow/history | `purchase.request.execution-failed@v1`, `workflow.execution.failed@v1`, `workflow.instance.failed@v1` | Same execution key replays failure; a new key cannot bypass the failed instance | If effect certainty is unknown, this transition is forbidden; use `compensation_pending` |
| `executing → compensation_pending` | Timeout, connection loss, partial response or domain result says effect occurred/unknown | Record uncertainty, create compensation/reconciliation record, revoke execution grant | `workflow.execution.effect-unknown@v1`, `workflow.compensation.requested@v1`, `workflow.instance.compensation-pending@v1` | Same execution ID is reconciled; no new business execution key is accepted | Reconciliation failure leaves `compensation_pending` |
| `executing → cancelled` | Domain adapter verifies `not_started` or `aborted_before_effect` | Record cancellation, close attempt, revoke grant, update workflow/history | `workflow.execution.cancelled@v1`, `workflow.instance.cancelled@v1` | Same cancel key replays; no new execution is issued | An uncertain response must become `compensation_pending`, never `cancelled` |
| `executed → completed` | Required audit receipt, normally verified `ledgered`, is present; no compensation is pending | Record terminal audit receipt and workflow closure | `audit.receipt.ledgered@v1`, `workflow.instance.completed@v1` | Receipt uniqueness by outbox/event/stage makes closure replay-safe | Missing or unverified receipt leaves state `executed` and returns `workflow.audit-pending` |
| `executed → compensation_pending` | Authorized compensation request exists and effect is verified | Create compensation record; preserve executed fact; update workflow/history | `workflow.compensation.requested@v1`, `workflow.instance.compensation-pending@v1` | Scope `workflow.compensation`; same request returns compensation ID | Unsupported compensation returns `workflow.compensation-not-supported`; no state is falsely marked compensated |
| `failed → compensation_pending` | Failure has `effect_certainty=unknown` or later reconciliation proves an effect | Create or activate compensation record; update workflow/history | `workflow.compensation.requested@v1`, `workflow.instance.compensation-pending@v1` | Stable execution ID and new compensation operation key | Missing compensation contract leaves the instance pending recovery |
| `failed → cancelled` | Reconciliation proves no effect and an authorized cancellation closes the request | Record no-effect reconciliation and cancellation; revoke grants | `workflow.execution.reconciled@v1`, `workflow.instance.cancelled@v1` | Same reconciliation key replays | Without proof of no effect, return `workflow.effect-certainty-unknown` |
| `compensation_pending → compensated` | Purchase port returns verified compensation success | Record compensation result, aggregate versions and histories; update workflow | `purchase.request.compensated@v1`, `workflow.compensation.succeeded@v1`, `workflow.instance.compensated@v1` | Stable compensation key replays original receipt | Any unverified result remains pending; no optimistic success |
| `compensation_pending → failed` | Compensation is permanently rejected or an operator records exhausted recovery with evidence | Record compensation failure, error digest and recovery requirement | `workflow.compensation.failed@v1`, `workflow.instance.failed@v1` | Same compensation key returns failure; retries use the same compensation ID | The original effect remains recorded; the instance cannot become completed |
| `compensated → completed` | Required compensation audit receipt is verified | Record terminal closure | `audit.receipt.ledgered@v1`, `workflow.instance.completed@v1` | Receipt stage uniqueness makes replay safe | Missing receipt leaves `compensated` |
| `cancelled → completed` | Cancellation audit receipt satisfies the configured completion policy | Record terminal closure | `audit.receipt.ledgered@v1`, `workflow.instance.completed@v1` | Receipt replay is harmless | No claim of completed audit is made without a real receipt |
| `rejected → completed` | Rejection event has reached the required audit receipt stage | Record terminal closure | `audit.receipt.ledgered@v1`, `workflow.instance.completed@v1` | Receipt replay is safe | Instance remains `rejected` |
| `expired → completed` | Expiration event has reached the required audit receipt stage | Record terminal closure | `audit.receipt.ledgered@v1`, `workflow.instance.completed@v1` | Receipt replay is safe | Instance remains `expired` |

Forbidden transitions include:

```text
approved → executing
approved → executed
approved → completed
rejected → approved
rejected → executable
cancelled → executable
cancelled → executing
executed → cancelled
compensated → approved
compensated → executed
```

## Command Contracts

Every command uses the following envelope. `tenant_id` in a request body, query string or ordinary header is never authoritative.

```yaml
command_context:
  request_id: string
  operation_id: string
  correlation_id: string
  causation_id: string|null
  trace_id: string
  tenant_id: string                 # server-derived
  principal:
    type: human|service|automation|agent|system
    id: string
  initiator:
    type: human|service|automation|agent|system
    id: string
  on_behalf_of: string|null
  delegation_id: string|null
  capability_grant_id: string|null
  workflow_instance_id: string|null
  execution_run_id: string|null
  idempotency_key: string           # required for all writes
  deadline: timestamp|null
  source_system: string
```

A command also carries the relevant expected versions:

```yaml
expected:
  purchase_version: integer|null
  workflow_version: integer|null
  work_item_version: integer|null
  claim_version: integer|null
  approval_epoch: integer|null
  context_digest: string|null
```

The application separates:

```text
API DTO
  → application command
  → domain command
  → persistence row
  → explicit read projection
```

No ORM row is returned directly as an API response.

The purchase boundary is a typed port:

```text
PurchaseAggregateQueryPort
  read(ctx, purchaseId)

PurchaseAggregateCommandPort
  create(ctx, command)
  submit(ctx, purchaseId, expectedPurchaseVersion, idempotencyKey)
  approve(ctx, purchaseId, expectedPurchaseVersion, approvalEvidence, idempotencyKey)
  reject(ctx, purchaseId, expectedPurchaseVersion, reason, idempotencyKey)
  amend(ctx, purchaseId, expectedPurchaseVersion, keyFieldPatch, idempotencyKey)
  cancel(ctx, purchaseId, expectedPurchaseVersion, reason, idempotencyKey)
  execute(ctx, purchaseId, expectedPurchaseVersion, confirmedCommand, idempotencyKey)
  fail(ctx, purchaseId, expectedPurchaseVersion, errorCode, idempotencyKey)
  compensate(ctx, purchaseId, expectedPurchaseVersion, compensationCommand, idempotencyKey)
  recover(ctx, purchaseId, expectedPurchaseVersion, recoveryCommand, idempotencyKey)
```

The workflow module may invoke these ports but may not implement them or issue purchase SQL.

The enabled command set is:

| Command | Actor and capability | Required effect |
|---|---|---|
| `purchase.request.create` | Human or authorized proxy with `purchase.request.create` | Creates a purchase draft |
| `purchase.request.submit` | Requester/proxy with `purchase.request.submit` | Starts the workflow and routes approval |
| `workflow.workitem.claim` | Eligible human with `workflow.workitem.claim` | Acquires a bounded work-item lease |
| `workflow.approval.decide` | Eligible human with `workflow.approval.decide` | Records approve/reject; final approval calls the purchase port |
| `purchase.request.amend` | Authorized requester with `purchase.request.amend` | Changes key fields only while waiting and starts a new approval epoch |
| `workflow.execution.confirm` | Independent authorized confirmer with `workflow.execution.confirm` | Confirms the exact typed business command; creates no side effect |
| `workflow.execution.dispatch` | Internal scheduler with `workflow.execution.dispatch` | Queues a confirmed execution |
| `purchase.command.execute` | Bounded service/automation/agent grant with `purchase.command.execute` | Executes the allowlisted business command |
| `workflow.execution.cancel` | Requester or authorized operator with `workflow.execution.cancel` | Cancels only before a verified effect |
| `workflow.compensation.request` | Authorized operator with `workflow.compensation.request` | Starts a separately authorized compensation |
| `purchase.command.compensate` | Bounded service with `purchase.command.compensate` | Executes the typed compensation command |
| `workflow.recovery.reconcile` | Recovery operator/service with `workflow.recovery.reconcile` | Reconciles an uncertain execution without inventing success |

The first business command is a bounded, typed command:

```yaml
business_command:
  name: purchase.request.confirm
  schema_version: v1
  purchase_request_id: string
  expected_purchase_version: integer
  approval_epoch: integer
  payload_ref: string|null
  payload_digest: string
  effect_kind: confirm_purchase_request
```

It records that an approved request is confirmed for downstream procurement processing. It does not authorize payment, shipment, inventory reservation or an arbitrary external command. Those require a new command schema, policy and compensation contract.

`workflow.execution.confirm` confirms this exact command. It is not an approval and does not execute it. The execution worker later passes the same command descriptor to `PurchaseAggregateCommandPort.execute`.

Each command response contains:

```yaml
command_result:
  operation_id: string
  resource_type: string
  resource_id: string
  purchase_version: integer|null
  workflow_version: integer|null
  execution_id: string|null
  state: string
  audit_delivery_state: pending|accepted|ledgered|indexed|archived|failed|dead_letter
```

No sensitive response body is stored in `operation_idempotency`; only resource references, versions and bounded error codes are retained.

## Purchase Approval Transition Table

The purchase aggregate owns `purchase_requests` and `purchase_request_status_history`. Its first-slice status remains:

```text
draft
pending_approval
approved
rejected
```

Workflow states such as `executable`, `executed`, `cancelled` and `compensated` are not copied into the purchase `status` column. A purchase command receipt or cancellation/compensation fact is owned by the purchase module.

| Purchase transition or command | Preconditions; actor/capability | Persisted changes | Events | Idempotency | Failure result |
|---|---|---|---|---|---|
| `∅ → draft` | Valid server tenant; requester has `purchase.request.create`; required item and quantity fields pass bounds | Insert purchase row with UUID text ID, `tenant_id`, `version=1`, server timestamps and `status=draft`; insert creation history | `purchase.request.created@v1` | Scope `purchase.create`; same digest returns original ID/version | Invalid tenant, field, size, schema or outbox failure writes nothing |
| `draft → pending_approval` | Current purchase version matches; requester/proxy has `purchase.request.submit`; route policy is fully resolved | Update status, submitted time, updated actor and version; insert status history; workflow transaction creates instance and work items | `purchase.request.submitted@v1` and workflow creation events | Scope `workflow.submit`; replay returns original workflow ID and versions | Route ambiguity, no candidate, tenant mismatch, version conflict or local transaction failure leaves `draft` |
| `pending_approval → pending_approval` for amendment | Workflow is `waiting`; authorized requester has `purchase.request.amend`; patch contains only allowed key fields | Update safe business fields and version; insert `from_status=pending_approval,to_status=pending_approval` history with `event_type=amended`; workflow increments approval epoch | `purchase.request.amended@v1`, `workflow.approval.invalidated@v1` | Scope `purchase.amend`; same patch digest replays | Amendments after final approval are rejected; stale version causes `purchase.aggregate-version-conflict` |
| `pending_approval → approved` | All current approval nodes are approved; purchase version matches; workflow supplies bounded approval evidence | Update status, resolved time, actor and version; insert approval history; no workflow SQL is issued by the purchase module | `purchase.request.approved@v1` | Scope `purchase.approve` under the workflow operation; replay returns the original version | Status, tenant, evidence or version failure rolls back the complete local unit of work |
| `pending_approval → rejected` | Current approval actor has authority and supplies a reason code; version matches | Update status, reason, resolved time, actor and version; insert rejection history | `purchase.request.rejected@v1` | Scope `purchase.reject`; same key replays rejection | Invalid actor, status, version or outbox failure leaves the request pending |
| `approved → approved` for the confirmed business command | Exact command digest and expected version match; execution adapter is enabled | Insert a purchase-owned command receipt/effect record; increment aggregate version if the domain records a new fact; retain status `approved` | `purchase.request.executed@v1` on verified success | Stable execution key returns the original domain receipt | `failed_before_effect` records no success; unknown effect is reconciled and cannot be treated as cancellation |
| `pending_approval/approved → same status` for cancellation | No execution effect; purchase port receives a typed cancel command | Insert a purchase-owned cancellation fact or command receipt; do not invent `status=cancelled` in the legacy status enum | `purchase.request.cancelled@v1` | Scope `purchase.cancel`; replay returns the original cancellation fact | If an effect may have occurred, purchase cancellation is rejected and workflow must compensate |
| `approved → approved` for compensation | Domain reports an executed effect; compensation command and expected version are valid | Insert a purchase-owned compensation receipt and append a new history fact; never delete the original execution fact | `purchase.request.compensated@v1` | Stable compensation key returns the original receipt | Unsupported or failed compensation leaves the workflow `compensation_pending` |
| Any status for `recover` | Recovery operator has `workflow.recovery.reconcile`; effect identity is supplied | Append reconciliation fact and update command receipt if the domain owns it; no status is guessed | `purchase.request.reconciled@v1` | Stable recovery key is replay-safe | Missing evidence leaves effect certainty unknown |

The purchase port must distinguish:

```text
not_found or tenant mismatch
version conflict
invalid purchase state
command not enabled
authorization failure
effect unknown
```

A successful workflow approval does not authorize the purchase port to skip its own tenant, status, version and command validation.

## Work Item and Approval Rules

The first-slice route is sequential:

```text
department approval
  → finance approval if policy says required
  → approval join
```

Department approval is always required. Finance applicability is calculated from a server-side, versioned policy input. The client cannot set `finance_required`, `amount_minor`, `currency_code`, `department_id` or an approver.

If the current system cannot provide authoritative amount/risk semantics, the policy result is `unknown` and submission fails closed with `workflow.route-input-missing`. It must not infer a threshold from transitional `qty TEXT`.

Candidates are snapshots, not authority:

```text
candidate_type: user|position|department|role|group|service|agent
candidate_id: stable ID
resolution_source: policy|organization-reference|legacy-migration
policy_id: string|null
policy_version: string|null
```

Names, display labels and stale position records never grant approval authority. Eligibility is rechecked at claim and decision time using current Snaplink and `aero-id` authority.

**Work-item transition matrix**

| Work-item transition | Preconditions; actor/capability | Persisted changes | Events | Idempotency | Failure result |
|---|---|---|---|---|---|
| `∅ → pending` | Workflow instance is created and node is active | Insert work item, candidate snapshot, version `1`, retry count `0`, deadline and history | `workflow.workitem.created@v1` | Deterministic key from workflow, node and approval epoch | Duplicate node, missing candidate or FK failure rolls back workflow creation |
| `pending → claimed` | Current actor is an eligible candidate; grant and membership are active; claim version matches | Set assignee, claim lease, increment claim/version fields, persist actor and expiry | `workflow.workitem.claimed@v1` | Claim key is scoped to work item and lease; duplicate claim returns current claim | Unauthorized, expired grant or stale claim returns `workflow.workitem-not-assignable` |
| `claimed → pending` | Claim lease expired and no decision was committed | Clear assignee, increment claim version, increment retry count, append history | `workflow.workitem.released@v1` | System key uses work item and expired lease; one reclaimer wins | Active lease or version conflict leaves the claim unchanged |
| `claimed → approved` | Human approver is still eligible; applicant separation and policy checks pass | Insert immutable decision, update work item and workflow versions | `workflow.approval.recorded@v1`, `workflow.workitem.approved@v1` | Same operation/digest replays; a different operation after decision returns conflict | No decision is inserted on authorization, SoD, version or outbox failure |
| `claimed → rejected` | Human approver is eligible; reason code is required | Insert immutable rejection decision; update work item and workflow versions | `workflow.approval.recorded@v1`, `workflow.workitem.rejected@v1` | Same operation replays rejection | Invalid reason, authority, state or version causes no write |
| `pending/claimed → expired` | Deadline passed; no final decision exists | Mark work item expired, update workflow if no alternate route remains, revoke grant | `workflow.workitem.expired@v1`, `workflow.instance.expired@v1` when applicable | Deterministic expiry key; repeated expiry is safe | Reclaimer conflict leaves item available for reconciliation |
| `pending/claimed/approved/rejected → cancelled` | Workflow cancellation or approval-epoch change is committed | Mark active item cancelled; preserve decision rows; old epoch is inactive | `workflow.workitem.cancelled@v1` | Same cancellation or epoch operation replays | A committed decision is never deleted or edited |
| `claimed → deferred` | Not enabled in the first slice | No accepted transition | No event | Command returns `workflow.command-not-enabled` | No write |

Approval decisions are immutable. A key-field change increments `approval_epoch`; older decisions remain historical evidence but are not active for the new epoch. A new approval must use a new operation and new current work item state.

Segregation of duties is strict for this slice:

- The requester and `initiator` cannot approve.
- The department approver and finance approver must be different effective subjects.
- The requester cannot be the explicit command confirmer.
- An approver cannot be the explicit command confirmer.
- A human executor, if one is ever enabled, must be distinct from requester, approvers and confirmer.
- A service or Agent executor does not satisfy human approval separation merely by using `on_behalf_of`.
- Delegation records both the actual actor and represented subject; both are checked for conflicts.
- An Agent may recommend but cannot produce a final approval in this slice.
- If no distinct authorized approver exists, the request fails closed rather than collapsing SoD.

Key fields requiring reapproval are:

```text
item_code
qty
requester
department_id
approver or position selection
reason
asset references or attachments
amount/currency when introduced
policy inputs used for routing
confirmed business-command payload
```

While the instance is `waiting`, an approved amendment starts a new approval epoch and reroutes the request. After the instance reaches `approved`, `executable`, `execution_pending` or `executing`, key-field changes are rejected with `workflow.key-change-reapproval-required`. The safe path is cancellation before effect and creation of a new request, or compensation after effect.

## Execution and Side-effect Boundary

`workflow.execution.confirm` and business execution are separate operations.

Confirmation:

```text
approved
  → workflow.execution.confirm
  → executable
```

The confirmer sees and confirms:

```text
purchase ID
purchase version
approval epoch
command name
command schema version
payload digest
policy version
required approvals
```

Confirmation writes only workflow-owned execution intent and provenance. It does not call an external system and does not change the purchase aggregate.

Execution:

```text
executable
  → execution_pending
  → executing
  → PurchaseAggregateCommandPort.execute(...)
```

The execution record is workflow-owned and contains at least:

```text
execution_id
tenant_id
workflow_instance_id
command_name
command_schema_version
command_payload_ref
command_payload_digest
confirmation_id
initiator provenance
executor provenance
on_behalf_of
capability_grant_id
approval_epoch
idempotency_key
aggregate_version_before
aggregate_version_after
effect_certainty
status
result_digest
timestamps
```

Execution attempts are separate records with:

```text
attempt_id
execution_id
attempt_no
worker_id
lease_until
status
started_at
completed_at
error_code
result_digest
```

The purchase module may maintain a concrete purchase-owned command receipt. Workflow never reads that table directly; it receives the typed result:

```yaml
execution_result:
  state: succeeded|failed_before_effect|unknown
  aggregate_version_after: integer|null
  effect_ref: string|null
  effect_digest: string|null
  error_code: string|null
```

The side-effect boundary is:

1. Commit `executable` and the exact execution intent.
2. Commit `executing` and a worker lease.
3. Call the typed purchase command outside the SQLite transaction.
4. The purchase module validates tenant, authorization, aggregate version, command schema and idempotency.
5. The purchase module commits its own business fact and local outbox if the command succeeds.
6. Workflow records the verified result and emits its own event/outbox row.
7. Audit relay delivers the durable events after commit.

No network call to identity, organization, audit governance, notification or another service occurs inside the SQLite write transaction.

If the purchase command succeeds but workflow finalization fails, the system does not execute again. A reconciler uses the stable `execution_id` and command idempotency key to retrieve the domain result and finalize workflow state.

If a domain effect may have occurred but no verified result exists, the state is `compensation_pending`, not `failed` with an implied absence of effect and not `cancelled`.

Every critical local transition inserts an `audit_outbox` row atomically with business-owned changes. The relay uses the same `event_id` and `event_key` for every retry.

The target audit envelope contains:

```text
event_id
event_key
source_system
event_type
schema_version
occurred_at
tenant_id
operation_id
correlation_id
causation_id
trace_id
actor_type
actor_id
initiator_type
initiator_id
on_behalf_of
delegation_id
capability_grant_id
workflow_instance_id
execution_run_id
aggregate_type
aggregate_id
aggregate_version
action
outcome
reason_code
payload_digest
data_classification
retention_class
```

Payloads exclude tokens, passwords, session data, authorization headers, raw request bodies, raw grants and raw authority material. Rejection text remains business data and is not copied into the audit payload by default.

Audit delivery is independent from workflow business state:

```text
pending → sending → accepted → ledgered → indexed → archived
                  └→ failed → dead_letter
```

`accepted`, `ledgered`, `indexed` and `archived` are recorded only from actual governance responses. A local outbox row is not evidence of a governance-ledger receipt. Until the active audit-governance ingest path, schema and receipt contract are compared and tested, production completion must remain fail closed.

## Cancellation Timeout and Retry

Cancellation is a distinct command, not a status overwrite.

| Current state | Cancellation rule | Actor/capability | Persisted changes and events | Failure result |
|---|---|---|---|---|
| `waiting` | Allowed; no side effect exists | Requester or authorized operator with `workflow.execution.cancel` | Cancel active work items and grants; append cancellation fact if required; emit `purchase.request.cancelled@v1` and `workflow.instance.cancelled@v1` | Version conflict, missing authority or outbox failure leaves `waiting` |
| `approved` | Allowed before command confirmation | Authorized requester/operator | Cancel workflow; emit `workflow.instance.cancelled@v1` | Does not become `rejected`; cancellation is a separate fact |
| `executable` | Allowed before worker start | Authorized requester/operator | Cancel execution intent and grant; emit `workflow.execution.cancelled@v1` and `workflow.instance.cancelled@v1` | A start race returns `workflow.cancellation-too-late` |
| `execution_pending` | Allowed only before lease acquisition | Authorized requester/operator | Cancel queue record and workflow | Acquired lease requires worker reconciliation |
| `executing` | Allowed only when purchase adapter verifies no effect or abort-before-effect | Authorized operator plus execution adapter | Close attempt and cancel workflow; emit `workflow.execution.cancelled@v1` | Unknown or successful effect routes to `compensation_pending` |
| `executed` | Ordinary cancellation is forbidden | Compensation authority required | No cancellation write; create compensation request instead | `workflow.compensation-required` |
| `compensation_pending` | Cancellation is forbidden | Recovery/compensation process owns the state | Continue reconciliation or compensation | `workflow.invalid-transition` |

Deadlines are server-controlled and stored as UTC Unix milliseconds. Each approval work item has a `decision_deadline`; each execution has a start deadline, lease deadline and maximum execution deadline. The client cannot extend a deadline by changing request data.

Retry policy:

| Failure | Retry behavior |
|---|---|
| Tenant, authentication, authorization, SoD or grant failure | No automatic retry |
| Schema, policy, invalid transition or command-not-enabled failure | No automatic retry |
| Optimistic version conflict | Return conflict; caller rereads and submits a deliberate new command |
| SQLite busy/temporary local failure | Bounded retry with the same idempotency key |
| Claim lease expiry before decision | Reclaim once under a new attempt count |
| Worker failure before domain call | Retry the same execution ID and command key |
| Domain response `failed_before_effect` | Record `failed`; do not blindly retry with a new business key |
| Domain response unknown | Reconcile first; do not execute with a new key |
| Audit network outage | Retry outbox delivery with stable event ID/key and jitter |
| Audit schema/authentication failure | Dead-letter; do not claim `accepted` or `ledgered` |
| Notification failure | Separate best-effort integration; never change purchase outcome |
| Compensation transient failure | Retry the same compensation ID and key |
| Compensation unknown | Reconcile; remain `compensation_pending` |

A retry never creates a second purchase request, second approval decision, second execution ID or second business side effect.

## Compensation and Recovery

Cancellation means “verified no side effect.” Compensation means “a side effect happened or may have happened and a new business command is required.”

The first-slice compensation command is:

```yaml
compensation_command:
  name: purchase.request.revoke-confirmation
  schema_version: v1
  original_execution_id: string
  purchase_request_id: string
  expected_purchase_version: integer
  effect_reference: string|null
  reason_code: string
  payload_digest: string
```

The purchase domain must explicitly declare whether this command is reversible. If the actual business adapter later performs payment, shipment, reservation or another irreversible action, it must provide a different typed compensation contract and approval policy.

Compensation transitions:

| Transition | Preconditions; actor/capability | Persisted changes | Events | Idempotency | Failure result |
|---|---|---|---|---|---|
| `executed → compensation_pending` | Verified effect; independent compensation authority with `workflow.compensation.request` | Create compensation record referencing execution, approval and effect receipt; revoke execution grant | `workflow.compensation.requested@v1`, `workflow.instance.compensation-pending@v1` | New compensation key is unique to the original execution and command digest | Unsupported command returns `workflow.compensation-not-supported` and remains pending recovery |
| `failed → compensation_pending` | Effect certainty is unknown or reconciliation proves an effect | Preserve failed attempt; create compensation record; do not delete failure evidence | `workflow.compensation.requested@v1`, `workflow.instance.compensation-pending@v1` | Same execution ID is retained; no new execution key | Missing effect evidence keeps the state pending |
| `compensation_pending → compensated` | Purchase port returns verified compensation receipt | Record compensation result, aggregate version and result digest | `purchase.request.compensated@v1`, `workflow.compensation.succeeded@v1`, `workflow.instance.compensated@v1` | Same compensation key returns the original receipt | Unverified response does not advance state |
| `compensation_pending → failed` | Compensation permanently rejected or operator records exhausted recovery with evidence | Record error code/digest and recovery-required marker | `workflow.compensation.failed@v1`, `workflow.instance.failed@v1` | Repeated attempts use the same compensation record | Original execution remains immutable and uncompensated |

Recovery for an uncertain execution is:

```text
query purchase adapter by execution_id/idempotency_key
  → verified not_started
       → failed or cancelled
  → verified succeeded
       → executed, then audit completion
  → verified effect with compensation available
       → compensation_pending
  → unavailable or contradictory
       → compensation_pending + operator recovery
```

A database restore does not undo an external effect. Recovery must:

1. Stop writers and restore SQLite to a new file.
2. Run `PRAGMA integrity_check` and migration checks.
3. Verify every row has the configured deployment tenant.
4. Detect orphaned purchase/workflow references.
5. Reconcile outbox rows by stable `event_id` and `event_key`.
6. Query the purchase adapter by stable execution keys.
7. Rebuild audit evidence only through audit-governance replay.
8. Use `recover`, `cancel` or `compensate` business commands for business correction.

Audit replay cannot mutate purchase or workflow state directly. A deployment rollback cannot reverse an approved request or an external effect.

## Authorization and Provenance

The server constructs `ExecutionContext` from Snaplink authentication, client binding, configured deployment tenant and current membership. The current deployment accepts exactly one configured tenant. This is a future isolation boundary, not a shared-instance multi-tenancy claim.

Rules:

- Startup fails if the configured tenant is absent or invalid.
- Every new row has `tenant_id NOT NULL`.
- Every read, update, history lookup, idempotency lookup and outbox operation includes `tenant_id`.
- A client-supplied tenant is ignored except for consistency validation.
- A missing tenant, mismatched tenant or second-tenant insertion fails closed.
- `department_id` comes from the server-side organization provider; the request body cannot choose it.
- Names are display data, never authorization keys.
- Workflow stores subject IDs, policy references and bounded authority snapshots, not tokens or grants.
- Cross-tenant access requires an explicit external grant and is not enabled for this single-tenant slice.

Required permissions are lowercase dotted names with at least three segments:

| Permission | Purpose |
|---|---|
| `purchase.request.create` | Create a draft |
| `purchase.request.submit` | Submit a draft |
| `purchase.request.amend` | Amend key fields during reapproval |
| `purchase.request.cancel` | Cancel a purchase command/fact |
| `workflow.workitem.claim` | Claim an approval task |
| `workflow.approval.decide` | Approve or reject a task |
| `workflow.execution.confirm` | Confirm the exact business command |
| `workflow.execution.dispatch` | Queue an approved execution |
| `workflow.execution.cancel` | Cancel before verified effect |
| `workflow.compensation.request` | Request compensation |
| `workflow.recovery.reconcile` | Reconcile uncertain execution |
| `purchase.command.execute` | Execute the allowlisted purchase command |
| `purchase.command.compensate` | Execute the typed compensation |
| `purchase.command.recover` | Execute a bounded recovery command |
| `audit.event.ingest` | Relay an event to the audit authority |
| `audit.receipt.read` | Read governance receipt state |

The final authorization decision is the intersection of:

```text
authenticated principal
∩ valid tenant context
∩ active membership
∩ role/policy permission
∩ resource/asset ACL where applicable
∩ capability grant
∩ workflow approval policy
∩ current aggregate version
∩ segregation-of-duties rules
```

`aero-vault` remains the ACL authority for attachments. Workflow may validate an asset reference through a typed port, but it cannot grant, revoke or infer file access.

Agents and automation:

- Agent grants are Snaplink-owned, short-lived, resource-scoped, action-scoped, quota-bounded and revocable.
- Agent capability is the intersection of initiator authority, Agent profile, grant, ACL and tenant policy.
- Agents cannot approve their own request, modify their grant, change policy or self-elevate.
- First-slice Agent approval mode is `suggestion_only`.
- Automation and relay actors never masquerade as human approvers.
- `actor`, `initiator`, `executor`, `on_behalf_of`, `delegation_id` and `capability_grant_id` are persisted separately.
- Approval authority is rechecked at decision time, not inferred from the original candidate snapshot.

## Idempotency and Concurrency

All critical writes require an idempotency key of at most 128 UTF-8 bytes. The database uniqueness boundary is:

```text
tenant_id + scope + idempotency_key
```

The request digest is SHA-256 over a canonical, redacted command containing:

```text
tenant
command
target
expected versions
normalized safe fields
approval epoch
actor/context digest
command payload digest
```

Processing algorithm:

```text
BEGIN IMMEDIATE

validate server context and command
insert operation_idempotency(status=pending)

if unique conflict:
    compare request_digest
    same digest + succeeded → replay stored resource/version result
    same digest + pending   → return operation_in_progress
    same digest + failed    → replay recorded failure
    different digest        → idempotency_conflict

load tenant-scoped resources
validate expected versions and transition
invoke typed purchase port when required
update owned state and version
insert exactly one history row per new version
insert local audit_outbox event(s)
mark operation_idempotency succeeded
COMMIT
```

A duplicate successful command creates no new purchase row, decision, history row, workflow transition or audit event.

Optimistic concurrency rules:

- Purchase updates use `tenant_id`, ID, expected status and expected version.
- Workflow updates use `tenant_id`, workflow ID, expected status and `current_version`.
- Work-item claims use expected `claim_version` and lease state.
- `changes == 0` is not success.
- A tenant-scoped reread distinguishes not-found/tenant mismatch from version conflict.
- Concurrent approve/reject operations produce one success and one structured conflict.
- The losing operation cannot overwrite the winner or create a second decision.
- Key-field amendment and approval use the same purchase version, preventing approval against stale fields.
- `approval_epoch` prevents old approvals from satisfying a new route.

A shared local unit of work may coordinate the purchase port and workflow persistence adapter. The workflow adapter still writes only workflow-owned tables. Cross-project authorization and organization calls occur before the write transaction or through a bounded prevalidated context; remote calls never occur inside the SQLite transaction.

Execution is intentionally two-phase:

1. Persist an execution intent and queue state.
2. Perform the external/domain side effect.
3. Finalize using the same execution ID and key.

If finalization fails after the domain reports success, reconciliation finalizes the existing execution. It never creates a new execution key.

Outbox relay concurrency:

- Claim uses a lease and compare-and-set update.
- `event_id` and `event_key` never change across attempts.
- `audit_outbox_attempts` records attempt number, result and bounded error digest.
- `audit_outbox_receipts` is unique by outbox and receipt stage.
- A duplicate remote delivery is safe only when audit governance honors the stable event ID.
- Local delivery state is never promoted optimistically.

## Error Codes and Client Semantics

Error responses use a stable code, operation ID, trace ID and bounded details:

```json
{
  "code": "workflow.version-conflict",
  "message": "The resource changed before this command was committed.",
  "operation_id": "op-...",
  "trace_id": "trace-...",
  "details": {
    "resource_type": "workflow_instance",
    "resource_id": "..."
  }
}
```

Error codes:

| Code | Client meaning | Retry |
|---|---|---|
| `workflow.tenant-context-missing` | Server could not establish tenant scope | No; fix server context |
| `workflow.tenant-mismatch` | Supplied context disagrees with server context | No |
| `workflow.resource-not-found` | Resource is absent or not visible in this tenant | No blind retry |
| `workflow.authorization-denied` | Permission or policy denied | No |
| `workflow.capability-expired` | Grant expired | Obtain a new authorized grant |
| `workflow.capability-revoked` | Grant was revoked | No retry with the old grant |
| `workflow.separation-of-duties` | Actor combination is forbidden | Use an independent actor |
| `workflow.invalid-transition` | Current state cannot accept this command | Reread and choose a valid command |
| `workflow.version-conflict` | Expected version is stale | Reread; do not reuse a new key blindly |
| `workflow.idempotency-conflict` | Same key was used for a different command | Generate no replacement business command automatically |
| `workflow.operation-in-progress` | Original operation is still pending | Poll using the same operation |
| `workflow.route-input-missing` | Finance applicability cannot be determined | Supply authoritative server-side data |
| `workflow.no-approval-candidate` | No eligible department/finance candidate exists | Correct organization/policy data |
| `workflow.approval-not-authorized` | Current actor cannot decide this work item | No |
| `workflow.approval-already-decided` | Work item already has a decision | Replay original result if the key matches |
| `workflow.key-change-reapproval-required` | Fields changed after approval or confirmation | Cancel/reapprove; never bypass |
| `workflow.command-digest-mismatch` | Confirmed command differs from execution command | No |
| `workflow.command-not-enabled` | Concrete domain adapter is not activated | No |
| `workflow.execution-not-approved` | Execution lacks current approvals or confirmation | No |
| `workflow.execution-timeout` | Execution exceeded its deadline | Reconcile first |
| `workflow.effect-certainty-unknown` | Side effect status cannot be proven | Reconcile or compensate |
| `workflow.cancellation-too-late` | Effect may have started | Use compensation |
| `workflow.compensation-required` | Cancellation is no longer valid | Start compensation |
| `workflow.compensation-not-supported` | Domain has not supplied a compensation contract | Manual recovery required |
| `workflow.compensation-failed` | Compensation did not complete | Remains recovery-required |
| `workflow.audit-pending` | Business result exists but required audit receipt is absent | Poll; do not claim ledger completion |
| `workflow.audit-contract-unavailable` | Active audit ingest/schema contract is not verified | Do not activate production execution |
| `workflow.audit-delivery-failed` | Relay failed delivery | Retry outbox or resolve DLQ |
| `workflow.persistence-failure` | Local atomic transaction failed | Safe to retry with same key |
| `workflow.recovery-required` | Automated recovery cannot establish a safe result | Operator action required |

Client state semantics:

| Returned state | Client must understand |
|---|---|
| `approved` | All required approvals are complete; execution has not been authorized |
| `executable` | Exact command confirmation is complete; no side effect has started |
| `execution_pending` | Command is queued; no success is implied |
| `executing` | Effect may be in progress; do not submit another execution |
| `executed` | Purchase domain confirmed success; audit governance may still be pending |
| `failed` | A terminal no-effect failure is recorded, or an operator explicitly closed an attempt; inspect effect certainty |
| `cancelled` | No side effect was verified; this is not rejection |
| `compensation_pending` | Effect occurred or may have occurred; cancellation is no longer valid |
| `compensated` | Compensation succeeded; the original execution remains in history |
| `completed` | Required audit and closure conditions are satisfied |

Recommended HTTP semantics:

- `201` for draft creation.
- `200` for a synchronously committed approval, rejection or confirmation.
- `202` for queued execution, cancellation requiring worker confirmation or compensation.
- `409` for version, idempotency, SoD-state and claim conflicts.
- `403` for authorization, tenant and capability denial without exposing cross-tenant existence.
- `422` for invalid command schema, route ambiguity or command not enabled.
- `503` only for temporary infrastructure unavailability where no business transition committed.

A `200` or `202` response never claims `ledgered`, `indexed` or `archived` unless the corresponding governance receipt was actually verified.

## Acceptance Test Matrix

| ID | Scenario | Required evidence |
|---|---|---|
| V-01 | Activate `purchase-approval@v1` | Immutable definition, digest and node schema are stored |
| V-02 | Attempt to edit an active definition | Rejected; a new version is required |
| V-03 | Missing configured tenant | Startup or command fails before business writes |
| V-04 | Body/query/header supplies another tenant | Server context wins; mismatch is rejected |
| V-05 | Attempt to insert a second tenant in current deployment | Rejected; no shared-instance tenancy is claimed |
| V-06 | Detail, list, history, workflow, idempotency and outbox queries | Every query has a server-derived tenant predicate |
| V-07 | Workflow module static inspection | No purchase-table SQL or purchase database handle |
| V-08 | Create with duplicate key and same digest | Original purchase ID/version is replayed without duplicate rows |
| V-09 | Create with duplicate key and different digest | `workflow.idempotency-conflict`; no write |
| V-10 | Submit a draft with valid department route | Purchase becomes `pending_approval`; workflow and work item are created atomically |
| V-11 | Finance policy returns `true` | Department work item precedes finance work item |
| V-12 | Finance policy returns `false` | Department approval is final; no finance task is created |
| V-13 | Finance policy input is absent or ambiguous | Submit fails closed; purchase remains `draft` |
| V-14 | Department approver claims task | Current membership, candidate and grant are rechecked; lease is persisted |
| V-15 | Applicant attempts approval | `workflow.separation-of-duties`; no decision |
| V-16 | Same human attempts department and finance approval | Second decision is rejected |
| V-17 | Department approval while finance is required | Work item is approved; workflow remains `waiting`; purchase remains `pending_approval` |
| V-18 | Final required approval | Purchase becomes `approved`; exactly one purchase history row, workflow history row and approval decision are committed |
| V-19 | Approval rejection | Purchase becomes `rejected`; remaining work items are cancelled; no execution intent exists |
| V-20 | Concurrent approve and reject | One success, one version/state conflict, one final purchase version increment and one corresponding history row |
| V-21 | Key-field amendment while waiting | Purchase version and approval epoch increment; old decisions remain immutable and inactive; fresh work items are routed |
| V-22 | Key-field change after approval | Rejected with `workflow.key-change-reapproval-required`; no stale execution |
| V-23 | Explicit command confirmation by requester or approver | Rejected by SoD |
| V-24 | Valid independent command confirmation | `approved → executable`; command digest, approval epoch and confirmer provenance are persisted; no side effect |
| V-25 | Change confirmed payload before execution | Digest mismatch; execution does not start |
| V-26 | Dispatch and worker lease | `executable → execution_pending → executing`; duplicate worker start is fenced |
| V-27 | Domain command succeeds | Purchase receipt and workflow execution result contain the same stable execution key and verified effect digest |
| V-28 | Domain command fails before effect | Workflow becomes `failed`; no compensation is falsely required |
| V-29 | Worker timeout or unknown response | Workflow becomes `compensation_pending`; no new execution key is accepted |
| V-30 | Cancellation before execution | Workflow becomes `cancelled`; no business effect and no execution success event |
| V-31 | Cancellation during uncertain execution | Ordinary cancellation is rejected; compensation/reconciliation is required |
| V-32 | Successful compensation | Workflow becomes `compensated`; original execution and audit facts remain |
| V-33 | Unsupported compensation | `workflow.compensation-not-supported`; state remains recovery-required |
| V-34 | Injected failure before local outbox insertion | Purchase/workflow state, history and idempotency completion roll back together |
| V-35 | Audit-governance outage after local commit | Business result remains; outbox remains retryable and no ledger receipt is claimed |
| V-36 | Repeated audit relay attempt | Same event ID/event key is sent; no duplicate governance event |
| V-37 | Fake `accepted`, `ledgered`, `indexed` or `archived` receipt | Test fails; local state cannot be promoted without an actual response |
| V-38 | Audit payload inspection | No token, password, authorization header, raw request body or raw grant appears |
| V-39 | Repeated command with same key after success | Original resource/version is returned without new business rows or events |
| V-40 | Same command with a new key after state change | Invalid-state or version-conflict response; no duplicate side effect |
| V-41 | SQLite backup and restore | Integrity check, migration checksum, tenant verification and orphan scan succeed |
| V-42 | Restore after execution | Outbox is reconciled by stable event ID; external effect is reconciled by execution ID |
| V-43 | Audit replay | Only audit evidence is rebuilt; purchase and workflow state are not directly mutated |
| V-44 | Query limits | Requester and approval lists remain bounded to 50 rows and use keyset pagination |
| V-45 | Migration backfill | Existing rows receive explicit configured tenant, version and `legacy_imported` history without fabricated approval decisions |
| V-46 | Legacy pending requests | Each is either marked as a legacy workflow requiring review or explicitly quarantined |
| V-47 | Disabled return/defer/arbitrary execution command | Typed `workflow.command-not-enabled` result with no table writes |
| V-48 | Full activation gate | Actual audit ingest path, active envelope schema, receipt semantics, backup/restore runbook, concrete purchase command and compensation contract are supplied before production execution is enabled |
