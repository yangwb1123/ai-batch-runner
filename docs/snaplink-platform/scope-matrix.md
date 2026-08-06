Scope matrix draft written to `docs/snaplink-platform/scope-matrix-draft.md` (15KB, 4 flows + cross-cutting rules).

**§0 Provenance & conventions** — verified anchors (aero-im→vault BlobStore via client_credentials, aero-id gRPC validation, connector scaffolding, console BFF rule, `vault:blob:read/write`/`audit:event:ingest`/`account:uid:resolve` registry seed names) separated from `[PROPOSED]` scope names. Conventions: `<domain>:<resource>:<action>` scope format bound to client_id, service tokens carry no user scopes, permission naming per `COMBINATION_MODEL.md`, admin ops require snaplink authorization check + `decision_id`.

**Per-flow sections** (`##` per flow):

1. **aero-im → aero-vault BlobStore**: needs `vault:blob:write` (PUT only) + `vault:blob:read` (GET only, droppable). Deny list: file delete/admin, share management, export, `vault:rag:*` (no silent RAG indexing of attachments), metadata update, user scopes, audit read-back. Open decision: service-owned vs per-user delegation (RFC 8693) for attachment attribution.
2. **aero-id → snaplink gRPC**: `snaplink:token:validate` (introspection, claim-minimized: subject/tenant/expiry/scopes only — no profile PII) or mTLS allowlist alternative (pick one). Deny: all `snaplink:user:*`/`policy:*`/`client:*` management, blanket token revoke, self-grant of `account:uid:resolve` (circular trust).
3. **aero-vault/aero-im → audit-governance ingest**: `audit:event:ingest` (append-only, idempotent by event_id, tenant consistency check). aero-im grant is conditional on implementing the missing Outbox relay (verified gap). Deny: read/export/legalhold/retention/integrity/compaction — and `audit:event:update`/`delete` must not exist at all (immutable ledger).
4. **console → every service admin API**: two-token model (admin user OIDC session + BFF service token holding enumerated admin scopes per service, e.g. `snaplink:admin:user:manage`, `account:admin:user`, `vault:file:admin`, `im:admin:room`, `audit:event:read/export`, `audit:legalhold:apply`). Binding rules: per-operation authorization check with decision_id, view/write separation, tenant scoping. Deny: blob/user-level scopes, `audit:event:ingest`, wildcards, service-secret rotation without dual control.

**Cross-cutting deny rules** (no user scopes on service tokens, no ledger read-back by producers, no delete/update/compaction scopes, no wildcards, one-domain-per-client, tenant consistency) + **6 open decisions** (registry landing P0-3, delegation model, gRPC authn choice, aero-im relay sequencing, admin scope granularity split, dual-control flows).

All scope names are proposals pending snaplink scope-registry landing and human approval, per `MULTI_REPO_OPERATIONS.md` contract rules.
