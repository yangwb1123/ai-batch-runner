# Agent Hub lifecycle facts in Audit Governance

`pbatch.hub_audit.AuditSink` adapts the Hub outbox's redacted facts to the
existing `POST /api/v1/events` contract. The adapter does not add routes to
Audit Governance and does not send prompts, command arguments, output, or
credentials as event data.

The sink accepts only the fact fields `tenant_id`, `event_id`, `cursor`,
`kind`, `created_at`, `instance_id`, `session_id`, `turn_id`, `task_id`,
`actor_subject`, `status`, and optional `trigger_subject`. Unknown fields are
rejected, so adding an envelope field cannot accidentally export body content.
Empty optional resource IDs are omitted from the Audit payload. For a real
cancel actor, Audit `actor.id` is `trigger_subject`; `actor_subject` and
`trigger_subject` both remain in the metadata payload so the initiator and
operator are distinguishable.

The prepared request is a complete immutable Audit envelope. Its event ID and
idempotency key are derived from the tenant, configured source ID, and Hub
event ID. Repeated delivery therefore sends the same request; a changed fact
with the same Hub event ID conflicts at Audit rather than silently replacing
the first record. The outbox should persist the prepared envelope before
delivery and retry it unchanged.

Delivery uses `POST /api/v1/events?wait_for=ledgered`. HTTP 202 by itself is
not treated as proof of ledgering: a receipt with status `accepted` raises the
retryable `ledger_pending` integration error. A completed delivery requires a
receipt matching the event ID and tenant, status `ledgered`, `indexed`, or
`archived`, a valid `ledgered_at`, a SHA-256 hash, a stream ID, and a positive
sequence. The returned receipt is a bounded projection of those verified
fields; it never claims archive completion unless Audit returned status
`archived`. Redirects, proxy use, cleartext non-loopback URLs, unbounded
responses, and unsafe token files are rejected by the shared JSON transport.

## Configuration and identity

The settings format is shown in [`examples/agent-hub-audit.yaml`](../../examples/agent-hub-audit.yaml).
`base_url` is the Audit API origin, `tenant_id` must match the Hub fact, and
`token_file` holds a private server-side bearer token. The writer identity
needs the `audit:event:write` permission for that tenant. The adapter reads the
token file for each request so token rotation does not require changing the
outbox fingerprint. Credential contents and local credential paths are not
part of the fingerprint; the target URL, tenant, source, event schema, data
classification, retention class, and TLS mode are.

The source and schema registration bodies are local templates only:

* [`examples/agent-hub-audit-source.json`](../../examples/agent-hub-audit-source.json)
  is sent to `POST /api/v1/sources` by a policy writer. Replace
  `<SIGNED_AUDIT_WRITER_CLIENT_ID>` with the exact signed OAuth `client_id` of
  the service token used by the Hub adapter. Do not rely on a display name or
  JWT subject as a substitute.
* [`examples/agent-hub-audit-schema.json`](../../examples/agent-hub-audit-schema.json)
  is sent to `POST /api/v1/schemas` by a policy writer. The event type,
  schema ID, version, and classification must match the adapter settings.

Both writes require `audit:policy:write`; a tenant-bound policy token must be
bound to the same tenant. Replace `<TENANT_ID>` with that tenant. These files
are examples; no request is sent or source/schema created by installing the
adapter. The schema's required and allowed fields cover only the fixed
metadata projection above.

## Use

The outbox integration creates one `AuditSink` per configured target, calls
`prepare(fact)` before persisting the immutable request, then calls
`deliver(saved_payload, delivery_id)` on its retry worker. `delivery_id` is
the outbox's own stable delivery identifier; Audit idempotency is bound to the
persisted envelope. `IntegrationError.retryable` distinguishes retryable
transport/ledger-pending outcomes from permanent contract and receipt errors.
The shared transport bounds each request to 64 KiB, bounds responses, rejects
redirects and proxies, and permits HTTP only to literal loopback addresses.

This adapter sends lifecycle facts to the configured Audit deployment. It
does not distribute Vault snapshots or task inputs, schedule devices, or
forward task execution data.
