Review complete. Written to **`docs/snaplink-platform/v2/reviews/distributed-systems-review-v2.md`** (36KB). All claims verified against current code across the 6 repos; no tests executed, no code modified.

## Summary of the distributed-systems review

### State map highlights (Verified)
- **Sink**: whole-snapshot single-row store (file rename without fsync, or PG `audit_state_snapshot` CAS). Effective single-writer; 2+ replicas → 500s. Per-(tenant,stream) hash chain; order = arrival, not `occurred_at`. Quota is per-instance.
- **aero-id**: ledger + `audit_publications` same-tx in 6 repo paths; `pg_advisory_xact_lock` serializes per-partition chain. 8 service sites post-commit with `_ =` discard.
- **aero-vault**: delete + L0 audit + outbox facts in one tx; lease-fenced claims (TTL 30s > 2×HTTP timeout); `(origin_kind, origin_id)` UNIQUE + delivered-origins tombstone — regeneration-safe locally.
- **IdP**: refresh tokens in session store (`refresh:` keys), JWT access tokens stateless, no jti.

### Top findings
| # | Sev | Finding |
|---|---|---|
| DS-01 | **Critical** | `AUDIT_ALLOW_DEV_AUTH` **defaults true**, dev-only config accepted, `dev:platform:platform-admin` bypasses all permissions (S1 violated in code) |
| DS-02 | **Critical** | Vault retries forever (no dead state) **and** one poison row flips vault `/readyz` 503 at 900s backlog — file-platform outage from the audit side-channel (F3+H1) |
| DS-03 | High | Sink CAS conflict → bare 500, no retry; file backend no fsync (F-01) |
| DS-04 | High | Deterministic `event_id` (F4) unimplemented everywhere — gap-reconcile/regeneration mints new UUIDs → sink dedup vacuous → silent duplicate ledger rows |
| DS-05 | High | 8 post-commit `_ = Record(...)` sites (F5); metric fires but no alert/rollback/replay |
| DS-06 | High | IdP refresh rotation is non-atomic Get→Delete→Create — concurrent replay splits the family; no reuse detection; JWT tokens not revocable |
| DS-07 | High | IdP mints no kid/iss/scope/client_id/tenant_id/roles — **T-1.2 e2e with real tokens is blocked today** (sink gets `client_id=""` → 403); discovery advertises `/authenticate` + non-existent `/revoke` |
| DS-08 | Medium | Sink silently **overwrites** envelope `tenant_id` with token tenant instead of 422 (design says fail-closed) |
| DS-09 | Medium | readyz has no store probe; gRPC ingest is active but undeclared; relay/Kafka in verify compose |

### Scenario table
Covers sink partition, producer crash between send/mark-sent, sink replica crash, power loss, clock rollback (no skew check exists), stale token cache (aero-id lacks 401 invalidation; vault has it), IdP outage, split-brain (dedup absorbs — only with stable IDs), stale reads, governance-op atomicity (verified good), refresh-token replay, per-replica quota.

### Bottom line
Every v2-mandated defect was re-verified as still present at B0; three are under-weighted in the docs: envelope-tenant overwrite (DS-08), IdP rotation race + missing jti (DS-06), and the undeclared gRPC topology (DS-09). Batch ordering is sound; DS-01/DS-02 are the release-blocking gates.
