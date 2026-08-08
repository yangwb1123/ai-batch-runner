Per-service audit ingestion design written to `docs/snaplink-platform/audit-ingestion-design-draft.md` (17KB).

**§0 Verified anchors** — aero-id `connector/auditgovernance` (publisher/oauth) and aero-vault `internal/auditgovernance/token.go` cited as the reference pattern; snaplink & aero-im flagged as verified connector gaps; 04-composition's "outbox inside the vault transaction" decision honored.

**§1 Common design** (applies to all producers):
- **Outbox schema**: `audit_outbox` with `event_id` (deterministic idempotency key), `UNIQUE(service, event_id)`, status 0/1/2/3, `attempt_count`, `next_attempt_at`, `lease_until`, written in the same tx as the business op
- **Relay behavior**: lease-based claiming (SKIP LOCKED), batch 100, client_credentials tokenSource, exponential backoff with jitter (rate class 30–60s, cap 300s), **never drop**, L1 aggregation for ‡ high-volume events, sync-minimal-ack only for extreme-risk ops
- **Idempotency**: `event_id = SHA-256(service|event_type|tenant_id|trigger_key)` with stable business trigger keys (request_id, file_id|version, message_id|edit_seq…) — retries reuse, sink dedups
- **DLQ**: permanent failures (422/schema/tenant mismatch) → dead status, poison events don't block batches, `audit.outbox.failure`/`audit.relay.dlq.replay` events, alert thresholds
- **Failure modes table** (10 modes incl. crash-between-send-and-mark absorbed by dedup, sink clock skew, queue growth) + **6-phase migration skeleton** (sink-ready → flag/dry-run → dual-write → cutover → bounded backfill → gate)

**Per-service sections** (`##` per service) — connector status + deltas:
- **snaplink**: no connector (gap); auth rows keyed by request_id; `auth.token.issue` stays L1; sync-ack for `tenant.delete`; backfill skipped unless fields exist
- **snaplink-console**: **no outbox** — consumer only; console-native facts generated at the edge (login via snaplink `client_id=console`, audit queries self-audited by the ledger's read API so direct API calls can't bypass)
- **snaplink-audit-governance**: no outbox — **self-audit writer in the same tx**, fail-closed (governance op aborts if its own ledger append fails); defines the sink-side ingest contract (dedup, ADR-0002 validation, tenant consistency, skew rejection)
- **aero-id**: connector exists (fastest rollout); verify it matches lease/batch/backoff semantics
- **aero-vault**: delete-flow tx integration per 04-composition (delete + `file.delete.admin` + notify same tx); `file_id|request_id` trigger key for distinct downloads
- **aero-im**: new Rust connector needed; scope grant conditional on relay existing; moderation events get priority partition

**§2 Rollout order** (sink → aero-id → aero-vault → snaplink → aero-im → console) + **§3 six open decisions** (backfill policy, L1 thresholds, moderation priority, sync-ack list, connector hardening, DLQ event ledgering when sink is down).

All details marked `[PROPOSED]` pending per-codebase validation and human approval, consistent with the registry and scope matrix drafts.
