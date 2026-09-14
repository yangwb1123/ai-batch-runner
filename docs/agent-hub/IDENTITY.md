# Aero-ID membership gate for Agent Hub

The optional `identity` setting connects Hub identity grants to Aero-ID's
read-only membership projection. An empty list disables this additional gate.
Each configured tenant requires a private Aero-ID service token and one or more
explicit subject/client/account mappings:

```yaml
identity:
  - tenant_id: tenant-a
    base_url: https://aero-id.example
    token_file: /run/secrets/agent-hub-aero-id-token
    timeout: 5
    ca_file: ""
    cert_file: ""
    key_file: ""
    bindings:
      - subject: snaplink-pairwise-subject
        client_id: snaplink-console
        account_id: 00000000-0000-0000-0000-000000000001
```

The map key is exactly `(tenant_id, subject, client_id)`. Hub never derives an
Aero-ID account ID from a subject, UID, email, or tenant name. Service tokens
are tenant-specific, so an upstream response must echo the configured tenant
and mapped account ID as well as `eligible`; any mismatch fails with 503. A
configured tenant with missing or duplicate mappings is rejected at startup or
denied at check time. Missing Hub-to-Aero-ID availability has no stale-cache
fallback.

The adapter uses the bounded ecosystem JSON transport: TLS verification,
optional mTLS, no redirects or proxy inheritance, private token files, fixed
endpoint paths, bounded request/response sizes, and finite timeouts. An
`eligible:false` response denies Hub access with 403. Upstream HTTP errors
(including invalid service credentials), outages, or invalid responses return
503. Each authenticated user request and each Gateway-derived user actor must
be checked before reading or mutating Hub resources. Gateway machine authentication is
itself checked by its dedicated Hub grant; when it acts for a turn, the
original turn actor's exact tenant/subject/client mapping is checked again.

The Aero-ID projection is bounded-stale and cannot promise immediate source
revocation. Hub continues to enforce its explicit tenant, instance, and scope
grants independently of membership eligibility.

## Aero-ID service configuration

Enable the endpoint in Aero-ID with an exact machine-principal binding and a
fixed membership projection for each permitted tenant:

```yaml
agent_membership:
  bindings:
    - issuer: https://identity.example
      subject: agent-hub
      client_id: agent-hub
      tenants:
        - tenant_id: tenant-a
          source: snaplink
          scope_type: tenant
          scope_id: tenant-a
          max_age: 10m
```

These values are examples, not registered identities. The subject must match
the actual signed machine-token subject. Snaplink's `client_credentials` flow
sets both `sub` and `client_id` to the client ID, as reflected in this example.
The token file configured in Hub must contain a token accepted by Aero-ID's
existing strict Control Plane verifier, including its configured audience and
tenant/client claims, and the signed OAuth scope `aero-id.membership:check`.
Restrict the machine client's allowed scopes accordingly; register this scope
in Snaplink's global scope registry when that registry is enabled. The new
scope is an additional requirement of this endpoint, not a change to other
Aero-ID APIs.

The endpoint accepts only an account ID. Callers cannot select the source,
membership scope, freshness window, or tenant. Both positive and negative
eligibility results produce a minimal durable audit fact using Aero-ID's
existing ledger. An audit-write failure returns 503, so Hub cannot allow the
request on an unaudited result. The check does not provision accounts or alter
membership state. Aero-ID's server contract and verification commands live in
its `docs/contracts/agent-membership-check.md`.

## Snaplink compatibility

The checked Snaplink implementation emits the access-token `scope` as a
space-separated string, stamps `tenant_id` from the OAuth client's tenant, and
sets `aud` from requested `resource` values. A client with an empty tenant omits
`tenant_id`; a token request without a resource omits `aud`. Neither token is
sufficient for Aero-ID's strict verifier. Configure the machine client's
tenant and request Aero-ID's configured audience as the resource. The issuer
must match the signer's configured issuer exactly; it cannot be inferred from
the API hostname. These facts were checked in Snaplink's
`internal/handler/tokengrant/token_client_credentials.go`,
`infrastructure/defaultimpl/issue_payload.go`, and
`cmd/sso-server/serverbuildsign/build_signing_issuers.go`.

OIDC subject semantics belong to the configured issuer. `sub` is never assumed
to be an Aero-ID account ID; the mapping remains explicit across tenant and
client boundaries and remains correct if the issuer uses pairwise subjects.
Before deployment, verify the actual access-token issuer, audience, tenant and
client claims against Aero-ID's strict Control Plane verifier. The gate must
not add an alternate verifier or accept legacy incomplete tokens. Verify the
deployed source membership lifecycle contract before describing revocation
timing; an Aero-ID bounded projection is not, by itself, proof of immediate
source-side revocation.

## Adapter API

`IdentityGate(settings).check(principal)` gates Hub user principals and skips
the direct membership check for Gateway credentials. `check_actor(tenant,
subject, client_id)` is the explicit call for a Gateway-derived actor. Root Hub
service wiring must invoke the former before user routes and the latter before
delegated compute admission.
