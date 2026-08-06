# Snaplink Platform — Per-Service Audit Ingestion Design (Draft)

> **Status**: DRAFT — proposal for human approval before promotion to
> contract (`MULTI_REPO_OPERATIONS.md`). Companion docs:
> `audit-event-registry-draft.md` (121 event types),
> `scope-matrix-draft.md` (scopes/grant rules).
> **Evidence standard**: connector existence is Verified where cited;
> all schema/behavior details are `[PROPOSED]` until validated against
> each codebase.

## 0. Verified anchors

| Anchor | Source |
|---|---|
| aero-id connector: `internal/connector/auditgovernance/` (publisher/oauth) — the Outbox+publisher reference pattern | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| aero-vault: `internal/auditgovernance/token.go` (Snaplink browser-flow token source) | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| snaplink: local audit-log concept only, no governance connector | `SCENARIO_ANALYSIS.md` §0 (Verified gap) |
| aero-im: no audit connector | `SCENARIO_ANALYSIS.md` §0 (Verified gap) |
| Outbox in the business transaction; audit never blocks business flow; L0/L1/L2 tiers; sync minimal audit ack only for extreme-risk ops (tenant delete / master-key reset) | `COMBINATION_MODEL.md` §3 (Proposed, cited) |
| 04-composition: audit + notify events generated in the same transaction as the file delete; outbox sits inside vault's transaction | `decisions/04-composition.md` (Verified decision) |
| Event envelope: id/type/version/source/tenant_id/time/trace_id/correlation_id/data (CloudEvents style); ingest idempotent by `event_id`; tenant consistency check | `COMBINATION_MODEL.md` §4; `scope-matrix-draft.md` Flow 3 (Proposed, cited) |

## 1. Common design (applies to all producer services)

### 1.1 Outbox table schema `[PROPOSED]`

```sql
CREATE TABLE audit_outbox (
  id               BIGSERIAL PRIMARY KEY,          -- local order (FIFO per service)
  event_id         VARCHAR(128) NOT NULL,          -- deterministic idempotency key
  event_type       VARCHAR(128) NOT NULL,          -- registry id (e.g. file.delete.admin)
  event_version    INT NOT NULL DEFAULT 1,         -- registry schema version
  tenant_id        VARCHAR(64)  NOT NULL,
  payload          JSONB        NOT NULL,          -- envelope + event-specific fields
  status           SMALLINT     NOT NULL DEFAULT 0,-- 0=pending 1=sending 2=sent 3=dead
  attempt_count    INT          NOT NULL DEFAULT 0,
  next_attempt_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  last_error       TEXT,
  sent_at          TIMESTAMPTZ,
  lease_until      TIMESTAMPTZ,                    -- worker lease (crash-safe)
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
  UNIQUE (service, event_id)                       -- service column fixed per DB
);
CREATE INDEX idx_outbox_due ON audit_outbox (status, next_attempt_at) WHERE status IN (0,1);
```

- Written **in the same transaction** as the business operation (per
  `COMBINATION_MODEL.md` §3). No business flow awaits the relay.
- `payload` is metadata-only (registry §1.7): no file/message content.
- Table name/local column conventions follow each service's DB style;
  the `UNIQUE (service, event_id)` and `status` semantics are the contract.

### 1.2 Relay/worker behavior `[PROPOSED]`

- Single-writer per service: one worker (or N workers partitioned by
  `hash(event_id) % N` with per-partition FIFO). Claim via
  `SELECT ... WHERE status IN (0,1) AND next_attempt_at <= now()
  ORDER BY id LIMIT batch FOR UPDATE SKIP LOCKED` (or lease_until stamping).
- Batch size 100, poll interval 1s idle / immediate on new rows.
- Auth: `client_credentials` via the connector's oauth token source
  (aero-id `publisher/oauth`, aero-vault `token.go` pattern); token
  refresh before expiry; single token shared across batches.
- Delivery: `POST <audit-governance>/v1/events` (or gRPC) with the
  `audit:event:ingest` scope (scope-matrix Flow 3).
- Backoff: exponential with jitter; rate-limit/network class 30–60s
  scatter, other failures ±50%; cap 300s. **Never drop.**
- Ordering: FIFO by `id` per partition; per-tenant ordering is best-effort
  (sink assigns `ledger_seq`).
- High-volume classes (`auth.token.issue` ‡, `message.send` ‡,
  `notification.send` ‡): L1 aggregation — relay sends summaries, details
  stay in local logs (registry §1.8).
- Extreme-risk ops (tenant delete, master-key reset): synchronous minimal
  audit ack + async full event (per `COMBINATION_MODEL.md` §3).

### 1.3 Idempotency keys

- `event_id = SHA-256(service | event_type | tenant_id | trigger_key)[:32]`
  where `trigger_key` is a **stable business identifier** of the operation:
  `session_id`/`request_id` for auth flows, `file_id|version` for file ops,
  `account_id|op_ts` for account ops, `message_id|edit_seq` for IM.
- Retries **reuse** the same `event_id` (derived, never regenerated per
  attempt). The sink dedups on `(source_service, event_id)`; duplicate
  delivery returns success (at-least-once).
- Distinct occurrences of the same operation type must yield distinct
  `trigger_key` (e.g., download: `file_id|request_id`).

### 1.4 Dead-letter handling

- Permanent failures (schema validation, unknown event_type, tenant
  mismatch, payload too large, unparseable) → `status=3 (dead)` with
  `last_error`; batch continues with remaining rows (poison event must not
  block the queue).
- Dead rows are visible to operators; replay tool flips `3→0` after fix.
- Relay emits `audit.outbox.failure` (permanent failure) and
  `audit.relay.dlq.replay` (replay) events per registry — **but only if the
  sink is reachable**; otherwise the failure is local-logged.
- DLQ alerting threshold: `dead_count > 0` for >15min or `attempt_count`
  stuck >24h → alert channel (aero-im notification is a delivery channel
  only; the fact is the ledger/local log).

### 1.5 Common failure modes

| Failure | Detection | Handling |
|---|---|---|
| Ingest down / 5xx / network | non-2xx, timeout | backoff, retry, never drop; business flow unaffected (outbox) |
| 429 rate limit | header/status | jittered backoff 30–60s (rate class) |
| Auth failure (token/scope) | 401/403 | refresh token; if scope missing → alert (scope grant problem, not retryable forever) |
| Duplicate delivery | sink dedup ack | treat as success |
| Schema/validation 4xx | 422 | → dead; registry version bump or payload fix |
| Tenant mismatch | 422/403 at sink | → dead + alert (possible misrouting/compromise) |
| Poison event | repeated failure of one row | → dead, batch continues |
| Crash between send and mark-sent | lease expiry, row still pending | at-least-once redelivery; sink dedup absorbs |
| Sink clock skew | occurred_at vs sink time | sink rejects/annotates skew >5min `[PROPOSED]` |
| Queue growth | row count / lag metric | alert; throttle business writes only as last resort |
| Local DB loss | — | outbox rows lost with business tx (unavoidable; ledger gap detected by integrity spot-checks) `[PROPOSED]` |

### 1.6 Migration phases (common skeleton)

1. **P0 — sink readiness**: audit-governance ingest API, dedup, ADR-0002
   schema validation, tenant check live.
2. **P1 — schema + relay behind flag**: outbox DDL; relay in
   log-only/dry-run mode (L1 output, no ledger writes).
3. **P2 — dual write**: business transactions begin writing outbox rows
   (same tx); relay enabled (L2) for non-critical event classes first.
4. **P3 — cutover**: all event classes; local audit log remains as L0
   complement/fallback, not a second truth.
5. **P4 — backfill** `[PROPOSED]` (scope decision needed): bounded,
   idempotent import of legacy local audit data; historical rows lacking
   required fields (tenant_id, actor) are skipped or marked `[PROPOSED]`
   in payload, never fabricated.
6. **P5 — gate**: DLQ/alert monitoring live; new write/admin op without
   event type fails review (registry gate).

---

## snaplink (IdP — Go)

**Connector status**: **none today** — local audit-log concept only
(Verified gap). This section is entirely `[PROPOSED]`.

- **Outbox**: `audit_outbox` table in the snaplink DB. Auth-flow rows
  (`auth.*`) written in the auth transaction where feasible; login
  success/failure rows written post-commit keyed by `request_id`
  (session may not exist on failure).
- **Relay**: Go worker mirroring the aero-id publisher pattern; token via
  its own `client_credentials` client (`snaplink` → audit-governance).
- **Idempotency**: `trigger_key = request_id` for login/flow events;
  `session_id` for session/token events; `role_id|subject_id` for
  grants/revokes.
- **DLQ/failures**: common §1.4–1.5; special case — `auth.token.issue` ‡
  never enters the outbox (L1 aggregation only), `auth.login.failure` is
  C2 ledgered (security monitoring).
- **Sync-minimal-audit**: `tenant.delete` and platform client-secret
  rotation require sync minimal ack before commit (`COMBINATION_MODEL.md` §3).
- **Migration**: P0 sink-ready → P1 table+flag → P2 dual-write
  non-sensitive classes (`auth.logout`, `auth.device.*`) → P3 full
  cutover → P4 backfill of local audit log `[PROPOSED]` (risk: historical
  entries lack tenant/actor fields; default: skip) → P5 gate.

---

## snaplink-console (Flutter admin console)

**Connector status**: **consumer only** (Verified: `audit_log_service.dart`
reads audit data). **No outbox needed** `[PROPOSED]`.

- **Design**: console-native facts are generated at the edge, not in the
  console:
  - console logins → snaplink `auth.login.*` events with
    `client_id=console` (console adds no duplicate event).
  - console audit queries → **audit-governance emits `audit.event.read` /
    `audit.event.export` self-audit rows on the query API** (ledger
    records who read what — no console-side table, cannot be bypassed by
    direct API calls) `[PROPOSED]`.
  - `console.session.revoke` → snaplink `auth.session.revoke` via the
    BFF's user session.
- **Failure modes**: query failures are console-side UX concerns; the
  ledger record of the query attempt is written by audit-governance
  itself (fail-closed on read events only if the read succeeded).
- **Migration**: none (no schema); verify the audit page queries carry
  tenant + trace_id; no backfill.

---

## snaplink-audit-governance (Go — ledger)

**Connector status**: it **is** the sink — no outbox/relay for its own
ops; uses a **self-audit writer** `[PROPOSED]`.

- **Outbox**: none. Governance operations (legal hold apply/release,
  export request/complete, retention update, checkpoint, integrity
  verify) append their self-audit events **in the same transaction** as
  the operation; if the append fails the operation **aborts** (fail-closed
  — governance ops are the one place sync audit is correct).
- **Relay**: none (same-process writer). Checkpoint sealing is a
  `system:<audit-governance>` event written by the checkpoint job.
- **Idempotency**: `trigger_key = operation_id` (hold_id, export_id,
  policy_id, checkpoint_id); unique `(service, event_id)` enforced by
  ledger-level unique index.
- **Ingest API contract** (the sink side, consumed by all producers):
  - Auth: publisher `client_credentials` JWT with `audit:event:ingest`;
    client allowlist.
  - Dedup: `(source_service, event_id)` — duplicate → success ack.
  - Validation: ADR-0002 schema registry (event_type known, version
    supported, required fields, metadata-only payloads); failure → 422
    with reason (producer marks row dead).
  - Tenant consistency: envelope `tenant_id` must match publisher token
    tenant scope; mismatch → 422 (scope-matrix Flow 3).
  - Skew: `occurred_at` >5min in future → reject; >5min past → annotate.
- **DLQ**: sink-side rejections are the producer's DLQ trigger; ledger
  itself has no DLQ.
- **Failure modes**: append failure → governance op aborts (no partial
  legal hold); ingest surge → 429 + bounded queue; hash-chain checkpoint
  failure → alert, ledger keeps accepting (integrity risk surfaced by
  `audit.integrity.verify.failure`).
- **Migration**: P0 first (it is P0); implement self-audit writer +
  ingest API before any producer cutover.

---

## aero-id (Go — account domain)

**Connector status**: **exists (Verified)** —
`internal/connector/auditgovernance/` (publisher/oauth). Details below
`[PROPOSED]` until validated against the actual connector.

- **Outbox**: `audit_outbox` in aero-id DB; account rows
  (`account.*`, `admin.account.*`) written in the account transaction.
- **Relay**: the existing publisher worker; token via `publisher/oauth`
  (Snaplink client_credentials). Verify it implements §1.2 lease/batch/
  backoff semantics; align if not.
- **Idempotency**: `trigger_key = account_id|op_ts` for updates,
  `account_id|consent_id` for consent, `request_id` for admin ops.
- **DLQ/failures**: common §1.4–1.5; verify connector maps 422→dead and
  keeps 5xx retrying.
- **Migration**: P0 sink-ready → P1 verify table matches connector
  expectations (or add DDL) + dry-run → P2 enable non-sensitive
  (`account.update`) → P3 full cutover → P4 backfill `[PROPOSED]`
  (bounded; skip rows without tenant/actor) → P5 gate. Expected to be
  the **fastest rollout** (pattern already proven).

---

## aero-vault (Go — file platform)

**Connector status**: **scaffolding exists (Verified)** —
`internal/auditgovernance/token.go` (tokenSource). Details `[PROPOSED]`.

- **Outbox**: `audit_outbox` in vault DB; per `decisions/04-composition.md`
  the outbox write is **inside the business transaction** — for
  `file.delete.admin` the delete, the `file.delete.admin` audit row and
  the `file.deleted.notify` row are generated in the same tx (audit and
  notify are same-source, no second truth).
- **Relay**: Go worker using the `token.go` tokenSource; HTTP ingest.
- **Idempotency**: `trigger_key = file_id|version` for file content
  events, `file_id|request_id` for downloads (distinct occurrences!),
  `file_id|share_id` for share ops, `request_id` for admin ops.
- **DLQ/failures**: common §1.4–1.5; special case — `file.download` ‡
  volume: L1 aggregation decision pending (registry open question #3).
- **Migration**: P0 sink-ready → P1 DDL + relay dry-run → P2 dual-write
  (`file.metadata.update`, `file.share.create`) → P3 full cutover
  (incl. delete-flow tx integration) → P4 backfill of historical file
  events `[PROPOSED]` (default: none initially — tombstone/history is
  vault-side per 04-composition C13) → P5 gate.

---

## aero-im (Rust — messaging/live/notifications)

**Connector status**: **none today (Verified gap)** — no audit connector;
only local content-governance logic. Entire section `[PROPOSED]`.

- **Outbox**: `audit_outbox` table in the IM store (SQL if present, else
  the message store's durable table); rows for `message.*`, `room.*`,
  `live.*`, `notification.*`, `admin.*` written in the business
  transaction. `message.send` ‡ and `notification.send` ‡ never enter
  the outbox (L1 aggregation).
- **Relay**: new Rust worker (first connector in this stack) implementing
  §1.2; client_credentials via snaplink; **scope grant
  (`audit:event:ingest`) is conditional on relay implementation** per
  scope-matrix Flow 3.
- **Idempotency**: `trigger_key = message_id|edit_seq` for
  edit/delete/retract, `room_id|member_uid` for membership,
  `stream_id` for live, `recording_id` for recordings,
  `request_id` for admin/moderation ops.
- **DLQ/failures**: common §1.4–1.5; special case — moderation events
  (`admin.content.flag`, `admin.moderation.action`) are C2/C1 and must be
  delivered before the outbox backlog drains: give them a priority
  partition (flag column or separate table) `[PROPOSED]`.
- **Migration**: P0 sink-ready → P1 DDL + relay dry-run → P2 dual-write
  (`room.create`, `notification.template.update`) → P3 full cutover →
  P4 backfill of content-moderation history `[PROPOSED]` (bounded,
  metadata-only) → P5 gate.

---

## 2. Rollout order & dependencies

| Phase | Service | Depends on |
|---|---|---|
| 1 | snaplink-audit-governance (sink: ingest API + self-audit) | ADR-0002 schema registry, scope registry |
| 2 | aero-id (connector exists — prove pattern) | sink live |
| 3 | aero-vault (delete-flow tx integration) | sink live, 04-composition tx design |
| 4 | snaplink (IdP — largest surface, L1 aggregation for token.issue) | sink live, scope registry (P0-3) |
| 5 | aero-im (new Rust connector) | sink live, `audit:event:ingest` grant after relay exists |
| 6 | snaplink-console (consumer verification; edge-generated read events) | sink self-audit read events |

## 3. Open decisions

1. **Backfill policy** `[PROPOSED]`: which services backfill legacy local
   logs (default: vault none, id/vault bounded, snaplink skipped unless
   fields exist). Needs compliance input.
2. **L1 aggregation thresholds** for `auth.token.issue`, `message.send`,
   `notification.send`, `file.download`, `file.rag.query` — summary
   granularity and whether summaries satisfy legal-hold needs.
3. **aero-im priority partition** for moderation events — flag column vs
   separate table.
4. **Sink sync-ack scope**: confirm the exact extreme-risk op list
   (tenant delete, master-key reset, +?) per `COMBINATION_MODEL.md` §3.
5. **Connector alignment**: validate aero-id publisher and aero-vault
   token.go against §1.2 semantics (lease/batch/backoff/422→dead) before
   rollout — they are the pattern, but may need hardening.
6. **DLQ visibility**: whether `audit.outbox.failure`/`audit.relay.dlq.replay`
   become ledgered events (they must not be generated by a relay whose
   sink is down — local-log fallback specified).
