# Agent Hub P0/P1/P2/P3 cross-repository change manifest

Status: implementation and local verification; deployment remains operator-configured.
Local checks and unexecuted deployment checks: [VERIFICATION.md](VERIFICATION.md)
for P0–P2, [ECOSYSTEM_VERIFICATION.md](ECOSYSTEM_VERIFICATION.md) for P3,
and [GPU_VERIFICATION.md](GPU_VERIFICATION.md) for the GPU increment.
Workspace verification: [WORKSPACE_VERIFICATION.md](WORKSPACE_VERIFICATION.md).
Gateway machine authentication: [MACHINE_AUTH_VERIFICATION.md](MACHINE_AUTH_VERIFICATION.md).
CLI/TUI user authentication: [USER_AUTH_VERIFICATION.md](USER_AUTH_VERIFICATION.md).
On-demand sessions: [SESSION_CREATION.md](SESSION_CREATION.md) and
[SESSION_CREATION_VERIFICATION.md](SESSION_CREATION_VERIFICATION.md).
Idle-session close and capacity reclaim: [SESSION_CLOSE.md](SESSION_CLOSE.md) and
[SESSION_CLOSE_VERIFICATION.md](SESSION_CLOSE_VERIFICATION.md).
Read-only compute placement diagnostics: [PLACEMENT.md](PLACEMENT.md).
Cross-client request history: [SESSION_OPERATIONS.md](SESSION_OPERATIONS.md) and
[SESSION_OPERATIONS_VERIFICATION.md](SESSION_OPERATIONS_VERIFICATION.md).
Lost-task recovery: [LOST_RETRY_VERIFICATION.md](LOST_RETRY_VERIFICATION.md).

| Repository | Contract/implementation changes |
|---|---|
| ai-batch-runner | New `/api/v1/agent` service, SQLite Hub store, Gateway outbox/receipts, CLI/TUI client, explicit JWT grants, durable compute tasks and active-turn tools |
| snaplink-console | Separate Agent UI/API models, Hub OAuth resource/scopes and endpoint configuration, same-origin deployment route |
| snaplink | Existing OAuth/JWKS contracts consumed; no login-session/device schema changes; client/resource configuration required |
| aero-vault | Existing versioned file API consumed for verified evidence archives and bounded input/output workspace bundles |
| aero-id | New opt-in, strictly authenticated member projection check; explicit Hub identity-to-account mappings |
| aero-im | Existing installation notification API consumed for bounded lifecycle cards; no receiver changes |
| snaplink-audit-governance | Existing registered events API consumed; ledger receipts verified without copying conversation content |

Public contract: `openapi.yaml` in this directory. Session identifiers are opaque globally
unique IDs; local session IDs and instance IDs stay separately typed. Scope names and
response envelopes are shared by all clients. No consumer reads another service's database.

Compatibility: local `pi-batch`, TUI and Fabric commands preserve their existing APIs.
New `fleet` entry points are opt-in. Loopback development credentials cannot start a
public listener. Rolling HA is unsupported; keep one Hub owner per SQLite database.

Session creation adds a bounded, idempotent request ledger and a Gateway worker.
User clients can create on an empty online instance using its fixed project and
runtime. Capacity includes initial sessions and durable pending requests. Successful
registration is atomic; acknowledgement loss reuses the existing runtime object,
and generation takeover marks old queued requests lost. Python and Flutter clients
retain uncertain request identities and avoid selecting a late result after the
user changes their current instance or session. Existing Forge and authentication
changes are preserved; no ecosystem service deployment is performed.

Before rollout: configure Snaplink audience/client/tenant bindings, exact Hub grants,
TLS and allowed origins, then exercise two Gateway instances with separate credentials.
Observe idempotent prompt retry, cursor replay and generation takeover. Confirm denied
principals cannot list, read, submit or cancel another instance's sessions.

Idle-session close adds a separate durable request ledger and Gateway worker.
Admission freezes the session atomically with active-turn/compute checks; capacity
is reclaimed only from a confirmed successful close receipt. Failed closes remain
frozen and retain capacity until Gateway restart; ACK retries cannot call a partially
failed runtime close again. CLI/TUI and the shared Flutter UI retain request identity,
confirm the selected target and keep closed-session history available. Existing
instance grants apply without new OAuth scopes or per-creator ownership claims.

## Regression repairs included with this milestone

The request-history increment adds a bounded read-only projection over the existing
creation and close ledgers. Clients can rediscover accepted requests after restart,
refresh their outcome and explicitly open the referenced session. The endpoint uses
the same instance read grants as request detail; it neither reveals idempotency keys
nor retries business requests. Query indexes and immutable keyset pagination keep
status changes from moving records; only the current page is retained by the UI.

The required runner checks exposed existing function-size/complexity violations in
campaign recovery and pipeline parsing. Small helper extractions preserve their
behavior. Three older tests now assert the established user-level runtime paths
instead of expecting `.pi-batch` artifacts in the project.

The native pipeline attachment now releases its state lock before invoking the UI
callback, so UI shutdown cannot deadlock with an in-flight callback. A deterministic
cross-thread close test protects this behavior. The real Codex smoke tests use the
local Codex account's model default and assert actual assistant output. Journal
persistence tests use a completed turn fixture; separate long-output tests retain
rendering-pressure coverage. Pipeline interaction tests use an explicit release
barrier and guaranteed cleanup; review-fix tests wait for completion before checking
persisted evidence, with the same bounded wait as adjacent review cases.

Console settings and native-login tests identify SSO and Hub address fields separately.
They cover rejecting an invalid Hub origin before either setting is saved and clearing
the authenticated session when only the Hub origin changes.

P2 adds per-turn compute authority, separate execution/archive workers and a
single execution slot per Runner. Campaign test fixtures now inject their own
runner entrypoint explicitly, so they validate worktree isolation and watchdogs
independently of project-local versus installed tool entrypoint selection.
The SSH cancellation test now unregisters its fake process in a finally block;
without cleanup, later Runner integration tests observed a phantom busy device.

P3 adds a durable lifecycle outbox, monotonic delivery pagination, independent
workers and explicit read/retry scopes. Exact sink identities and prepared payloads
survive retries. A six-day IM replay limit respects the receiver's seven-day
idempotency retention. See [ECOSYSTEM.md](ECOSYSTEM.md) and
[ECOSYSTEM_VERIFICATION.md](ECOSYSTEM_VERIFICATION.md) for deployment and verification.

Aero ID's new membership endpoint uses the existing strict verifier, requires
the signed `aero-id.membership:check` scope, and records both eligible and
ineligible decisions through its existing durable audit ledger before returning.
Audit failure denies the read. Four pre-existing strict-lint violations in
operation/sync audit and erase recovery were corrected with small helper
extractions and a line wrap; their state-transition behavior is covered by
regression tests. Existing Aero ID frontend and API-document edits are preserved.

The PostgreSQL integration run also exposed an existing compensation-claim SQL
query with missing placeholder arguments. Lease predicates now use the database
clock, consistent with adjacent claim paths. Regression assertions cover pending
and unknown claims, live-lease exclusion and reclaim after expiry. Full-suite
timeouts and the bounded supplementary runs are recorded in the verification log.

The GPU increment adds bounded NVIDIA physical-card discovery, per-card memory
placement, durable UUID assignments and Runner preflight/evidence validation.
CLI, TUI and Console share optional GPU resource fields; CPU request identity
remains unchanged. Inventory pagination queries only authorized target batches.
Console also invalidates stale task-detail caches so newly reported assignments
remain visible. No additional ecosystem service changes are needed for this increment.

GPU projection, Runner preflight and TUI presentation helpers are separated to
preserve existing file budgets. Console response parsing follows its existing `part` convention;
equivalent test commands resolve the earlier print-string false positives. The
final Console harness passes without changing gate rules or dependency locks.

The authentication increments add private Gateway client-credentials renewal and
CLI/TUI device login with shared short-lived user sessions. User credentials remain
separate from Gateway identities; local logout fences pending login completions and
Hub 401 never automatically replays a business request. Snaplink's device approval
normalizes the approving pairwise subject before target-client issuance, avoiding a
second pairwise mapping. User login deployment and approval-page limits are recorded
in [USER_AUTH.md](USER_AUTH.md); no live account or deployment was configured.

The workspace increment adds session-scoped immutable Vault snapshots, signed Fabric
input distribution, isolated Runner restore and bounded output capture. CLI/TUI and
Console share snapshot/upload/output DTOs; active-turn tools keep service credentials
on the Hub. Output archive retries retain execution outcomes. See [WORKSPACE.md](WORKSPACE.md).

Workspace review also exposed two capacity boundaries: batched poll responses could
exceed the RPC limit after leasing several inputs, and task-log compaction did not
enforce its total file limit. Workspace polling now claims at most one task and
compaction preserves the old file when capacity is exhausted. Signed-envelope
tests cover Unicode paths and escaped process output; staging failures check remote
cancellation before reporting failure. Final evidence is recorded in
[WORKSPACE_VERIFICATION.md](WORKSPACE_VERIFICATION.md).

The native-file increment adds system JSON pick/save dialogs to Console's Android,
iOS, macOS, Linux and Windows hosts, with a versioned capability handshake, bounded
reads, cancellation and lifecycle handling. Flutter preserves the text fallback
and validates downloaded bundles before saving. Android/macOS network permissions
allow native clients to reach their configured HTTPS endpoints. No Runner or Hub
runtime code changes are required; compiled platforms and remaining verification
limits are recorded in Console's `docs/AGENT_NATIVE_WORKSPACE_FILES.md`.

Runner-side validation for this documentation-only increment: `make check` passed
all eight registered gates, `python3 quality.py . --strict` passed, and
`git diff --check` passed. Runtime code is unchanged from the prior workspace
verification; its 3,078-test result is historical evidence, not a new test run.

The placement increment shares Hub eligibility with a bounded read-only diagnostic
endpoint, Python CLI/TUI, active-turn tools and the shared Flutter task detail.

The Fabric federation increment allows one tenant to bind up to sixteen independent
control endpoints. Device inventory and placement diagnostics are aggregated across
the tenant, while the selected device pins dispatch, polling, cancellation, workspace
transfer and evidence archival to its owning endpoint. Configuration rejects duplicate
endpoints and device bindings; legacy single-connection configuration remains valid.
Workspace operations fail closed when unpinned snapshots would cross distinct Vaults.
The local regression covers configuration conflicts and a task routed exclusively to
the second Fabric connection.
Targets are filtered by original task authority, current compute grants and project
bindings before inventory. Non-queued tasks return no device observations. Reads
never reserve resources, rewrite task state or replay submissions.

The queued-task reschedule increment adds an owner-scoped, atomic target mutation.
It clears any stale placement handle, records a queued lifecycle event, and lets
the CLI, TUI, active-turn capability and Flutter clients select an authorized
device or restore automatic placement. Dispatched and running tasks remain
immutable; no migration or checkpoint transfer is implied.

The pre-start loss increment closes the narrow failure window after Hub dispatch
and before Runner start. A Fabric task reported `lost` with an explicit
`started_at: null` is
returned to the Hub queue with its old handle cleared, a durable
`device_lost_before_start` reason, and a new per-attempt Fabric request id so
Fabric idempotency records cannot replay the old lost task. Start evidence keeps
the task terminal `lost`; explicit target constraints are preserved.

The lost-task retry increment adds a deliberate recovery path for the remaining
terminal state. A task can be retried only by its actor with `agent.tasks:write`,
only after `confirm_duplicate=true`, and only while evidence archiving is
disabled. The Hub clears the old handle and result, preserves an original
explicit target when the request omits one, emits a `retry_requested` lifecycle
event, and assigns a fresh Fabric request identifier. Python CLI/TUI, active-turn
tools and the shared Console expose the same action; no client retries a lost
task implicitly because its command may already have run.
