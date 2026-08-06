# Snaplink Platform Audit — Event Type Registry (v2 Revision)

> **Status**: DRAFT v2 — revision of `audit-event-registry-draft.md` (v1)
> incorporating gate findings S2 / F4 / F-06 / F13 / F-3 (per
> `decisions/gate.md` and `v2/revision-plan.md`). Still a proposal for
> human approval before promotion to contract (per
> `MULTI_REPO_OPERATIONS.md`).
> **Evidence standard**: verified anchors are cited; everything else is
> `[PROPOSED]` and must be validated against each codebase before use.
> **Revision marking**: every change vs v1 is marked `[v2]`; the v2
> revision log (§0.1) maps each change to its blocking item.
> **Gate**: under the full-composition deployment, any new write/admin
> operation without a corresponding event type = review FAIL
> (per `SCENARIO_ANALYSIS.md` P0-2).

## 0. Provenance — verified anchors vs proposed

### 0.0 Anchors (v1, retained)

| Anchor | Evidence | Source |
|---|---|---|
| Event id naming convention `<domain>.<verb>[.<scope>]` (e.g., `file.delete.admin`) | Verified | `docs/snaplink-platform/decisions/03-capabilities.md` |
| aero-id has audit-governance connector (`internal/connector/auditgovernance/` publisher/oauth) | Verified | `SCENARIO_ANALYSIS.md` §0 |
| aero-vault has audit-governance integration (`internal/auditgovernance/token.go`) | Verified | `SCENARIO_ANALYSIS.md` §0 |
| snaplink-console has audit log service page (`lib/services/audit_log_service.dart`) — **local `localStorage` 1000-ring, reads no audit API** (v1 "consumer only (Verified)" was inaccurate) `[v2]` | Verified | `SCENARIO_ANALYSIS.md` §0; `database-architect-review.md` F-06 |
| snaplink (IdP) has only a local audit-log concept; no governance connector found | Verified gap | `SCENARIO_ANALYSIS.md` §0 |
| aero-im has no audit-governance connector found | Verified gap | `SCENARIO_ANALYSIS.md` §0 |
| audit-governance: immutable ledger, hash chain, Legal Hold, export, integrity verification; event schema registry per ADR-0002 (design-time, single-node reference impl); **self-audit writer / read-path self-audit Missing** `[v2]` | Verified (partial) | `SCENARIO_ANALYSIS.md` §0, §1; distributed F13; database F-06 |
| All concrete event types, payload fields, retention classes, legal-hold sensitivities | **Proposed** | this document |

### 0.1 v2 revision log (2026-08-06)

| Blocking item | Change | Location |
|---|---|---|
| S2 | `tenant_id` and `actor.roles` REQUIRED on every event except `— (platform)` system-level events; `tenant_id (token)` pinned to the snaplink access-token `tenant_id` claim (v2 claim contract, scope-matrix-v2 Flow 2) | §1.2, §1.3, §1.4 |
| F4 | New §1.9: deterministic `event_id = SHA-256(source_service \| tenant_id \| event_type \| resource_id \| time_bucket)[:32]`; dedup keys made explicit at every layer; regeneration must reuse event_id; anti-squatting backstop | §1.9 |
| F-06 | Console section re-labeled "local-only today (Verified)"; `console.audit.view` / `console.export.view` **removed**; sink-generated `audit.event.read` / `audit.event.export` **added** | console §, sink § ledger |
| F13 | Self-audit writer normative: governance/ledger self-audit events written by the sink in the same store update, fail-closed (append failure aborts the op); supported ingest topology declared (outbox relay / Kafka consumer = NOT supported) | sink § |
| F-3 | New §1.10: OIDC discovery truthfulness table (verified facts only); actor `user:<uid>` via OIDC auth-code flow and `auth_flow=oidc` marked `[PROPOSED]` | §1.3, §1.10, snaplink § |
| F2 (carry-over) | Namespace note: event type `audit.event.*` (dot) ≠ scope `audit:event:*` (colon); ingest scope is `audit:event:write` (v2, scope-matrix-v2) | §1.1, sink § |
| count fix | v1 §2 per-service counts 46/12 were transposed (actual 45/13); v2: snaplink 45, sink 15 (13 + 2 new); total 121 unchanged | §2 |

### 0.2 Anchors added in v2

| Anchor | Evidence | Source |
|---|---|---|
| Access-token claim contract: `kid` (header), `iss`, `aud` (RFC 8707 resource), `scope`, `client_id`/`azp`, `tenant_id`, `roles` — minted by snaplink `buildAccessPayload` (all three issuers), consumed by sink `parseJWT` `[v2]` | **Proposed** (contract, pins S2) | `v2/revision-plan.md` §S2; `identity-protocol-review.md` F-2; `security-review.md` S2 |
| Sink dedup: `(source_service, tenant_id, event_id)` + tenant-scoped `idempotency_key`; same digest → 202 Duplicate, different digest → 409 Conflict | Verified | `security-review.md` §4; `distributed-systems-review.md` F4 |
| Producer outbox uniques as regeneration anchors: vault `UNIQUE (origin_kind, origin_id)`; aero-id `UNIQUE (tenant_id, source, event_id)` / `UNIQUE (fact_id)` | Verified | `distributed-systems-review.md` F4/F-07; `database-architect-review.md` §3 |
| Read-path self-audit (`audit.event.read` on query API) — **Missing in implementation** | Verified gap | `security-review.md` S9; `database-architect-review.md` F-06; QA T-12 |
| OIDC discovery metadata mis-advertises endpoints (`token_endpoint=/authenticate`, no `/authorize`, `/revoke` 404, unimplemented auth methods advertised) | Verified defect | `identity-protocol-review.md` F-3/F-4; QA T-2 |
| Control-plane mutation self-audit exists as internal `adminAction` (same `Store.Update`); registry-conformant self-audit events and read-path self-audit do not | Verified (partial) | `security-review.md` §4.8; distributed F13 |

## 1. Conventions

### 1.1 Event id naming
`<service-domain>.<verb>[.<scope>]` — lowercase, dot-separated, past-tense-free
verbs (`create/update/delete/grant/revoke/register/rotate/apply/release/start/
stop/send/view/export/…`). Scope qualifier for admin-path variants
(e.g., `file.delete` vs `file.delete.admin`).

> **Namespace note `[v2]`**: event type ids use dot separation
> (`audit.event.read`) and are **distinct from OAuth scope names** which
> use colon separation (`audit:event:write`). The ingest scope is
> `audit:event:write` (aligned with every implementation; v1's
> `audit:event:ingest` was stale — scope-matrix-v2 Flow 3). Do not
> cross-derive one from the other.

### 1.2 Common envelope (every event) `[v2: S2]`
`event_id, event_type, version(v1), service, occurred_at, tenant_id*,
actor {type, id, client_id, roles*}, trace_id, request_id, ip, user_agent,
nonce, prev_hash (ledger-assigned), event_hash (ledger-assigned)`.
Payload fields listed per event type below are **in addition to** the envelope.

**Required fields `[v2]` (S2 — claim-contract alignment):**
- `tenant_id` — **REQUIRED** on every event except `— (platform)`
  system-level events (which carry the explicit platform marker per §1.4).
  Producers resolve it from the access-token `tenant_id` claim (v2 claim
  contract, scope-matrix-v2 Flow 2); events without a resolvable tenant are
  rejected at ingest (422, fail-closed) unless the event type is declared
  platform-scope.
- `actor.roles` — **REQUIRED** array of role names, minted from the
  access-token `roles` claim for `user:`/`admin:`/`service:` actors;
  `system:<component>` actors carry `["system"]`. Empty roles on a
  non-system actor → rejected (403 at the edge, 422 at ingest).

### 1.3 Actor model `[v2]`
- `user:<uid>` — end user authenticated via snaplink. **Verified**: password
  login + session. **OIDC authorization-code flow NOT verified** (no
  `/authorize` exists; discovery metadata mis-advertises it — F-3), so
  OIDC-path user events are `[PROPOSED]` (see §1.10). `[v2]`
- `admin:<uid>@console` — user with admin role acting through
  snaplink-console.
- `service:<client_id>` — service-to-service via `client_credentials`
  (e.g., aero-im → aero-vault BlobStore, publisher → audit-governance).
- `system:<component>` — background job / scheduler / internal trigger;
  roles = `["system"]` `[v2]`.
- `delegated:<service>:<uid>` — service acting on behalf of a user
  (BFF pattern) `[PROPOSED]`.
- Every non-system actor carries `roles` from the token `roles` claim `[v2]`.

### 1.4 Tenant context `[v2]`
- `tenant_id (session)` — from snaplink user session (Verified for
  password/session flows).
- `tenant_id (token)` — from snaplink-issued access-token `tenant_id`
  claim; propagation mechanism **pinned by the v2 claim contract**
  (previously `[PROPOSED]`, open decision #5 — now resolved) `[v2]`.
- `tenant_id (event)` — carried in the ingested event envelope.
- `tenant_id (new)` — provisioned tenant, not yet in session.
- `— (platform)` — platform-level operation, no tenant; **the only
  `tenant_id` exemption** (S2) `[v2]`.

### 1.5 Retention classes (proposed defaults)
| Class | Default | Notes |
|---|---|---|
| **C1** Critical | ≥7y or local legal minimum | immutable, always legal-hold eligible |
| **C2** Standard | 1y | legal-hold eligible |
| **C3** Operational | 90d | legal-hold eligible only if explicitly attached to a hold |
| C0 (transient) | not ledgered | local logs only — high-volume events explicitly marked |

### 1.6 Legal-hold sensitivity
- **High** — content-bearing or reveals content access (downloads, exports,
  admin content views, message content, secrets rotation).
- **Medium** — identity/attribute/permission changes with privilege impact.
- **Low** — session/operational metadata.
- **None** — infrastructure/relay/checkpoint.

### 1.7 Content-in-payload policy `[PROPOSED]`
Ledger payloads are **metadata-only**: file/message content is referenced by
id, never embedded. Access events record that access occurred (with
content hash where available), not the content itself.

### 1.8 High-volume marker
`‡` = high-volume event type; ledgered as summaries/aggregates (L1 per the
L0/L1/L2 audit-tier model), details stay in local service logs.

### 1.9 Idempotency & dedup key contract `[v2]` (F4)

**Deterministic `event_id` derivation:**

```
event_id = SHA-256(source_service | tenant_id | event_type | resource_id | time_bucket)[:32]
```

| Input | Definition |
|---|---|
| `source_service` | producer identity (registered client_id) |
| `tenant_id` | envelope tenant (REQUIRED per §1.2) |
| `event_type` | registry id (e.g., `file.delete.admin`) |
| `resource_id` | stable business anchor of the occurrence: `file_id\|version`, `account_id`, `message_id\|edit_seq`, `share_id`, `room_id\|member_uid`, `session_id` (auth), `request_id` (admin/console ops) |
| `time_bucket` | occurrence window, per-event-type granularity (default: minute) |

**Distinct-occurrence rule**: when two distinct occurrences of the same
`(source_service, tenant_id, event_type, resource_id)` can fall into one
`time_bucket` (e.g., two downloads of the same file), the bucket MUST be
extended with a per-occurrence component (`request_id` / occurrence
counter). Bucket granularity is chosen per event type so distinct
occurrences never collide.

**Dedup keys (explicit):**

| Layer | Key | Semantics |
|---|---|---|
| Producer outbox (common) | `UNIQUE (service, event_id)` | one row per occurrence; retries reuse the row |
| aero-id ledger / publications | `UNIQUE (tenant_id, source, event_id)` / `UNIQUE (fact_id)` | per-occurrence, in-tx dup check |
| aero-vault outbox | `UNIQUE (origin_kind, origin_id)` | **regeneration anchor** |
| Sink ledger | `(source_service, tenant_id, event_id)` + tenant-scoped `idempotency_key` | same digest → 202 Duplicate; same key + different digest → 409 Conflict |

**Rules:**
1. Retries reuse the same `event_id` — never regenerated per attempt.
2. **Regeneration** (gap-reconcile, replay tool, row-clear reconstruction)
   MUST reuse the original `event_id`; minting a new ID for the same
   occurrence is a contract violation (F4). Vault `(origin_kind, origin_id)`
   and aero-id `(tenant_id, source, event_id)` are the anchors.
3. **Anti-squatting**: `time_bucket` (occurrence time) enters the hash and
   is not attacker-predictable; the sink additionally enforces
   claim-then-verify — same `event_id` with different content digest → 409
   (implemented, Verified).
4. **Transition**: while producers mint random UUIDs, sink dedup covers only
   same-row retries (vacuous on regeneration); deterministic IDs are a
   P2 dual-write cutover prerequisite. Random-UUID semantics are
   retry-dedup-only until then. `[v2]`

### 1.10 OIDC discovery dependency `[v2]` (F-3)

Registry rows and actor semantics that depend on snaplink OIDC discovery are
constrained by **verified discovery truthfulness** (identity-protocol-review
F-3/F-4/F-8; QA T-2):

| Metadata fact | Status | Evidence |
|---|---|---|
| `client_credentials` token issuance at `/token` | Verified | protocol F-2/F-4 |
| JWKS at `/.well-known/jwks.json` (well-formed keys) — but signer header lacks `kid` | Verified (partial) | protocol F-2 |
| Advertised `token_endpoint=/authenticate` (real RFC 6749 endpoint is `/token`) | **False** | protocol F-3 |
| `authorization_endpoint=/login` + `response_types: [code]` (no `/authorize` route exists) | **False** | protocol F-3 |
| `revocation_endpoint=/revoke` (route 404s) | **False** | protocol F-3 |
| `userinfo_endpoint=/validate` (non-standard session check) | **False** | protocol F-3 |
| `client_secret_basic` / `private_key_jwt` advertised, unimplemented | **False** | protocol F-4 |
| `device_authorization_endpoint` absent (device grant advertised) | **False** | protocol F-3/F-8 |
| issuer derived from `Host` header (no allowlist) | **False** | protocol F-3 |

**Consequences:**
- Actor `user:<uid>` via OIDC authorization-code flow, and any
  `auth_flow=oidc` / SSO event, are `[PROPOSED]` until discovery metadata is
  fixed (snaplink `routes.go` + `TestOIDCDiscovery` rewrite — conformance
  sweep T-2). `[v2]`
- `tenant_id (session)` remains Verified for password/session flows only.

---

## snaplink (OAuth2/OIDC SSO + RBAC + policies — trust root)

> Ingestion status: local audit-log concept only; governance connector
> `[PROPOSED]` (P0-2 gap). All events below are `[PROPOSED]` as ledger events.
> **Discovery caveat `[v2]` (F-3)**: rows whose trigger depends on the OIDC
> authorization-code flow (`auth_flow=oidc`, SSO provider login) are
> `[PROPOSED]` — discovery metadata currently mis-advertises the surface
> (see §1.10); password/session flows are Verified.

### auth

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `auth.login.success` | User completes password login (OIDC/SSO login paths `[PROPOSED]` — §1.10) `[v2]` | `user:<uid>` | tenant_id (session) | auth_flow, idp, client_id, session_id, mfa_used, auth_time | C2 | Low |
| `auth.login.failure` | Failed authentication attempt | attempted `user:<uid>` | tenant_id (session, if resolvable) | auth_flow, idp, client_id, failure_reason (bad_credentials/locked/mfa_failed/disabled), attempts_since_success | C2 | Low |
| `auth.mfa.enroll` | User enrolls an MFA factor | `user:<uid>` | tenant_id (session) | factor_type, factor_id, method (self/admin) | C2 | Medium |
| `auth.mfa.reset` | Admin resets a user's MFA | `admin:<uid>@console` | tenant_id (session) | target_uid, reason | C1 | Medium |
| `auth.mfa.challenge.failure` | MFA challenge rejected | `user:<uid>` | tenant_id (session) | factor_type, session_id | C2 | Low |
| `auth.logout` | User ends session | `user:<uid>` | tenant_id (session) | session_id, idp, client_id | C3 | Low |
| `auth.session.revoke` | Session revoked (self or admin) | `user:<uid>` or `admin:<uid>@console` | tenant_id (session) | target_session_id, initiator, reason | C1 | Medium |
| `auth.token.issue` ‡ | Access/refresh/id token issued | `user:<uid>` or `service:<client_id>` | tenant_id (token) | token_type, client_id, scope, grant_type, session_id | C3 | Low |
| `auth.token.revoke` | Token revoked (logout/rotation/compromise) | `user:<uid>` or `system:<snaplink>` | tenant_id (token) | token_id, token_type, reason, client_id | C1 | Medium |
| `auth.password.change` | User changes own password | `user:<uid>` | tenant_id (session) | method, session_id (no secret material) | C1 | High |
| `auth.password.reset` | Password reset (self-service or admin) | `user:<uid>` or `admin:<uid>@console` | tenant_id (session) | method (email/sms/admin), target_uid | C1 | High |
| `auth.device.register` | New device registered to user | `user:<uid>` | tenant_id (session) | device_id, device_type, platform | C2 | Medium |
| `auth.device.trust` | Device marked trusted | `user:<uid>` | tenant_id (session) | device_id, policy_id | C2 | Medium |

### policy

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `policy.role.create` | New RBAC role defined | `admin:<uid>@console` | tenant_id (session) | role_id, name, description | C1 | Medium |
| `policy.role.update` | Role definition modified | `admin:<uid>@console` | tenant_id (session) | role_id, changed_fields | C1 | Medium |
| `policy.role.delete` | Role removed | `admin:<uid>@console` | tenant_id (session) | role_id, affected_members_count, reassignment_target | C1 | High |
| `policy.role.grant` | Role assigned to user/group | `admin:<uid>@console` or `system:<snaplink>` | tenant_id (session) | role_id, subject_type (user/group), subject_id | C1 | High |
| `policy.role.revoke` | Role removed from user/group | `admin:<uid>@console` or `system:<snaplink>` | tenant_id (session) | role_id, subject_type, subject_id | C1 | High |
| `policy.permission.update` | Permissions attached to role changed | `admin:<uid>@console` | tenant_id (session) | role_id, permissions_added, permissions_removed | C1 | High |
| `policy.scope.register` | New OAuth scope registered | `admin:<uid>@console` | — (platform) | scope_id, service, description | C1 | Medium |
| `policy.scope.update` | Scope definition/visibility changed | `admin:<uid>@console` | — (platform) | scope_id, changed_fields | C1 | Medium |
| `policy.group.create` | Group created | `admin:<uid>@console` | tenant_id (session) | group_id, name | C1 | Medium |
| `policy.group.update` | Group metadata changed | `admin:<uid>@console` | tenant_id (session) | group_id, changed_fields | C1 | Medium |
| `policy.group.delete` | Group removed | `admin:<uid>@console` | tenant_id (session) | group_id, member_count | C1 | Medium |
| `policy.group.member.add` | Member added to group | `admin:<uid>@console` or `system:<snaplink>` | tenant_id (session) | group_id, member_uid, source | C1 | Medium |
| `policy.group.member.remove` | Member removed from group | `admin:<uid>@console` or `system:<snaplink>` | tenant_id (session) | group_id, member_uid, source | C1 | Medium |
| `policy.condition.update` | Auth/session policy rules changed (IP allowlist, session duration, geo) | `admin:<uid>@console` | tenant_id (session) | policy_id, rule_type, changed_fields, dry_run | C1 | High |

### client (OAuth applications / service identities)

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `client.register` | OAuth client (app/service) registered | `admin:<uid>@console` | tenant_id (session) | client_id, client_type (public/confidential), redirect_uris, owner | C1 | Medium |
| `client.update` | Client configuration changed | `admin:<uid>@console` | tenant_id (session) | client_id, changed_fields | C1 | Medium |
| `client.delete` | Client removed | `admin:<uid>@console` | tenant_id (session) | client_id, redirect_uris, deactivation_mode | C1 | High |
| `client.secret.rotate` | Client secret rotated | `admin:<uid>@console` or `service:<client_id>` | tenant_id (token) | client_id, rotation_reason | C1 | High |
| `client.scope.update` | Client's allowed scopes changed | `admin:<uid>@console` | tenant_id (session) | client_id, scopes_added, scopes_removed | C1 | High |
| `client.grant.update` | Authorization grant types changed | `admin:<uid>@console` | tenant_id (session) | client_id, grants_added, grants_removed | C1 | High |
| `federation.trust.update` `[PROPOSED]` | External IdP trust config (SAML/OIDC) changed | `admin:<uid>@console` | tenant_id (session) | provider_id, protocol, cert_fingerprint, changed_fields | C1 | High |

### tenant

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `tenant.create` | Tenant provisioned | `admin:<uid>@console` | tenant_id (new) | tenant_id, name, plan, admin_uid | C1 | Medium |
| `tenant.update` | Tenant settings changed | `admin:<uid>@console` | tenant_id (session) | changed_fields | C1 | Medium |
| `tenant.suspend` | Tenant suspended | `admin:<uid>@console` | tenant_id (session) | reason, scope (login/ingest) | C1 | High |
| `tenant.activate` | Tenant reactivated | `admin:<uid>@console` | tenant_id (session) | reason | C1 | Medium |
| `tenant.delete` | Tenant decommissioned | `admin:<uid>@console` | tenant_id (session) | deletion_mode (soft/hard), data_destruction | C1 | High |

### admin

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `admin.user.lock` | User account locked | `admin:<uid>@console` | tenant_id (session) | target_uid, reason, duration | C1 | Medium |
| `admin.user.unlock` | User account unlocked | `admin:<uid>@console` | tenant_id (session) | target_uid, reason | C1 | Medium |
| `admin.user.attribute.update` | Identity attributes corrected by admin | `admin:<uid>@console` | tenant_id (session) | target_uid, changed_fields, reason | C1 | High |
| `admin.impersonate.start` | Admin starts impersonation session | `admin:<uid>@console` | tenant_id (session) | target_uid, reason, session_id | C1 | High |
| `admin.impersonate.stop` | Impersonation session ended | `admin:<uid>@console` | tenant_id (session) | target_uid, session_id, duration | C1 | High |
| `admin.tenant.config.update` | Tenant-level IdP configuration changed | `admin:<uid>@console` | tenant_id (session) | changed_fields, reason | C1 | Medium |

---

## snaplink-console (Flutter admin console — control plane)

> **Ingestion status `[v2]` (F-06)**: **local-only today (Verified)** —
> `lib/services/audit_log_service.dart` is a browser `localStorage`
> 1000-ring recording local admin ops; it reads **no** audit API. The v1
> "consumer only (Verified)" claim was inaccurate. The consumer role
> (reading the ledger via the sink read API) is the **target state**.
> **Read-path events moved to the sink `[v2]`**: v1's
> `console.audit.view` / `console.export.view` are **removed** — read
> auditing is generated by the ledger itself (`audit.event.read` /
> `audit.event.export`, sink section), so direct API calls cannot bypass
> it (F-06/S9). Console-native facts below are `[PROPOSED]`.

### admin

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `console.login.success` | Admin signs in to console | `admin:<uid>@console` | tenant_id (session) | session_id, auth_flow | C2 | Low |
| `console.login.failure` | Failed console sign-in | attempted admin | tenant_id (session, if resolvable) | failure_reason | C2 | Low |
| `console.session.revoke` | Console session revoked | `admin:<uid>@console` | tenant_id (session) | session_id, initiator | C1 | Medium |
| `console.capabilities.refresh` | Runtime admin endpoint list refreshed (`/api/v1/admin/endpoints`) | `system:<console>` | — (platform) | endpoint_count, source | C3 | None |
| `console.config.update` | Console settings changed | `admin:<uid>@console` | tenant_id (session) | changed_fields | C3 | Low |

---

## snaplink-audit-governance (audit ledger — unique audit source of truth)

> Self-audit: ledger/governance operations are themselves events in the
> ledger. Schema registry per ADR-0002 (design-time). All rows
> `[PROPOSED]` pending ADR alignment.
> **Self-audit writer `[v2]` (F13, normative P0)**: control-plane
> mutations already record an internal `adminAction` atomically with the
> store update (Verified); the registry-conformant self-audit events
> below (`audit.legalhold.*`, `audit.export.*`, `audit.retention.*`,
> `audit.ledger.checkpoint`, `audit.integrity.*`) are written **by the
> sink itself in the same store update as the operation** — append
> failure aborts the operation (fail-closed). Read-path self-audit
> (`audit.event.read` / `audit.event.export`, new in v2) is part of the
> same P0.
> **Supported ingest topology `[v2]`**: HTTP ingest API + self-audit
> writer. The sink's outbox relay / Kafka consumer are **unwired** and
> are NOT a supported ingest path in this contract.

### ledger

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `audit.event.ingest` ‡ | Event appended to ledger via relay/API (scope `audit:event:write` — scope-matrix-v2 Flow 3) `[v2]` | `service:<client_id>` (publisher) | tenant_id (event) | source_event_type, source_service, event_hash, prev_hash, ledger_seq | C3 | None |
| `audit.event.read` `[v2]` | **Read-path self-audit**: ledger queried (`QueryEvents`/`getEvent`) — appended by the sink in the same store update as the served query (F-06/S9; T-12) | caller: `admin:<uid>@console` or `service:<client_id>` | caller's tenant (token) | query_filter (service/category/time range), result_count, request_id | C1 | High |
| `audit.event.export` `[v2]` | **Export read self-audit**: export artifact downloaded/read (`downloadExport`) — appended by the sink | caller: `admin:<uid>@console` or `service:<client_id>` | caller's tenant (token) | export_id, scope (time range/services/event types), format, destination | C1 | High |
| `audit.ledger.checkpoint` | Hash-chain checkpoint sealed (written by sink, same-tx) `[v2]` | `system:<audit-governance>` | — (platform) | checkpoint_id, start_seq, end_seq, root_hash, signer_key_id | C3 | None |
| `audit.integrity.verify.run` | Scheduled/manual integrity verification (written by sink) `[v2]` | `system:<audit-governance>` or `admin:<uid>@console` | tenant_id (session, if manual) | range (start/end seq), result, mismatches | C1 | None |
| `audit.integrity.verify.failure` | Verification found tamper/anomaly (written by sink) `[v2]` | `system:<audit-governance>` | — (platform) | range, mismatch_details, event_ids | C1 | High |
| `audit.compaction.run` `[PROPOSED]` | Compaction/anonymization applied (GDPR erasure vs immutability — needs ADR) | `system:<audit-governance>` | tenant_id (event) | policy_id, affected_event_ids, fields_anonymized | C1 | High |

### governance

> All rows in this table are written by the **sink's self-audit writer in
> the same store update as the operation** — append failure aborts the
> operation (fail-closed) `[v2]` (F13).

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `audit.legalhold.apply` | Legal hold placed | `admin:<uid>@console` | tenant_id (session) | hold_id, subject_scope (tenant/user/service/case), reason, requested_by, expire_at | C1 | High |
| `audit.legalhold.release` | Legal hold lifted | `admin:<uid>@console` | tenant_id (session) | hold_id, authorized_by, reason | C1 | High |
| `audit.retention.policy.update` | Retention class policy changed | `admin:<uid>@console` | — (platform) | class, old_duration, new_duration, effective_at | C1 | High |
| `audit.export.request` | Export requested | `admin:<uid>@console` | tenant_id (session) | export_id, scope (time range/services/event types), format, purpose | C1 | High |
| `audit.export.completed` | Export produced (encrypted archive) | `system:<audit-governance>` | tenant_id (event) | export_id, artifact_ref, encryption, checksum, delivered_to | C1 | High |
| `audit.export.failed` | Export failed | `system:<audit-governance>` | tenant_id (event) | export_id, failure_reason | C1 | Medium |

### relay

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `audit.outbox.failure` | Publisher outbox relay failure detected (emitted only if sink reachable; local-log fallback otherwise) `[v2]` | `system:<service>` | tenant_id (event) | source_service, event_ids, retry_count, error | C3 | None |
| `audit.relay.dlq.replay` | Dead-letter events replayed (emitted only if sink reachable) `[v2]` | `system:<audit-governance>` | tenant_id (event) | dlq_batch_id, event_count, outcome | C3 | None |

---

## aero-id (account domain)

> Connector exists (Verified): `internal/connector/auditgovernance/`
> (publisher + oauth) is the reference Outbox pattern. Event taxonomy below
> is `[PROPOSED]`. Identity lives in snaplink (external SSO); aero-id holds
> business account data keyed by snaplink `uid`.

### account

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `account.create` | Business account provisioned | `user:<uid>` (self) or `system:<aero-id>` | tenant_id (token) | account_id, uid, plan, initial_attributes | C2 | Medium |
| `account.update` | Account attributes updated | `user:<uid>` | tenant_id (token) | account_id, changed_fields | C2 | Medium |
| `account.email.change` | Primary email changed | `user:<uid>` | tenant_id (token) | account_id, old_email_hash, new_email_hash, verification_method | C1 | High |
| `account.phone.change` | Phone changed | `user:<uid>` | tenant_id (token) | account_id, old_phone_hash, new_phone_hash | C1 | High |
| `account.consent.grant` | Consent recorded | `user:<uid>` | tenant_id (token) | account_id, consent_id, scope, version | C1 | High |
| `account.consent.revoke` | Consent withdrawn | `user:<uid>` | tenant_id (token) | account_id, consent_id | C1 | High |
| `account.ownership.transfer` `[PROPOSED]` | Account ownership transferred | `admin:<uid>@console` | tenant_id (token) | account_id, from_uid, to_uid, reason | C1 | High |
| `account.merge` `[PROPOSED]` | Two accounts merged | `admin:<uid>@console` | tenant_id (token) | primary_account_id, merged_account_id, conflict_resolution | C1 | High |
| `account.deactivate` | Account deactivated | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | account_id, initiator, reason | C1 | Medium |
| `account.reactivate` | Account reactivated | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | account_id, initiator | C1 | Medium |
| `account.delete` | Account deleted (self-service or admin) | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | account_id, deletion_mode, retention_applied | C1 | High |

### admin

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `admin.account.view` | Admin views account record | `admin:<uid>@console` | tenant_id (token) | account_id, fields_viewed, reason | C1 | High |
| `admin.account.attribute.update` | Admin corrects account attributes | `admin:<uid>@console` | tenant_id (token) | account_id, changed_fields, reason | C1 | High |
| `admin.account.delete` | Admin deletes account | `admin:<uid>@console` | tenant_id (token) | account_id, reason, mode | C1 | High |

---

## aero-vault (file platform — REST/S3/WebDAV/MCP + RAG)

> Connector exists (Verified): `internal/auditgovernance/token.go`
> (Snaplink browser-flow auth). Event taxonomy below `[PROPOSED]`.
> `file.delete.admin` name verified from `decisions/03-capabilities.md`.
> Fact IDs MUST follow §1.9 deterministic derivation (currently
> `uuid.NewString()` — v2 requires the change before P2 cutover) `[v2]`.

### file

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `file.upload` | File created (incl. aero-im attachments via BlobStore) | `user:<uid>` or `service:<client_id>` | tenant_id (token) | file_id, blob_ref, size, mime, parent_folder, rag_indexed | C2 | Medium |
| `file.download` | File content retrieved | `user:<uid>` or `service:<client_id>` | tenant_id (token) | file_id, size, channel (rest/s3/webdav/mcp), purpose | C2 | High |
| `file.update` | File content overwritten/versioned | `user:<uid>` | tenant_id (token) | file_id, version, size, prev_version | C2 | Medium |
| `file.metadata.update` | Rename/move/tags | `user:<uid>` | tenant_id (token) | file_id, changed_fields | C3 | Low |
| `file.delete` | File deleted by owner | `user:<uid>` | tenant_id (token) | file_id, size, deletion_mode, retain_until | C1 | Medium |
| `file.delete.admin` | Admin deletes file on behalf of owner | `admin:<uid>@console` | tenant_id (token) | file_id, owner_uid, reason, notified_owner | C1 | High |
| `file.version.restore` `[PROPOSED]` | Earlier version restored (versioning unverified) | `user:<uid>` | tenant_id (token) | file_id, from_version, to_version | C2 | Medium |
| `file.retention.apply` `[PROPOSED]` | Retention/DLP policy applied to file | `system:<aero-vault>` | tenant_id (token) | file_id, policy_id, action | C1 | Medium |

### share / permission

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `file.share.create` | Share link or ACL grant created | `user:<uid>` | tenant_id (token) | file_id, share_id, access_type (link/ACL), scope, expires_at | C1 | Medium |
| `file.share.revoke` | Share revoked | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | file_id, share_id, initiator | C1 | Medium |
| `file.permission.update` | File ACL changed | `user:<uid>` | tenant_id (token) | file_id, subject, permissions_added, permissions_removed | C1 | High |
| `file.lock` `[PROPOSED]` | WebDAV lock acquired | `user:<uid>` | tenant_id (token) | file_id, lock_token, owner, timeout | C3 | Low |
| `file.unlock` `[PROPOSED]` | WebDAV lock released | `user:<uid>` | tenant_id (token) | file_id, lock_token, force | C3 | Low |

### rag

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `file.rag.index` | File indexed into RAG store | `system:<aero-vault>` | tenant_id (token) | file_id, index_ref, chunk_count | C2 | Low |
| `file.rag.reindex` | Full/partial reindex | `admin:<uid>@console` or `system:<aero-vault>` | tenant_id (token) | scope, files_affected, reason | C2 | Medium |
| `file.rag.query` `[PROPOSED]` | RAG query executed (volume/privacy decision needed) | `user:<uid>` or `service:<client_id>` | tenant_id (token) | query_hash, result_file_ids, model | C2 | High |

### admin

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `admin.file.view` | Admin opens file content | `admin:<uid>@console` | tenant_id (token) | file_id, owner_uid, reason | C1 | High |
| `admin.file.list` | Admin browses file metadata | `admin:<uid>@console` | tenant_id (token) | query_filter, result_count | C2 | Medium |
| `admin.file.export` | Admin bulk-exports files | `admin:<uid>@console` | tenant_id (token) | export_id, scope, format, reason | C1 | High |
| `admin.file.policy.apply` `[PROPOSED]` | Admin applies governance policy to files | `admin:<uid>@console` | tenant_id (token) | policy_id, target_scope, action | C1 | Medium |

---

## aero-im (messaging / live / notifications)

> Ingestion status: no audit-governance connector found (Verified gap, P0-2);
> content governance logic exists locally. Attachments go through aero-vault
> BlobStore (Verified). All rows `[PROPOSED]`.

### message

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `message.send` ‡ | Message delivered to room | `user:<uid>` | tenant_id (token) | message_id, room_id, content_present (bool), attachment_ref | C3 | High |
| `message.edit` | Message edited | `user:<uid>` | tenant_id (token) | message_id, room_id, edit_count | C2 | Medium |
| `message.delete` | Message retracted | `user:<uid>` | tenant_id (token) | message_id, room_id, mode (self/all) | C2 | Medium |

### room

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `room.create` | Conversation created | `user:<uid>` | tenant_id (token) | room_id, room_type (dm/group/channel), member_count | C2 | Low |
| `room.update` | Room settings changed | `user:<uid>` | tenant_id (token) | room_id, changed_fields | C3 | Low |
| `room.delete` | Room deleted/archived | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | room_id, initiator, mode | C1 | Medium |
| `room.member.add` | Member joined/added | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | room_id, member_uid, invited_by | C1 | Medium |
| `room.member.remove` | Member removed/left | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | room_id, member_uid, initiator, reason | C1 | Medium |
| `room.member.role.update` | Member role changed (owner/admin/moderator) | `user:<uid>` | tenant_id (token) | room_id, member_uid, from_role, to_role | C1 | High |
| `room.permission.update` | Room permission profile changed | `admin:<uid>@console` | tenant_id (token) | room_id, permissions_changed | C1 | High |

### live

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `live.stream.start` `[PROPOSED]` | Live stream started | `user:<uid>` | tenant_id (token) | stream_id, room_id | C2 | Low |
| `live.stream.stop` `[PROPOSED]` | Live stream ended | `user:<uid>` or `system:<aero-im>` | tenant_id (token) | stream_id, duration | C2 | Low |
| `live.recording.start` `[PROPOSED]` | Recording started | `user:<uid>` or `admin:<uid>@console` | tenant_id (token) | stream_id, recording_id, storage_ref | C1 | High |
| `live.recording.stop` `[PROPOSED]` | Recording ended | `user:<uid>` or `system:<aero-im>` | tenant_id (token) | recording_id, duration, storage_ref | C1 | High |

### notification

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `notification.channel.update` | Notification channel config changed (email/sms/push/webhook) | `admin:<uid>@console` | tenant_id (token) | channel, changed_fields | C1 | Medium |
| `notification.template.update` `[PROPOSED]` | Notification template changed | `admin:<uid>@console` | tenant_id (token) | template_id, locale, changed_fields | C3 | Low |
| `notification.send` ‡ | Notification dispatched | `system:<aero-im>` | tenant_id (token) | recipient_uid, channel, template_id | C3 | Low |

### admin

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `admin.message.delete` | Moderation delete of message | `admin:<uid>@console` | tenant_id (token) | message_id, room_id, author_uid, reason, notified | C1 | High |
| `admin.room.archive` | Admin archives room | `admin:<uid>@console` | tenant_id (token) | room_id, reason | C1 | Medium |
| `admin.room.member.remove` | Admin removes member | `admin:<uid>@console` | tenant_id (token) | room_id, member_uid, reason | C1 | Medium |
| `admin.content.flag` | Content flagged for moderation | `system:<aero-im>` or `user:<uid>` | tenant_id (token) | flag_id, content_ref, reason | C2 | High |
| `admin.moderation.action` `[PROPOSED]` | Moderation decision applied | `admin:<uid>@console` | tenant_id (token) | flag_id, decision, action | C1 | High |

---

## 2. Coverage checklist (gate)

Per `SCENARIO_ANALYSIS.md` P0-2: new write/admin operation without an event
type = review FAIL. Per-service self-check table `[PROPOSED]`:

| Service | Ledgered event types | High-volume (‡, L1) | Admin-path events | Connector status |
|---|---|---|---|---|
| snaplink | 45 | 1 | 6 | Gap (local audit only) |
| snaplink-console | 5 `[v2]` | 0 | 5 `[v2]` | **local-only today (Verified); consumer = target state** `[v2]` |
| snaplink-audit-governance | 15 `[v2]` | 1 | — | Self (source of truth); **self-audit writer + read-path self-audit = sink P0** `[v2]` |
| aero-id | 14 | 0 | 3 | Connector exists (Verified) |
| aero-vault | 20 | 0 | 4 | Connector exists (Verified) |
| aero-im | 22 | 2 | 5 | Gap (no connector) |
| **Total** | **121** | **4** | **23** `[v2]` | — |

Count notes `[v2]`: total unchanged at 121 (v2: −2 console read rows,
+2 sink read/self-audit rows). v1 per-service counts for snaplink
(46→45) and sink (12→13) were transposed and are corrected here.

## 3. Open questions / decisions pending

1. **IdP ledgering** (P0-2): snaplink has no governance connector — confirm
   the full list of IdP events to relay (this draft assumes all `auth.*`,
   `policy.*`, `client.*`, `tenant.*`, `admin.*`).
2. **Content-in-payload policy**: confirm metadata-only payloads; decide
   whether message/file content hashes are always included.
3. **`file.rag.query` / `message.send` volume**: confirm L1 aggregation
   threshold and whether summaries are sufficient for compliance.
4. **`audit.compaction.run` vs immutability**: needs an ADR before
   implementation (GDPR erasure path).
5. **Tenant propagation** — **RESOLVED in v2**: pinned to the access-token
   `tenant_id` claim (v2 claim contract, scope-matrix-v2 Flow 2); the
   token-vs-header alternative is closed. `[v2]`
6. **Console audit page data source** — **RESOLVED in v2**: sink-side
   self-audit (`audit.event.read` / `audit.event.export`); console displays
   server-side records only. Sink read self-audit is a P0 deliverable
   (F-06). `[v2]`
7. **Schema registry**: align every row above with ADR-0002 event schema
   registry before code-level implementation.
8. **`time_bucket` granularity** (new, F4): per-event-type bucket
   granularity must be calibrated with the L1 aggregation thresholds
   (open question #3) so distinct occurrences never collide; default is
   minute + per-occurrence extension for same-bucket duplicates. `[v2]`
9. **`roles` claim content** (new, S2): exact role names minted by snaplink
   for service clients (mapping to sink permissions, e.g.,
   `event-writer` → `audit:event:write`) pending the IdP scope registry
   (P0-3). `[v2]`
