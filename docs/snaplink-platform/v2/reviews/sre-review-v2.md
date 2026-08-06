# SRE Review — API Backend & External Frontend/Proxy Boundary (v2)

**Role**: SRE — can operators detect, withstand, and recover from failures?
**Date**: 2026-08-06 · **Reviewer**: sre (adversarial review, v2)
**Scope**: API backend (IdP `sso-server`, audit sink, aero-id/aero-vault/aero-im producers) and the external frontend/proxy boundary (snaplink-console SPA + nginx proxy + BFF wiring).
**Evidence standard**: per `prompts/README.md` — Verified / Partial / Missing / Proposed / Unknown labels with repo paths + symbols. No tests executed, no code modified this round; all claims from direct source reading and grep scans at current working-tree HEADs.

---

## 1. Service / dependency map and operational assumptions

### 1.1 Topology (verified wiring)

```
browser (Flutter SPA, snaplink-console)
   │  same-origin /app/* + /auth/* + /api/* + /token/* + /.well-known/*
   ▼
nginx proxy  [snaplink-console nginx.conf; k8s/base/deployment.yaml]
   │ SNAPLINK_UPSTREAM → http://sso-server.sv-sso.svc.cluster.local:8080   (k8s: TLS-verified billing/stripe adapters)
   ▼
IdP sso-server  [ACTIVE = /home/u1/workspace/demo/snaplink cmd/sso-server,
                 image snaplink/sso-server:latest; legacy twin = /home/u1/snaplink cmd/server]
   ├─ stores: identity/connections/password = memory|sqlite|postgres   (config.yaml default: memory)
   ├─ audit: remote audit client (circuit breaker) | stdlog stdout/file | NONE   (legacy logger.go)
   ├─ tracing: OTLP optional; metrics: Prometheus /metrics
   └─ registry: consul/etcd/redis optional (legacy wiring)

audit producers (outbox → relay):
   aero-id    → ledger + audit_publications same-tx, outbox relay → POST /api/v1/events  (in-tx; 8 post-commit `_ =` sites)
   aero-vault → audit_governance_outbox, relay (lease/backoff, no dead-letter, no metrics)
   aero-im    → local audit_events table (migration 0007); sink connector crate NOT built (B5-2 pending)

sink (snaplink-audit-governance):
   HTTP :8089 (/healthz /readyz /metrics /api/v1/*) + optional gRPC (AUDIT_GRPC_LISTEN, authenticated, undeclared)
   store: file snapshot (./data/state.json) or PG audit_state_snapshot  — single-writer
   archive: file WORM (fsync) or S3 Object Lock
   orphans: cmd/audit-outbox-relay, cmd/audit-kafka-consumer, cmd/audit-projector, internal/outbox (verify-compose only)
   deps: optional PG, S3/MinIO, Vault Transit (checkpoint signatures), OTLP, Prometheus scrape (verify compose)
```

### 1.2 Two IdP codebases — the defining operational hazard

- **Active trust anchor** = SDK repo `workspace/demo/snaplink` `cmd/sso-server`. Verified: console `k8s/base/deployment.yaml:40` upstream `sso-server.sv-sso.svc.cluster.local:8080`; console `docker-compose.yml` upstream `snaplink/sso-server:latest` (image built from SDK `Dockerfile:7`). SDK issuers **already stamp `kid`** (`infrastructure/defaultimpl/ed25519_types.go:12`, `ecdsa_jwt_issuer.go:439`, `rsa_jwt_issuer.go:436`) but `buildAccessPayload` (`issue_payload.go:26-43`) mints **no `tenant_id`, no `roles`**, and no scope registry exists.
- **Legacy twin** = `/home/u1/snaplink` (module `github.com/opensso/sso`): mints `{sub,aud,iat,exp,uid}` with header `{alg,typ}` — **no `kid`** (`token/jwt/generator.go:52-55`, `Generate`), discovery Host-derived with port bug, `/authenticate` advertised, `/revoke` 404, codified by `routes_test.go` (`TestOIDCDiscoveryEndpoint`).
- The v2 batch plan B4 rows **mix both trees** (`issue_payload.go` = SDK; `cmd/server/routes.go`/`sso.go:894-960` = legacy). Protocol review V2-5 and DB review F-13 both flag this; it is not just a codebase nit — it determines which binary an operator patches, which discovery an e2e test asserts, and which JWKS the sink trusts.

### 1.3 Operational assumptions (documented)

| Assumption | Status |
|---|---|
| SLO/error budget (sink) | Draft `docs/SLO_ALERTS.md`: ingest avail 99.99%, query 99.95%, p99<150ms accepted, ledgered p99<2s, RPO=0 same-region; cross-region RPO≤60s/RTO≤15min **explicitly unverified** |
| Single-writer sink | Verified implicit: file backend single process; PG CAS (`ErrSnapshotConflict` → 500); 2+ replicas → 500s (distributed review DS-01/03) |
| Quota per instance | Verified: `service.go:80-84` in-memory `quotas map[string]quotaWindow` — per-process, not shared |
| Audit never blocks business | **Violated today**: vault `/readyz` 503 on audit backlog (SRE-04) |
| IdP state survives restart | **Violated by default**: memory stores (SRE-03) |
| dev-token is test-only | **Violated today**: default true (SRE-01) |
| Alerting/runbooks exist for sink | Missing: no rules files, no Alertmanager, no runbook docs (only draft SLO_ALERTS.md) |

---

## 2. Readiness table

| Component | Signal | Dependency probed | Failure behavior (today) | Alert | Runbook |
|---|---|---|---|---|---|
| console pod | readiness/liveness/startup → `/` (nginx static) | **none upstream** (k8s/base/deployment.yaml:81-95) | pod Ready while IdP down; traffic 502s at proxy | none | none |
| nginx proxy | n/a (container health = `/`) | no upstream health check; no proxy timeouts tuned | default 60s proxy read timeout; no error_page fallback; no retry | none | none |
| IdP sso-server (SDK) | `/readyz` | SQLite peers Ping + DR aggregate (report-only default; `build_http.go:56-57`, `build_app_core.go:180`) | DR staleness report-only; gated only by `dr.gate_readiness` opt-in | none observed for DR stale | docs/dr-framework.md (SDK) |
| IdP legacy (`/home/u1/snaplink`) | `/ready` = session_store pinger + registry check (`routes.go:1013-1020`) | store (redis/PG) + registry | **audit backend, token store, permission svc, admin auth NOT probed**; memory store pinger nil → check is skipped silently | none | none |
| sink audit-api | `/readyz` (`httpapi/server.go:161-178`) | archive (file dir / S3 bucket) only; `Store==nil` check | **store backend (file/PG) NOT probed** — PG down → still 200 until write fails; archive down → 503 (correct) | `archive_unavailable` (draft SLO) | none |
| sink audit-api | `/healthz` | none | always 200 | n/a | n/a |
| aero-vault | `/readyz` (`cmd/server/http.go:46-67`) | repo Ping + storage Stat + **audit-governance runtime Ready** (`runtime.go:145-160`: backlog > maxLag 900s → error) | **audit relay backlog or poison row flips file platform 503** → restart-loop risk; read paths degrade to 503 | none (vault relay has 0 metrics — H6) | none |
| aero-id | gRPC health (flipped NOT_SERVING on shutdown, `app.go:95-99`) + warmup SSO probe | SSO endpoint probe at boot (breaker cold-start) | falls back to stale cache/breaker; graceful shutdown sequence wired | none observed | none |
| aero-im | n/a (no sink relay exists) | — | local `audit_events` only; no sink connectivity at all today | n/a | n/a |

**Summary**: readiness signals exist everywhere, but (a) the console boundary probes only static HTML, (b) the sink never probes its own store, (c) vault lets the audit side-channel gate platform readiness, and (d) **no alert rule is deployed anywhere** (only a draft doc and a scrape config). Operators cannot be paged on any of the failure modes below.

---

## 3. Findings (severity-ordered)

### SRE-01 — Critical — dev-token auth enabled by default in the sink
- **Evidence (Verified)**: `sink cmd/audit-api/main.go:45` — `boolEnv("AUDIT_ALLOW_DEV_AUTH", true)`; `deploy/docker-compose.verify.yml` sets `AUDIT_ALLOW_DEV_AUTH: "true"`; `internal/auth/auth.go:44-48` — `dev:` prefix bypasses JWT verification; `parseDevToken` grants `platform-admin` → all permissions (`permissionsForRoles`); `ValidateConfiguration` accepts dev-only trust (no fail-closed on "dev is the only trust source").
- **Impact**: a default deployment accepts unsigned `dev:platform:platform-admin` bearer tokens granting full read, export, legal-hold, policy-write, integrity-verify. Complete auth bypass on the compliance store. This is the S1 violation (also DS-01, V2-2) — **release blocker**.
- **Remediation**: default `false`; `ValidateConfiguration` rejects configs where dev-auth is the only trust source; CI manifest scan fails on `AUDIT_ALLOW_DEV_AUTH=true` in any production manifest (batch B1-1).
- **Recovery validation**: after fix, `curl -H "Authorization: Bearer dev:platform:platform-admin" /api/v1/events` → 401 on every route (T-1.1); boot with dev-only config exits non-zero.

### SRE-02 — Critical — Production trust anchor is not pinned; B4 batch rows point at two different IdP trees
- **Evidence (Verified)**: console k8s `deployment.yaml:40` → SDK sso-server; SDK mints `kid` (SRE above) but no `tenant_id`/`roles`/scope-registry; legacy tree mints no `kid`, wrong discovery, and its tests **codify the wrong contract** (`routes_test.go:41` `TestOIDCDiscoveryEndpoint` asserts `8080:0`). Batch plan row "snaplink (IdP) #1/#3/#4" mixes `issue_payload.go` (SDK) with `cmd/server/routes.go`+`sso.go:894-960` (legacy). Also the legacy `/token` handler (`routes.go:247-310`) does no scope validation (RFC 6749 §3.3) and `ClientCredentials` (`sso.go:894-960`) compares secrets non-constant-time and omits `iss`/`kid`/`tenant_id`/`roles`.
- **Impact**: an implementer or operator applying the batch plan patches the wrong binary; e2e trust-path tests assert against the wrong discovery; the sink's JWKS verifier (`verifier.go:167` requires `kid`) cannot validate legacy tokens — any deployment pointed at the legacy binary breaks G1/G5.
- **Remediation**: pin B4 rows to the SDK (`cmd/sso-server/*`, `infrastructure/defaultimpl/issue_payload.go`); add a CI guard that fails if legacy `cmd/server` routes are wired into the console upstream; decide and document the fate of `/home/u1/snaplink` (retire or explicitly dual-run).
- **Recovery validation**: `POST /token` (cc, `resource=audit-governance`) → assert header `kid` + claims `iss/scope/client_id/tenant_id/roles` (T-8a); sweep every advertised discovery endpoint with HEAD (T-2).

### SRE-03 — High — IdP default configuration ships memory stores: restarts lose the identity plane
- **Evidence (Verified)**: SDK `cmd/sso-server/config.yaml` — identity store `backend: memory` (line ~114), connections `backend: memory` (line ~87), password users `backend: "memory"` (line ~135); comments admit "memory loses them [passwords] on restart" and "GDPR consent records must survive restarts". Legacy `cmd/server/config.go` — `SSO_STORE` default `memory`; audit default `SSO_AUDIT_LOG=""` → **no audit log at all** (`cmd/server/logger.go:12-36` returns nil logger).
- **Impact**: any restart (deploy, crash, OOM, node drain) silently drops sessions, users, password changes, and consent; the compliance platform's own IdP emits no audit trail by default — the L0 audit source (login events) is absent in the default deployment.
- **Remediation**: production manifests must pin `sqlite`/`postgres` (or a config validation that refuses `memory` outside dev); wire `SSO_AUDIT_LOG=stdout` or the audit client as a boot gate; add a startup warning when store=memory (batch B4-5 scope).
- **Recovery validation**: restart drill — 10 logins, restart, assert 10 login events survived (L0 persistence, B4-5 assertion) and sessions still validate.

### SRE-04 — High — Vault readiness gated on audit relay backlog: audit side-channel can take down the file platform
- **Evidence (Verified)**: `aero-vault/cmd/server/audit_governance.go:52-59` builds `runtimeReadiness` incl. audit runtime; `cmd/server/http.go:46-67` readyz runs `extra.Ready`; `internal/auditgovernance/runtime.go:145-160` — `Ready()` errors when `OldestPendingAuditGovernance` older than `maxLag` (900s default, `config_audit_governance.go:66`). Relay has **zero metrics** (no Observe calls in the whole `auditgovernance` package — H6), no dead-letter (permanent 422s retried forever — F3).
- **Impact**: a stuck sink or one poison row flips `/readyz` 503 → orchestrator restarts → restart loop for the whole file platform. Violates the documented "audit never blocks business" principle.
- **Remediation**: H1/B3-1 — backlog reports `degraded` + `maxLag×0.5` (450s) alert; never 503; dead rows excluded from `OldestPending`; add relay metrics (H6) to feed H2 alerts.
- **Recovery validation**: D1 drill — stop sink 60 min, assert vault `/readyz` 200 + alert firing at 450s; replay after sink returns.

### SRE-05 — High — aero-id audit write failures are swallowed (8+ sites)
- **Evidence (Verified)**: `_ = s.audit.Record(...)` at `internal/service/account.go:155,184`, `audit_hold.go:59,74`, `legacy_cutover.go:88`, `callback.go:160,221,247`, `account_provision.go:168`; metric `ObserveAuditIngestFailure` exists (`audit_ledger.go:73`, `internal/pkg/metrics/control_plane.go:97-98`) but the error is discarded — no rollback, no retry, no alert, no replay.
- **Impact**: silent audit loss on the identity provider — the platform's most security-relevant events (login, account lifecycle) can vanish without any operator signal.
- **Remediation**: B2-1 (F5) — move all to `AppendInTx`; where in-tx is impossible, return the error or at minimum `ObserveAuditIngestFailure` + alert; grep gate bans `_ =` (T-5).
- **Recovery validation**: mock the ledger to fail → assert counter increments or operation fails; no silent call sites remain (grep).

### SRE-06 — High — Sink store: no fsync, O(snapshot) writes, CAS conflicts → bare 500, readyz blind to store
- **Evidence (Verified)**: `internal/store/store.go:258-264` — `os.WriteFile(tmp)+os.Rename` with **no `Sync()`** (power-loss → silent snapshot revert; archive side *does* fsync, `internal/archive/archive.go:52`); ingest rewrites the whole snapshot (`store.go` Save semantics; distributed review F-1: O(all events) idempotency scan, `QueryEvents` O(ledger)); `statusForError` maps `ErrSnapshotConflict` → 500 (`httpapi/server.go:959-978`) with no retry (F-01); `readyz` probes only archive, not the store (`server.go:161-178`) — PG outage → readyz still 200.
- **Impact**: data-loss window on power failure; latency grows linearly with ledger size; any CAS conflict (2nd writer, restart race) is a 500 with no recovery path; orchestrators get no signal when the store is down.
- **Remediation**: B1-2 — fsync + dir sync; bounded jitter retry on `ErrSnapshotConflict` (or explicit single-writer); store probe in readyz (T-6: stop PG → readyz 503); B1-3 — wire migration-001 relational ledger or capacity envelope (BenchmarkIngest) to escape O(ledger).
- **Recovery validation**: T-6 PG CI — kill PG mid-ingest → readyz 503, restart → 202/Duplicate, no 500; power-loss drill on file backend → state.json not truncated/corrupt.

### SRE-07 — High — Envelope `tenant_id` silently overwritten by token tenant (fail-open where design says fail-closed)
- **Evidence (Verified)**: `service.go:381-389` — `tenantHint := tenantID` (from claims), `resolveIngestTenant` (`sources.go:101-117`) returns the hint when non-empty; envelope `tenant_id` is never compared — `event.TenantID = tenantID` overwrites it. (DS-08.)
- **Impact**: a misconfigured producer's events land under the caller's tenant silently — cross-tenant mis-attribution in a compliance ledger, undetectable except by reconciliation.
- **Remediation**: mismatch → 422 (`tenant_mismatch`) per contract (ingestion-design §1.2), plus alert; keep overwrite only for backward-compat fixtures.
- **Recovery validation**: send event with envelope tenant ≠ token tenant → 422; event not recorded.

### SRE-08 — Medium — IdP metrics unbounded cardinality (legacy); rate limiter is proxy-blind
- **Evidence (Verified)**: `middleware_metrics.go:72-76` — `RequestsTotal`/`RequestDuration` labeled with raw `r.URL.Path` (e.g. `/admin/users/{id}` → per-ID series); SDK already has a cardinality guard test (`platform/metrics/middleware_method_test.go`). `middleware_ratelimit.go:16-40` — per-IP in-memory limiter keyed on `r.RemoteAddr`; behind the nginx proxy every client shares the proxy IP → single shared bucket (global throttle for all users) and the per-route map is per-process (multi-replica → per-replica limits).
- **Impact**: Prometheus memory blow-up on the legacy IdP; effective rate limiting is either a fleet-wide brake or bypassable, and it does not see `X-Forwarded-For`.
- **Remediation**: normalize labels (route pattern or method-only, SDK pattern); honor trusted `X-Forwarded-For` (nginx `X-Real-IP` is already set) or move limiting to the proxy/LB; document per-replica semantics.

### SRE-09 — Medium — SLO/alert/runbook layer is draft-only and nothing is deployed
- **Evidence (Verified)**: `sink docs/SLO_ALERTS.md` (Draft 2026-08-05) defines SLOs, error budgets, alert expressions — but `deploy/prometheus.verify.yml` contains **scrape config only, zero rules**, no Alertmanager, no runbook documents anywhere in the six repos; cross-region RPO/RTO (≤60s/≤15min) explicitly "未在本机宣称" (not claimed); no backup/restore drill, no load test evidence (BenchmarkIngest envelope is a B1-3 deliverable — i.e., missing).
- **Impact**: even with the batch work shipped, no alert will page anyone; error budgets are unmeasured; DR claims are unvalidated.
- **Remediation**: ship rules files with the batch gates (H2/H6 alerts, integrity P0, `archive_unavailable` P1), alert ↔ runbook mapping per component, schedule D1 + restore drills, and re-baseline SLOs from real load before cutover (G8).
- **Recovery validation**: fire a synthetic integrity failure → P0 alert delivered; run restore drill → RPO/RTO measured.

### SRE-10 — Medium — Sink topology has undeclared/orphan surfaces
- **Evidence (Verified)**: gRPC server registered when `AUDIT_GRPC_LISTEN` set (`cmd/audit-api/main.go:120-135`, `internal/grpcapi/server.go:28-33` — authenticated with `audit:event:write`, but the surface is **undeclared** in docs/deploy; DS-09); `cmd/audit-outbox-relay` + `cmd/audit-kafka-consumer` + `cmd/audit-projector` + `internal/outbox` are wired **only** in `deploy/docker-compose.verify.yml` with `dev:demo:service` tokens; `migrations/001_control_plane.sql` (ledger tables) is dead schema — zero Go references (F-1).
- **Impact**: operators can't enumerate ingress paths; the verify-compose's `dev:` tokens mean the topology demo itself runs with auth bypass; a future implementer may wire a second ingest path (dual-path drift, cf. aero-id F6).
- **Remediation**: B1-6 — wire or remove orphan components; declare the single supported ingress; add a grep gate (`no enqueue` outside outbox worker).
- **Recovery validation**: grep for `enqueue`/`Insert(` call sites outside the outbox; T-6 topology sweep.

### SRE-11 — Medium — Console boundary: probes don't cover the trust path; audit read path is client-side only
- **Evidence (Verified)**: k8s probes hit `/` (static index.html) — a console pod is Ready while IdP/sink are down; nginx has no upstream health checks, no tuned proxy timeouts, no error page fallback (`nginx.conf`); `lib/services/audit_log_service.dart` — localStorage ring (max 1000, forgeable, per-browser); no sink read path (B6-1 pending). Token refresh (`token_refresh_service.dart:57-64`) swallows refresh failures (`catch (_)`) and relies on `onUnauthorized` retry; legacy IdP refresh rotation is non-atomic Get→Delete→Create (DS-06) with no reuse detection.
- **Impact**: rollout tooling cannot detect a broken auth/audit path; the console's audit UI shows non-evidence (client-side records) as if it were an audit trail; silent refresh failure = user-facing logout storms under IdP pressure, and a rotated-but-reused refresh token replays indefinitely.
- **Remediation**: probe `/readyz`/upstream via nginx `health_check` or a small status endpoint; wire console audit page to sink read API via BFF (B6-1, T-12); make refresh failure observable (metric + alert) and add reuse detection + atomic rotation on the IdP side.
- **Recovery validation**: T-12 — query from console → self-audit row appears at sink; devtools-forged localStorage entries no longer render as evidence.

### SRE-12 — Medium — Startup/shutdown gaps in the legacy IdP
- **Evidence (Verified)**: legacy `main.go` — graceful shutdown 15s budget (fine), but audit emitter failures on shutdown path are dropped; audit remote client uses a circuit breaker (5 failures/30s reset, `config.go:206-219`) — but the *default* config (`AuditServiceURL=""`) silently disables audit (SRE-03); readiness ignores audit/permission dependencies (SRE table). aero-id shutdown is exemplary by contrast (`app.go:95-121`: gRPC NOT_SERVING flip → relays → GC → servers → trace shutdown).
- **Impact**: no operator signal when the IdP's audit or permission dependencies are down (only request-path errors); a restart loses the in-memory audit ring.
- **Remediation**: include audit backend + permission resolver in `/ready`; log a boot-time warning when audit is disabled; document the circuit breaker states in a runbook.

### SRE-13 — Info — Positive controls worth keeping
- aero-id: same-tx ledger+outbox, advisory-lock serialized chain, gRPC health flip on shutdown, warmup probes, outbox relay with backoff + dead-letter + metrics sink (`internal/outbox/relay.go:23-27,45-56,72`).
- aero-vault: lease-fenced claims (TTL 30s > 2×HTTP timeout, config validation enforces `ClaimTTLSeconds > 2*HTTPTimeoutSeconds`), `SKIP LOCKED` claims, boot-time binding/backlog fail-closed (`runtime.go:166-190`).
- sink: idempotency with content-digest conflict detection (409), quota per tenant (per-instance), request-id + W3C trace propagation, panic recovery in middleware (`server.go:94-128`), graceful shutdown (10s) + tracer + gRPC drain (`main.go:145-162`).
- SDK IdP: DR readiness aggregate with report-only default (`build_http.go:56-57`) — safe-by-default; bounded tenant metrics (`platform/metrics/tenant_metrics.go`).
- console k8s: rolling update `maxUnavailable:0`, topology spread, seccomp/read-only root, probes present (though shallow).

---

## 4. Failure drills

Drills are the *validation* step for the remediation above; none exist in any repo today (no scripts, no docs — **Missing**).

| Drill | Scenario | Procedure (proposed) | Pass criteria | Maps to |
|---|---|---|---|---|
| D1 — outage (sink down) | Sink unreachable 60 min | Stop sink; observe vault relay backoff + alert at 450s; restore; verify backlog drains, no restart loop | vault `/readyz` stays 200; 450s alert fired; all facts delivered after restore; no poison loop | B3-1/H1, T-3 |
| D2 — outage (IdP down) | IdP unreachable | Console login fails; producers hit breaker; restore; verify sessions/refresh recover | console 502s visible in proxy logs; breaker transitions observable; no data loss on producer side | SRE-02/03 |
| D3 — saturation | Ingest at quota limit + 10× rate | Load-generate to quota; observe 429s, duplicate/conflict counters; verify latency SLO not breached | `audit_ingest_quota_exceeded_total` rises; p99 accepted < 150ms (baseline); no 500s | SLO_ALERTS §2 |
| D4 — bad rollout | Deploy with `AUDIT_ALLOW_DEV_AUTH=true` manifest | CI manifest scan must fail before apply; if it ships, boot must fail closed | manifest scan red; boot refuses dev-only trust; all routes 401 on `dev:` tokens | B1-1/T-1.1 |
| D5 — stale state | Restart with stale `state.json` (no fsync loss simulated) | Kill -9 mid-save (or truncate), restart; run integrity verify + restore drill | integrity verify reports invalid → P0; restore-from-archive path re-seals; no silent accept | F-3/F-01, B1-2 |
| D6 — restore | Restore audit from archive to fresh store | Preview → create → approve (SoD) → verify hash chain + segments | restore approval workflow exercised; digest chain re-verifies; export matches | sink restores APIs; G8 |
| D7 — refresh-token replay | Replay a rotated refresh token | Replay after rotation; assert old token rejected and reuse flagged | no token family split; reuse detected + alerted | DS-06, SRE-11 |

---

## 5. Launch blockers, rollback triggers, monitoring gaps, residual risks

### Launch blockers (G1/G8, in order)
1. **SRE-01** — `AUDIT_ALLOW_DEV_AUTH` default true + dev-only config accepted (B1-1, T-1.1). Blocks any real traffic.
2. **SRE-02** — trust anchor pinning + claim contract (B4-1, T-8a/T-1.2): without `kid`/`tenant_id`/`roles`/scope-registry on the **active** IdP, sink rejects or mis-attributes every real token.
3. **SRE-03** — production IdP must not boot with memory stores or no audit backend (B4-5).
4. **SRE-04** — vault readiness decoupling + relay metrics (B3-1/H6, D1) before vault relay goes to production.
5. **SRE-09** — alert rules deployed (integrity P0, lag, archive) before cutover (G8).

### Rollback triggers (per service)
- **Sink**: integrity verify `result=invalid` (P0); `archive_unavailable`; 5xx error rate > 1% over 5m (SLO draft); ingest conflict burst.
- **IdP**: login failure-rate spike; `/readyz` red on store ping; console 502 rate; token minting without `kid` (regression to legacy generator).
- **Producers (vault/aero-id/im)**: relay dead-letter growth without corresponding sink 4xx; outbox backlog > 450s with alert; duplicate-rate anomaly.
- **Rollback mechanics**: images are immutable tags (`latest` used in console compose — pin SHA tags); `state.json`/PG snapshot must be backed up **before** any sink deploy (no backup exists today — do this first).

### Monitoring gaps (nothing currently deploys alerts)
- No Prometheus rules/Alertmanager in any repo; sink `prometheus.verify.yml` is scrape-only.
- aero-vault relay: **zero metrics** (H6) — cannot detect stalled relay or dead-letter growth.
- IdP (SDK): no alert on memory-store boot, no audit-disabled warning surfaced, no refresh-reuse counter.
- Console: no client-side telemetry of refresh failures/unauthorized storms; proxy has no error-rate metrics.
- Sink: no per-tenant ingest/query latency split (histogram is aggregate), no store-backend latency, no fsync-failure counter.
- Cross-component: no trace propagation from producers → sink exists (W3C traceparent, verified sink middleware) but no distributed trace UI/alert integration documented; no log aggregation/storage story anywhere (stdout logs only).

### Residual risks (accepted / unmeasured)
- **Unknown**: real volume/latency envelopes (no BenchmarkIngest), retention defaults unverified, backup/restore never drilled, cross-region RPO/RTO unvalidated — all explicitly out of scope of local claims per SLO_ALERTS.md §4.
- **Partial**: SDK IdP surface (rate limiting, discovery, scope validation) not fully re-verified this round — protocol review pinned the legacy tree; B4 must re-pin and re-verify on the SDK tree.
- **Proposed**: single-writer sink is an availability ceiling (no multi-replica ingest); relational ledger (B1-3) is the escape hatch; console read path (B6) turns the audit UI from demo to evidence.
- The legacy `/home/u1/snaplink` twin remains deployed-adjacent with a broken discovery contract and no `kid` — until retired, it is a standing misconfiguration trap (SRE-02).

---

## Checks that ran (this review)

- Read `prompts/README.md` (authority/evidence/severity rules).
- Source-level inspection (no tests executed, no code modified): legacy IdP `cmd/server/{main,routes,config,logger,middleware_metrics,middleware_ratelimit}.go`, `sso.go` (ClientCredentials/RefreshWithRotation), `token/jwt/generator.go`; SDK `cmd/sso-server/{config.yaml,build_http.go,build_app_oidc.go,build_app_cluster.go,build_app_core.go}`, `infrastructure/defaultimpl/{issue_payload,ed25519_types,ecdsa_jwt_issuer,rsa_jwt_issuer}.go`, `platform/metrics/`; sink `cmd/audit-api/main.go`, `internal/{httpapi/server.go,auth/auth.go,store/store.go,service/{service,sources}.go,archive/archive.go,grpcapi/server.go}`, `deploy/*`, `docs/SLO_ALERTS.md`; aero-vault `cmd/server/{http.go,audit_governance.go}`, `internal/auditgovernance/{runtime.go,relay.go}`, `internal/config/config_audit_governance.go`; aero-id `cmd/server/main.go`, `internal/app/app.go`, `internal/service/{audit_ledger,account,audit_hold,callback,legacy_cutover,account_provision,control_plane_metrics}.go`, `internal/outbox/relay.go`; console `nginx.conf`, `Dockerfile`, `docker-compose.yml`, `k8s/base/deployment.yaml`, `lib/services/{audit_log_service,token_refresh_service,session_storage_web}.dart`, `lib/api/sso_client.dart`.
- Grep scans: `AUDIT_ALLOW_DEV_AUTH`, `dev:platform`, `readyz`/`healthz` per repo, `_ = .*Record` sites, `Observe`/metrics in vault relay, `kid` headers, `enqueue`/`Insert(` in sink outbox.
- Cross-checked against sibling reviews: protocol V2-1..13, distributed DS-01..09, database F-1..F-13, batch plan B1–B6 and gates G0–G8 (all consistent with my independent verification; SRE-02/DS-07/V2-5/F-13 agree on the two-tree hazard).

**Bottom line**: the platform is reviewable and the failure modes are enumerable, but today **no alert would fire, no runbook exists, no drill has run, and the default configurations of both the sink (dev auth) and the IdP (memory stores, audit off) are unsafe for production**. SRE-01 and SRE-02 are the release-blocking gates; SRE-04 and SRE-09 must land before the audit pipeline is production-enabled.
