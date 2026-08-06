Review complete. Report written to `docs/snaplink-platform/reviews/database-architect-review.md` (34KB). Summary of what I verified across the six platform repos:

## Store inventory (hot vs durable, stock wiring)

| Service | Store | Durable? | Stock binary wires it? |
|---|---|---|---|
| snaplink (IdP) | identity + audit `backend: memory` (stock), sqlite/postgres optional | **Hot by default** | audit recorder yes (memory ring); **no governance relay in `cmd/sso-server`** (connector used only by provisioner/billing cmds) |
| snaplink-console | browser `localStorage` 1000-ring | Hot | n/a — draft's "consumer only (Verified)" claim is **wrong**: it records local ops, reads no audit API |
| snaplink-audit-governance (sink) | **one JSON snapshot row** (file or Postgres `audit_state_snapshot`) + ClickHouse projector + S3 archive | File: no fsync; PG: durable | Yes — but the relational `ledger_events` schema in migration 001 is **unused by the runtime** |
| aero-id | Postgres: local signed hash-chain ledger + `audit_publications` outbox | Durable | Local ledger yes; **governance publisher flag-off by default** (`enabled: false` in stock config) |
| aero-vault | SQLite WAL (MaxOpenConns=1) | Durable | Relay fully built and wired, **default disabled** (`AUDIT_GOVERNANCE_ENABLED=false`) |
| aero-im | Postgres + local `audit_events` | Durable | Local yes; **no connector (Verified gap)** |
| snaplink-sso (legacy) | in-memory rings | Hot | Not part of the 6-repo platform |

## Key findings (top severity)

- **F-01 High**: The sink keeps the **entire ledger in one JSON snapshot** — every ingest is an O(ledger-size) read-modify-write with optimistic locking and **no conflict retry**; the relational ledger schema it ships is dead code. Hard ceiling for the draft's high-volume classes.
- **F-02 High**: Stock snaplink audit is **volatile by default** (memory backend, hash-chain off) — the draft's "L0 complement" premise needs a durability decision first.
- **F-03 Med/High**: Both producer connectors exist but **no stock binary publishes today** — the "fastest rollout (pattern proven)" claim is code-true, deployment-false.
- **F-04 Medium**: Anchor-pattern backoff/DLQ diverges from draft §1.2/§1.4 — no 422→dead in either relay; vault has **no dead state at all** (retries forever).
- **F-06 Medium**: Sink's query API records **no `audit.event.read` self-audit** — the console's "cannot be bypassed" guarantee is unimplemented.

Positive verifications: vault delete-flow tx (04-composition) is real (`HardDeleteObjectWithEvent` — legal-hold + delete + audit + 2 facts atomic); aero-id chain append uses per-partition advisory locks with double dedup check; both relays use proper lease/SKIP LOCKED claiming; sink has approval-gated restore workflow and verified integrity APIs.

The migration sequence (P0–P5) is re-worked with a new "P0a sink hardening" phase, flag-gated compatibility windows, validation queries, and safe-disable guarantees. Volume/retention/recovery unknowns are itemized — no workload evidence exists anywhere for sizing the L1 aggregation thresholds.
