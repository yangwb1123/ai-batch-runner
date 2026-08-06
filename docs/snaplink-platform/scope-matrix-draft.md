# Snaplink Platform — Service-to-Service Scope Matrix (Draft)

> **Status**: DRAFT — proposal for human approval before promotion to
> contract (per `MULTI_REPO_OPERATIONS.md`: scope matrix enters repo
> contract docs only after manual approval; per-repo `scope-matrix-v1.md`
> files are derived from this platform-level matrix).
> **Evidence standard**: verified anchors cited; all scope names and grants
> are `[PROPOSED]` until the snaplink platform scope registry lands (P0-3).

## 0. Provenance & conventions

### 0.1 Verified anchors

| Anchor | Source |
|---|---|
| aero-im attachments → aero-vault BlobStore via Snaplink `client_credentials` | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| aero-vault service auth uses Snaplink browser flow (user path) + tokenSource pattern | `SCENARIO_ANALYSIS.md` §0, P0-3 (Verified) |
| aero-id validates tokens against Snaplink via gRPC; has `connector/auditgovernance/` publisher/oauth | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| aero-vault has `internal/auditgovernance/token.go` | `SCENARIO_ANALYSIS.md` §0 (Verified) |
| aero-im has **no** audit connector (gap) | `SCENARIO_ANALYSIS.md` §0 (Verified gap) |
| console: capability-driven nav via runtime `/api/v1/admin/endpoints`; browser must not bypass BFF | `SCENARIO_ANALYSIS.md` §0; `decisions/04-composition.md` (Verified) |
| Platform scope registry seed names: `vault:blob:read/write`, `im:room:write`, `audit:event:ingest`, `account:uid:resolve` | `SCENARIO_ANALYSIS.md` P0-3 (Proposed, cited) |
| Permission naming `<domain>.<resource>.<action>`; unified authz `POST /v1/authorization/check` → decision + policy_id + **decision_id** (decision_id enters audit) | `COMBINATION_MODEL.md` §4, §5 (Proposed, cited) |
| Identity context header contract: subject_id/tenant_id/org/session/client_id/scopes/request_id/trace_id | `COMBINATION_MODEL.md` §4 (Proposed, cited) |

### 0.2 Conventions

- **Scope format** `[PROPOSED]`: `<domain>:<resource>:<action>` — e.g.
  `vault:blob:write`, `audit:event:ingest`. Scopes are **client-bound**:
  a scope grant is valid only for the named `client_id` in snaplink's
  registry, never a wildcard.
- **Token model** `[PROPOSED]`: service-to-service uses OAuth 2.1
  `client_credentials` — the token subject is the **service client**, not a
  user. No user-scopes (`openid`, `profile`, `email`) on service tokens.
  User-context delegation (console BFF → services) is Flow 4.
- **Permission set** maps a scope to the concrete operations it unlocks;
  permissions are enforced at the resource API, scopes are the admission
  gate at the token boundary.
- **Minimum permission principle**: grant the smallest scope set that makes
  the flow work; every admin API additionally requires a per-admin
  snaplink authorization check producing a `decision_id` — a scope alone is
  never sufficient for admin operations `[PROPOSED]`.
- **Tenant context** `[PROPOSED]`: service tokens carry the service's own
  tenant scope (platform/service tenant), and the caller must forward the
  end-user's `tenant_id` via identity-context headers; target service
  validates header vs token consistency.

---

## Flow 1: aero-im → aero-vault (attachment BlobStore)

> Verified: attachment upload/download already flows aero-im → vault
> BlobStore with Snaplink `client_credentials`. Scope names and grants
> below are `[PROPOSED]`.

### Scopes needed (minimum)

| Scope | Granted to | Permission set (min) | Notes |
|---|---|---|---|
| `vault:blob:write` | `aero-im` client | `vault.blob.create` — BlobStore PUT only | Attachment upload; must NOT include metadata or delete |
| `vault:blob:read` | `aero-im` client | `vault.blob.get` — BlobStore GET only | Download/delivery path (read receipts, re-download); may be dropped if delivery is fully vault-side `[PROPOSED]` |

### Must NOT grant to aero-im

| Scope | Why not |
|---|---|
| `vault:file:delete` / `vault:file:admin` | Attachment lifecycle is vault-owned; aero-im must not delete user files (incl. no `file.delete.admin` path) |
| `vault:share:manage` / `vault:permission:update` | No share/ACL manipulation from IM context |
| `vault:file:export` | No bulk export capability for a messaging service |
| `vault:rag:*` (`vault:rag:index`/`vault:rag:query`/`vault:rag:reindex`) | Attachments must not be silently RAG-indexed by IM |
| `vault:file:metadata:update` | No rename/move/tag from IM |
| `openid` / `profile` / `email` | Service identity must not impersonate users; attachment attribution is `delegated:<aero-im>:<uid>` via identity headers, not user tokens |
| `audit:event:read` / `audit:event:export` | Producer must not read back the ledger (anti-tamper: cannot hide its own traces) |

**Open decision** `[PROPOSED]`: attachment ownership model — service-owned
blobs with user reference in metadata vs per-user delegation (RFC 8693
token exchange). Affects `file.upload` actor field in the audit registry.

---

## Flow 2: aero-id → snaplink (gRPC token validation)

> Verified: aero-id already validates tokens against Snaplink over gRPC.

### Scopes needed (minimum)

| Scope | Granted to | Permission set (min) | Notes |
|---|---|---|---|
| `snaplink:token:validate` `[PROPOSED]` | `aero-id` client | `snaplink.token.introspect` — validate token, return `subject_id`, `tenant_id`, `expires_at`, `scopes`, **no** profile/PII claims | Introspection is the only operation aero-id needs from snaplink |
| (alternative) none — gRPC mTLS + client allowlist `[PROPOSED]` | `aero-id` client | same, admission by client cert/allowlist instead of OAuth scope | Pick one; do not maintain both |

### Minimum permission set (returned claim contract)

`subject_id (uid)`, `tenant_id`, `token_type`, `expires_at`, `scopes`,
`active: bool` — **explicitly excluding** `email`, `name`, `phone`,
`profile`, `groups` unless a separately justified claim is added.

### Must NOT grant to aero-id

| Scope | Why not |
|---|---|
| `snaplink:user:manage` / `snaplink:user:lock` / `snaplink:user:attribute:update` | Account domain must not manage identities (identity lives in snaplink) |
| `snaplink:role:grant` / `snaplink:role:revoke` / `snaplink:policy:*` | RBAC mutations are IdP-admin only |
| `snaplink:client:manage` / `snaplink:client:secret:rotate` | OAuth client lifecycle is IdP-admin only |
| `snaplink:token:revoke` (bulk) | Only user-initiated/session-scoped revocation; no blanket revoke from account domain |
| `openid` / `profile` / `email` on the service token | Claim minimization: aero-id gets introspection claims, not userinfo |
| `account:uid:resolve` (self-grant) | `account:uid:resolve` is for **other** services resolving account views from aero-id; aero-id must not hold it (no circular trust) |

**Note** `[PROPOSED]`: gRPC validation responses must be logged with
`trace_id` and the snaplink decision (`decision_id`) for audit continuity
with `auth.*` events in the registry.

---

## Flow 3: aero-vault / aero-im → audit-governance (event ingest)

> Verified: aero-vault (and aero-id, out of scope here) have connector
> scaffolding; **aero-im connector is a verified gap** — aero-im ingest is
> `[PROPOSED]` pending implementation of the Outbox relay.

### Scopes needed (minimum)

| Scope | Granted to | Permission set (min) | Notes |
|---|---|---|---|
| `audit:event:ingest` | `aero-vault` client | `audit.event.create` — append-only write, one event type per authz check, **no update/delete** | Outbox relay → ingest API, idempotent by `event_id` |
| `audit:event:ingest` | `aero-im` client | same | Gap: relay to be implemented first `[PROPOSED]` |
| (publisher auth) `snaplink:token:validate` | both, if ingest API validates publisher tokens `[PROPOSED]` | validate + tenant consistency check | Or rely on client_credentials JWT validation at the ingest edge |

### Ingest contract `[PROPOSED]`

- Idempotency: retries must reuse the same `event_id`; replay must be a no-op.
- Tenant consistency: `tenant_id` in event envelope must match the tenant
  scope of the publishing client; mismatch → reject (`audit.relay.dlq.replay`
  path).
- Payload: metadata-only per audit registry §1.7 (no file/message content).
- Failure handling: ingest unavailable must not block business flow
  (Outbox pattern); DLQ visibility per `audit.outbox.failure`.

### Must NOT grant to publishers

| Scope | Why not |
|---|---|
| `audit:event:read` | Producers must not read the ledger (cannot verify/alter their own trail) |
| `audit:event:export` | Export is console/compliance-admin only |
| `audit:legalhold:apply` / `audit:legalhold:release` | Legal hold authority is governance-admin only |
| `audit:retention:update` | Retention policy is platform-admin only |
| `audit:integrity:verify` / `audit:integrity:verify:run` | Verification is governance-admin only |
| `audit:compaction:*` `[PROPOSED]` | Compaction/anonymization must never be callable by producers |
| `audit:event:update` / `audit:event:delete` | Ledger is append-only; these scopes must not exist |

---

## Flow 4: snaplink-console → every service (admin APIs)

> Verified: console is the control plane (capability-driven nav via
> `/api/v1/admin/endpoints`, audit timeline page). Browser must not bypass
> BFF (04-composition). Console's per-service admin access is `[PROPOSED]`
> in full: the BFF holds service scopes; the **acting admin user** is
> authorized per-operation via snaplink RBAC (decision_id in audit).

### Scopes needed (minimum) — held by the console BFF client

| Target service | Scope(s) | Permission set (min) |
|---|---|---|
| snaplink (IdP) | `snaplink:admin:user:manage` `[PROPOSED]` (lock/unlock/attribute/MFA reset) | `snaplink.user.lock`, `snaplink.user.unlock`, `snaplink.user.attribute.update`, `snaplink.user.mfa.reset` |
| snaplink (IdP) | `snaplink:admin:policy` `[PROPOSED]` (roles/groups/conditions) | `snaplink.role.*`, `snaplink.group.*`, `snaplink.policy.condition.update` |
| snaplink (IdP) | `snaplink:admin:client` `[PROPOSED]` (OAuth apps) | `snaplink.client.register/update/delete/secret.rotate/scope.update/grant.update` |
| snaplink (IdP) | `snaplink:admin:tenant` `[PROPOSED]` | `snaplink.tenant.create/update/suspend/activate/delete` |
| aero-id | `account:admin:user` `[PROPOSED]` | `account.admin.view`, `account.admin.attribute.update`, `account.admin.delete`, `account.uid.resolve` (display-name projection) |
| aero-vault | `vault:file:admin` `[PROPOSED]` | `vault.file.list`, `vault.file.view`, `vault.file.delete.admin`, `vault.file.export` |
| aero-im | `im:admin:room` `[PROPOSED]` | `im.admin.message.delete`, `im.admin.room.archive`, `im.admin.room.member.remove` |
| aero-im | `im:admin:notification` `[PROPOSED]` | `im.notification.channel.update`, `im.notification.template.update` |
| audit-governance | `audit:event:read` | `audit.event.read` (timeline queries) |
| audit-governance | `audit:event:export` | `audit.event.export` |
| audit-governance | `audit:legalhold:apply` / `audit:legalhold:release` `[PROPOSED]` | governance operations, separate from view scopes |
| audit-governance | `audit:retention:update` `[PROPOSED]` | retention policy administration |

### Minimum permission set (binding rules) `[PROPOSED]`

1. **Two-token model**: console end-user session (OIDC auth code flow, admin
   role) + BFF service token (`client_credentials` with the scopes above).
   Both must pass: RBAC for the **user**, scopes for the **BFF client**.
2. **Per-operation authorization check**: BFF calls snaplink
   `POST /v1/authorization/check` for each admin action; `decision_id`
   recorded in the corresponding audit event (registry `admin.*` rows).
3. **View/write separation**: read-only browse scopes
   (`vault:file:admin:view`, `audit:event:read`) are granted to the console
   client; mutating scopes are granted only where the console actually
   implements the action, and each is individually registered.
4. **Tenant scoping**: every admin API verifies the target resource belongs
   to the acting admin's `tenant_id` (identity-context header vs resource
   tenant) — no cross-tenant admin via scope alone.

### Must NOT grant to the console client

| Scope | Why not |
|---|---|
| `vault:blob:write` / `vault:blob:read` | Console must not bypass vault's own file APIs with service-level blob access |
| `audit:event:ingest` | Console is a consumer of audit data, never a producer |
| `vault:rag:reindex` / `vault:rag:index` | RAG maintenance is vault-operations, not console |
| `im:room:write` / `im:message:send` (user-level IM scopes) | Console must not act as an IM user |
| `openid`/`profile`/`email` on the **BFF service token** | PII claims only on the admin's user session, never on the service token |
| `snaplink:admin:client`-level secrets (`client.secret.rotate` for platform service clients) | Rotating other services' credentials from console requires a separate approval flow (dual-control) `[PROPOSED]` |
| Any `*:admin` wildcard scope | Admin scopes are enumerated per resource; wildcards are forbidden by the registry |
| `snaplink:token:revoke` (global) | Only session-scoped revocation through the acting user's session |

---

## Cross-cutting deny rules (all flows) `[PROPOSED]`

1. **No user scopes on service tokens** — `openid/profile/email` never
   appear in `client_credentials` grants.
2. **No ledger read-back by producers** — `audit:event:read/export` only
   for console/compliance clients.
3. **No delete/update/compaction scopes on the audit ledger** — these
   scopes must not exist in the registry.
4. **No wildcard or `*:admin` scopes** — every grant is
   `<domain>:<resource>:<action>` enumerated and bound to a `client_id`.
5. **No cross-domain privilege accumulation** — each service client gets
   scopes in exactly one consuming domain (aero-im: `vault:blob:*` only;
   aero-vault: `audit:event:ingest` only; aero-id: `snaplink:token:validate`
   only).
6. **Tenant consistency everywhere** — service token tenant scope must
   match identity-context header; mismatch → reject.

## Open decisions

1. **Scope registry landing (P0-3)**: exact scope names above must be
   registered in snaplink with per-client allowlists before any grant is
   valid; this document is the input list.
2. **Delegation model** `[PROPOSED]`: RFC 8693 token exchange for
   user-context service calls (Flow 1 attachment attribution) vs
   identity-header forwarding; pick one platform-wide.
3. **gRPC validation authn** (Flow 2): scope-based vs mTLS allowlist — do
   not maintain both.
4. **aero-im audit relay** (Flow 3): implement Outbox relay first; ingest
   grant for aero-im is conditional on it.
5. **Console admin scope granularity** (Flow 4): split `vault:file:admin`
   into `vault:file:admin:view` / `vault:file:admin:delete` /
   `vault:file:admin:export` to allow read-only admin roles.
6. **Dual-control flows** `[PROPOSED]`: platform service secret rotation
   and legal-hold operations require two-administrator approval; scope
   grants alone must not enable single-actor execution.
