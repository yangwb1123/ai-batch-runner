# Snaplink Platform Audit — Event Type Registry (Draft)

> **Status**: DRAFT — proposal for human approval before promotion to contract
> (per `MULTI_REPO_OPERATIONS.md`: audit event type list enters repo contract
> docs only after manual approval).
> **Evidence standard**: verified anchors are cited; everything else is
> `[PROPOSED]` and must be validated against each codebase before use.
> **Gate**: under the full-composition deployment, any new write/admin
> operation without a corresponding event type = review FAIL
> (per `SCENARIO_ANALYSIS.md` P0-2).

## 0. Provenance — verified anchors vs proposed

| Anchor | Evidence | Source |
|---|---|---|
| Event id naming convention `<domain>.<verb>[.<scope>]` (e.g., `file.delete.admin`) | Verified | `docs/snaplink-platform/decisions/03-capabilities.md` |
| aero-id has audit-governance connector (`internal/connector/auditgovernance/` publisher/oauth) | Verified | `SCENARIO_ANALYSIS.md` §0 |
| aero-vault has audit-governance integration (`internal/auditgovernance/token.go`) | Verified | `SCENARIO_ANALYSIS.md` §0 |
| snaplink-console has audit log service page (`lib/services/audit_log_service.dart`) | Verified | `SCENARIO_ANALYSIS.md` §0 |
| snaplink (IdP) has only a local audit-log concept; no governance connector found | Verified gap | `SCENARIO_ANALYSIS.md` §0 |
| aero-im has no audit-governance connector found | Verified gap | `SCENARIO_ANALYSIS.md` §0 |
| audit-governance: immutable ledger, hash chain, Legal Hold, export, integrity verification, Outbox relay; event schema registry per ADR-0002 (design-time, single-node reference impl) | Verified | `SCENARIO_ANALYSIS.md` §0, §1 |
| All concrete event types, payload fields, retention classes, legal-hold sensitivities | **Proposed** | this document |

## 1. Conventions

### 1.1 Event id naming
`<service-domain>.<verb>[.<scope>]` — lowercase, dot-separated, past-tense-free
verbs (`create/update/delete/grant/revoke/register/rotate/apply/release/start/
stop/send/view/export/…`). Scope qualifier for admin-path variants
(e.g., `file.delete` vs `file.delete.admin`).

### 1.2 Common envelope (every event)
`event_id, event_type, version(v1), service, occurred_at, tenant_id, actor
{type, id, client_id}, trace_id, request_id, ip, user_agent, nonce,
prev_hash (ledger-assigned), event_hash (ledger-assigned)`.
Payload fields listed per event type below are **in addition to** the envelope.

### 1.3 Actor model
- `user:<uid>` — end user authenticated via snaplink OIDC.
- `admin:<uid>@console` — user with admin role acting through snaplink-console.
- `service:<client_id>` — service-to-service via `client_credentials`
  (e.g., aero-im → aero-vault BlobStore, publisher → audit-governance).
- `system:<component>` — background job / scheduler / internal trigger.
- `delegated:<service>:<uid>` — service acting on behalf of a user
  (BFF pattern) `[PROPOSED]`.

### 1.4 Tenant context
- `tenant_id (session)` — from snaplink user session.
- `tenant_id (token)` — from snaplink-issued token claims (aero-* services;
  exact propagation mechanism `[PROPOSED]`).
- `tenant_id (event)` — carried in the ingested event envelope.
- `tenant_id (new)` — provisioned tenant, not yet in session.
- `— (platform)` — platform-level operation, no tenant.

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

---

## snaplink (OAuth2/OIDC SSO + RBAC + policies — trust root)

> Ingestion status: local audit-log concept only; governance connector
> `[PROPOSED]` (P0-2 gap). All events below are `[PROPOSED]` as ledger events.

### auth

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `auth.login.success` | User completes password/OIDC/SSO login | `user:<uid>` | tenant_id (session) | auth_flow, idp, client_id, session_id, mfa_used, auth_time | C2 | Low |
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

> Console is primarily the **surface** that triggers events in owning services;
> it emits its own events only for console-native concerns (sessions, views).
> `[PROPOSED]` in full — data source boundary of the audit page is unconfirmed
> (direct audit-governance API vs per-service audit endpoints, per
> `SCENARIO_ANALYSIS.md` gap #3).

### admin

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `console.login.success` | Admin signs in to console | `admin:<uid>@console` | tenant_id (session) | session_id, auth_flow | C2 | Low |
| `console.login.failure` | Failed console sign-in | attempted admin | tenant_id (session, if resolvable) | failure_reason | C2 | Low |
| `console.session.revoke` | Console session revoked | `admin:<uid>@console` | tenant_id (session) | session_id, initiator | C1 | Medium |
| `console.audit.view` | Audit timeline/ledger queried from console | `admin:<uid>@console` | tenant_id (session) | query_filter (service/category/time range), result_count | C1 | High |
| `console.export.view` | Compliance/audit export initiated from console | `admin:<uid>@console` | tenant_id (session) | export_scope, format, destination | C1 | High |
| `console.capabilities.refresh` | Runtime admin endpoint list refreshed (`/api/v1/admin/endpoints`) | `system:<console>` | — (platform) | endpoint_count, source | C3 | None |
| `console.config.update` | Console settings changed | `admin:<uid>@console` | tenant_id (session) | changed_fields | C3 | Low |

---

## snaplink-audit-governance (audit ledger — unique audit source of truth)

> Self-audit: ledger/governance operations are themselves events in the
> ledger. Schema registry per ADR-0002 (design-time). All rows `[PROPOSED]`
> pending ADR alignment.

### ledger

| Event type | Trigger operation | Actor | Tenant context | Payload fields (event-specific) | Retention | Legal hold |
|---|---|---|---|---|---|---|
| `audit.event.ingest` ‡ | Event appended to ledger via relay/API | `service:<client_id>` (publisher) | tenant_id (event) | source_event_type, source_service, event_hash, prev_hash, ledger_seq | C3 | None |
| `audit.ledger.checkpoint` | Hash-chain checkpoint sealed | `system:<audit-governance>` | — (platform) | checkpoint_id, start_seq, end_seq, root_hash, signer_key_id | C3 | None |
| `audit.integrity.verify.run` | Scheduled/manual integrity verification | `system:<audit-governance>` or `admin:<uid>@console` | tenant_id (session, if manual) | range (start/end seq), result, mismatches | C1 | None |
| `audit.integrity.verify.failure` | Verification found tamper/anomaly | `system:<audit-governance>` | — (platform) | range, mismatch_details, event_ids | C1 | High |
| `audit.compaction.run` `[PROPOSED]` | Compaction/anonymization applied (GDPR erasure vs immutability — needs ADR) | `system:<audit-governance>` | tenant_id (event) | policy_id, affected_event_ids, fields_anonymized | C1 | High |

### governance

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
| `audit.outbox.failure` | Publisher outbox relay failure detected | `system:<service>` | tenant_id (event) | source_service, event_ids, retry_count, error | C3 | None |
| `audit.relay.dlq.replay` | Dead-letter events replayed | `system:<audit-governance>` | tenant_id (event) | dlq_batch_id, event_count, outcome | C3 | None |

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
| snaplink | 46 | 1 | 6 | Gap (local audit only) |
| snaplink-console | 7 | 0 | 7 | Reads audit API (Verified) |
| snaplink-audit-governance | 12 | 1 | — | Self (source of truth) |
| aero-id | 14 | 0 | 3 | Connector exists (Verified) |
| aero-vault | 20 | 0 | 4 | Connector exists (Verified) |
| aero-im | 22 | 2 | 5 | Gap (no connector) |
| **Total** | **121** | **4** | **25** | |

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
5. **Tenant propagation**: verify how tenant_id reaches aero-* services
   (token claims vs request header) — affects `tenant_id (token)` column.
6. **Console audit page data source**: direct audit-governance API vs
   per-service endpoints (SCENARIO_ANALYSIS gap #3) — determines whether
   `console.audit.view` events are generated by console or by
   audit-governance itself.
7. **Schema registry**: align every row above with ADR-0002 event schema
   registry before code-level implementation.
