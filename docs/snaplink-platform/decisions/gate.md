# Gate — Audit Contract (Registry + Scope Matrix + Ingestion Design)

> Date: 2026-08-06 · Gatekeeper verdict for the three contract drafts:
> `audit-event-registry-draft.md`, `scope-matrix-draft.md`,
> `audit-ingestion-design-draft.md`.
> Evidence: five adversarial reviews (protocol_expert, distributed_engineer,
> database_architect, qa_lead, sre_engineer, security_engineer), all
> Verified against executable code; test suites executed this revision
> (sink 157 pass, aero-id 926, aero-vault 1519, snaplink 1558, aero-im 7
> pass/30 ignored).

## Check 1 — Every service has an ingestion path: FAIL

All six services have an ingestion **design**, but the design's factual
premises about the codebase are contradicted by verified evidence:

| Service | Design | Verified reality |
|---|---|---|
| snaplink | outbox + relay (Proposed) | No connector wired in `cmd/sso-server`; L0 audit is a **volatile memory ring** by default (stock config), hash-chain off — backfill premise vacuous |
| snaplink-console | "consumer only (Verified)" | **Inaccurate** — `audit_log_service.dart` is a client-side `localStorage` 1000-ring recording local ops; **reads no audit API**. Edge-generated read self-audit (`audit.event.read`) is **Missing** in sink `QueryEvents` — the "cannot be bypassed" guarantee is unimplemented |
| audit-governance | self-audit writer (its own P0) | **Does not exist** — governance ops (legal hold/export/retention/checkpoint) append nothing to their own ledger; sink outbox relay/Kafka consumer unwired |
| aero-id | fastest rollout, pattern proven | Connector exists (Verified) but **flag-off by default** (`enabled: false`); 8 post-commit `_ = Record(...)` discard sites = silent loss window; delivers every event twice; no token invalidation on 401 |
| aero-vault | §1.2/§1.4 relay semantics | Relay built but **flag-off default** (`AUDIT_GOVERNANCE_ENABLED=false`); **no dead state** — poison rows retry forever and permanently red-flag `Ready()` → `/readyz` 503 → entire file platform down (restart loop); random-UUID fact IDs |
| aero-im | new Rust connector | No connector (Verified gap) — as designed |

**No stock binary publishes to the sink today** (database_architect F-03,
Verified). The "audit never blocks business" guarantee is violated by
design: vault couples relay backlog (`maxLag=900s`) and poison rows into
core readiness (SRE H1).

## Check 2 — Scope matrix follows least privilege: PARTIAL (shape) / FAIL (operability)

The matrix's *shape* is least-privilege correct: per-client binding, deny
lists, no wildcards, one-domain-per-client, no user scopes on service
tokens, no ledger read-back by producers.

**But the operative scope name is wrong**: matrix/registry provision
`audit:event:ingest`; **every implementation enforces `audit:event:write`**
— sink `server.go` `require("audit:event:write")`, aero-id
`validOAuthBinding`, vault `RequiredScope` (distributed F2, protocol F-08,
database F-02, security, SRE §428, all consistent). Provisioning per the
matrix fails at rollout with 403s. A least-privilege matrix whose grants
cannot be provisioned is not a contract.

## Check 3 — Blocking review findings resolved or dismissed: FAIL

None of the Critical/High findings are resolved in the drafts, and none
can be dismissed with evidence — they are Verified against code by
independent reviewers:

| # | Finding | Evidence | Blocking? |
|---|---|---|---|
| S1/F-1 | **Critical**: sink dev-token auth (`AUDIT_ALLOW_DEV_AUTH` default **true**) grants `platform-admin` cross-tenant read/export/legal-hold/restore to any caller; only path exercised by any test/e2e | security-review §S1 (exploit executed), protocol F-1, QA T-1 | Yes — trust model unenforceable; contract describes properties that don't exist |
| S2/F-2 | **Critical**: IdP client_credentials JWT lacks `tenant_id`/`roles` claims → every real-token ingest/read/export/legal-hold call 403s; end-to-end pipeline works only on forged tokens | security-review §S2, protocol F-2, QA T-1 | Yes — the entire contract (registry → relay → ingest → ledger) is untestable end-to-end |
| F2/F-08/F-02 | Scope name contradiction `audit:event:ingest` vs `audit:event:write` (all code) | distributed §F2 | Yes — direct defect in scope-matrix |
| F3/H1 | Vault no dead state; poison row → file platform 503; sink outage >15min takes vault down — contradicts design §1.2/§1.4/§1.5 | distributed §F3, SRE §H1, QA T-3 | Yes — design guarantee vs implemented behavior |
| F4 | Vault random-UUID fact IDs — deterministic `event_id` design exists nowhere; sink dedup vacuous on regeneration; deterministic IDs also enable squatting (S3) | distributed §F4, security §S3 | Yes — idempotency contract unenforced |
| F5 | aero-id silent audit gaps (post-commit discarded errors) | distributed §F5, QA T-5 | Yes — violates "never drop" |
| F-01/F1/H5 | Sink = whole-snapshot single-writer store: O(ledger) ingest, no fsync (file), optimistic lock **no conflict retry**, relational schema dead code — P0 "sink readiness" unmet; no capacity path for registry high-volume classes | database F-01, distributed F1, SRE H5 | Yes — P0 premise of the migration plan |
| F-06/S9/F13 | Read self-audit missing; console "consumer" claim wrong; self-audit writer missing | database F-06, security S9, distributed F13 | Yes — console design element unimplemented |
| F-3/T-2 | Discovery metadata lies; conformance test regression-locks the wrong endpoints | protocol F-3, QA T-2 | Yes — breaks flows depending on discovery (vault browser flow, console OIDC) |
| F-03/H3/H2 | No stock binary publishes; no alerting exists; SLO/RTO/RPO all Draft and unvalidated | database F-03, SRE H2/H3 | Yes for rollout claims ("fastest rollout, pattern proven") |

## What unblocks (minimum)

1. Fix sink trust path: `AUDIT_ALLOW_DEV_AUTH` default **false** (S1) and
   make IdP access tokens carry `tenant_id` (+roles) so real tokens pass
   sink authz (S2); add cross-repo e2e test on the production path (T-1).
2. Align scope matrix to implemented `audit:event:write` (or implement
   `audit:event:ingest` as an alias) — one-line contract fix, then
   re-verify.
3. Vault: implement dead state (§1.4), decouple audit backlog from
   `/readyz` (degraded + alert at maxLag×0.5), fix fact-ID determinism.
4. Aero-id: move `Record` calls in-tx or alert on discard; enable
   connectors as explicit migration steps (F-03).
5. Sink: wire relational ledger (or fsync + conflict retry + bounded
   ingest), implement self-audit writer + read-path self-audit (P0).
6. Fix OIDC discovery metadata and its regression test (F-3/T-2).

## Assessment

The drafts are a sound **architecture proposal** — verified anchors held
structurally, connectors exist, delete-flow tx integration is real, and
the review set itself is high-value (20+ cross-corroborated findings).
But as **contracts to be promoted and provisioned**, they fail closed:
the operative scope grant cannot be provisioned, the production trust
path is broken end-to-end, the "never drop / never block" guarantees are
contradicted by implemented behavior (vault 503 coupling, aero-id silent
loss), and the design's own P0 (sink self-audit, durable ledger) does not
exist. Per repo gate semantics, unresolved Critical/High findings block
promotion.

VERDICT: FAIL - Scope name audit:event:ingest contradicts implemented audit:event:write; production trust path broken (dev-token auth default-on, IdP tokens lack tenant_id/roles → 403 everywhere); vault poison-event/no-dead-state + Ready() coupling downs the file platform; aero-id silent audit loss; sink self-audit/read self-audit/durable ledger missing; no stock binary publishes; OIDC discovery lies. Resolve S1/S2/F2/F3/F4/F5/F-01/F-06/F13/F-3 per gate report before contract promotion.
