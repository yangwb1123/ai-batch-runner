# SRE Review — Snaplink Platform Audit Pipeline & API/Frontend Boundary

**Role**: SRE engineer — can operators detect, withstand, and recover from failures?
**Scope**: API backend (snaplink IdP + audit-svc, aero-id, aero-vault, aero-im, sink
`snaplink-audit-governance`) and the external frontend/proxy boundary
(snaplink-console Flutter bundle + nginx proxy, openresty gateway).
**Baseline**: `audit-ingestion-design-draft.md` §0–§3, with prior cross-role findings
(protocol F-1..F-15, distributed F1–F13, database F-01..F-06) treated as
cross-referenced evidence, not re-verified here.
**Method**: read executable code, configs, deploy assets, and ops docs in all six
repos. Ran no live traffic/fault-injection drills; claims that need a drill are
marked `[drill needed]` with the expected outcome per code path.

All claims labeled **Verified** (read in code/config/deploy asset for the reviewed
revision), **Partial**, **Missing**, **Proposed** (design draft), or **Unknown**.

---

## 1. Service/Dependency Map and Operational Assumptions

```
                        ┌─────────────────────────────┐
   Browser (console SPA)│ nginx (console)             │──proxy──► snaplink IdP :8080
   static bundle /app/  │ TLS verify per upstream      │──proxy──► billing / stripe-adapter
                        └─────────────────────────────┘              (optional)
                                    │
   ┌──────────────┬─────────────────┼──────────────────┬───────────────────┐
   ▼              ▼                 ▼                  ▼                   ▼
┌─────────┐  ┌──────────┐  ┌───────────────┐  ┌──────────────┐   ┌──────────────┐
│snaplink │  │ aero-id  │  │  aero-vault   │  │   aero-im    │   │ openresty    │
│ IdP     │  │ PG+Redis │  │ SQLite (WAL)  │  │ PG+Redis+NATS│   │ gateway (dev)│
│ PG/Redis│  │ SSO dep  │  │ S3 compat     │  │ +blob store  │   │              │
└────┬────┘  └────┬──────┘  └──────┬───────┘  └──────┬───────┘   └──────────────┘
     │ (audit-svc│                │                  │
     │  memory/  │ audit_publications│ audit_governance│ (no connector — gap)
     │  file)    │ (outbox)      │  facts (outbox)  │
     ▼           ▼                ▼                  ▼
        ┌────────────────────────────────────────────────┐
        │      snaplink-audit-governance (sink)          │
        │  audit-api :8089  /healthz /readyz /metrics    │
        │  store: file JSON snapshot (no fsync) OR       │
        │         PG audit_state_snapshot single jsonb   │
        │  archive: S3 Object Lock (WORM) / file dir     │
        │  relay, kafka-consumer, projector,             │
        │  governance-worker (headless, no probes)       │
        └───────┬───────────────┬───────────────┬────────┘
                ▼               ▼               ▼
             Kafka/Redpanda  ClickHouse      S3 (archive)
```

**Shared dependencies**: PostgreSQL (sink, aero-id, snaplink), Redis (aero-id,
snaplink), Consul/etcd (snaplink registry), Kafka/Redpanda + ClickHouse (sink
production path), S3-compatible WORM bucket (sink archive), Vault Transit
(optional checkpoint signing), OTLP collector (optional tracing, all services),
Prometheus (scrape config exists only for sink in `deploy/prometheus.verify.yml`;
vault has `deploy/prometheus/alerts.yml`; aero-id has none).

### Operational assumptions (as-built, not as-drafted)

| # | Assumption | Status |
|---|---|---|
| A1 | Producers deliver audit facts to the sink in production | **False today** — every producer connector is off by default: aero-id `audit_governance.enabled: false` (config.yaml:174), vault `AUDIT_GOVERNANCE_ENABLED=false` (config_audit_governance.go:55), aero-id k8s manifest never sets governance env; sink dev-auth is the only exercised path |
| A2 | The platform has alerting and runbooks | **False** — sink `docs/SLO_ALERTS.md` is Draft with alert rules that exist only on paper; `deploy/` ships scrape config only, zero rule files |
| A3 | SLO/RTO/RPO are documented and current | **Partial** — sink draft SLOs exist (marked "recalibrate before launch"); cross-region RPO/RTO explicitly unvalidated; no SLOs for IdP/producers/console |
| A4 | A dependency failure is contained to the audit pipeline | **False** — vault couples audit-relay backlog into `/readyz`; a 15-min sink outage makes the entire file platform not-ready |
| A5 | Dead/poison audit records are visible and recoverable | **False** — aero-id dead letters are purged by TTL cleaner with no replay tool; vault has no dead state at all |
| A6 | Every service can report readiness accurately | **Partial** — aero-im probes PG/Redis/NATS/blob with 2s timeouts (best in class); sink `/readyz` checks only the archive, not its store; console probes only static `/` |
| A7 | Metrics have bounded cardinality | **False at the IdP** — `sso_http_requests_total{path=r.URL.Path}` is unbounded per resource id; sink and aero-id/aero-im metrics are bounded |

---

## 2. Readiness Table

Signal per service: **liveness / readiness / metrics / health semantics**.

| Service | Liveness | Readiness (deps checked) | Failure behavior | Alert | Runbook |
|---|---|---|---|---|---|
| snaplink IdP `cmd/server` | `/live` always 200 (registry.go:111) | `/ready` = session-store ping + registry check, 5s deadline, parallel (routes.go:1013) | Store down or Consul/etcd down → **503 not_ready** (registry outage takes IdP out of rotation even though login works) | **None defined** (no rule files) | **None** |
| snaplink audit-svc | `/health` only (routes.go:15); no `/ready` | none | Memory ring (default, 100k bound) loses all events on restart; no drop/eviction metric | None | None |
| snaplink admin/permission/network/registry svc | `/health` (Verified in each routes.go) | none | No dependency checks at all | None | None |
| aero-id API | `/healthz` always 200 | `/readyz`: critical/non-critical checks (health.go:69); SSO `readiness_critical: false` **by default** (config.yaml:112) → SSO outage = `degraded` 200, not 503; stale_ttl 300s serves last-known identity | Degraded-only by design; breaker opens after 5 consecutive failures, cooldown 10s | No alert on degraded/breaker-open (metrics exist: `ObserveSSOCall` ok flag) | None |
| aero-vault | `/healthz` 200 | `/readyz` = repo ping + storage probe + `extra` readiness = **audit governance runtime** (audit_governance.go:52-59) | Audit backlog > maxLag 900s OR binding drain in progress OR **one poison row** → whole vault 503 (single replica, SQLite) → **full file platform outage** | vault alerts.yml has HTTP/AI/integrity rules — **zero governance rules** (no governance metrics exist) | None |
| aero-im | `/health`, `/health/live` | `/health/ready`: PG/Redis/NATS/blob probes 2s each; commercial readiness fail-closed on missing projection but outage-tolerant by design (health.rs) | Clean dependency reporting, best in class | /metrics optional bearer; no rules | None |
| sink audit-api | `/healthz` 200 | `/readyz` = archive Ready only (server.go:157-175); **store (PG/file) not probed** | S3 WORM down → 503; PG dead → **readyz still 200** until ingest 500s | Draft rules only (SLO_ALERTS.md §2, not deployed) | None |
| sink relay / kafka-consumer / projector / governance-worker | **None** (no HTTP at all) | — | Stall = silent audit gap; only log lines (`handled=N`); no heartbeat metric | **None — no dead-man's switch** | None |
| console (nginx + SPA) | k8s probes `GET /` (static 200) | **static content only** — never verifies `SNAPLINK_UPSTREAM` health | API down → console "ready" with broken UX; nginx passive health only, no retry/timeout tuning (defaults 60s), single upstream | None | None |

**Readiness dependency graph**: IdP→{session store, registry}; aero-id→{PG, Redis, SSO
(non-critical)}; vault→{PG/SQLite, storage, **audit relay**}; sink→{archive (only)};
aero-im→{PG, Redis, NATS, blob, commercial}; console→{static, proxies}.

---

## 3. Findings (by severity)

### H1 — Vault couples audit-pipeline health into core file-platform readiness [High]

**Evidence (Verified)**: `cmd/server/audit_governance.go:52-59` appends
`auditRuntime` to the readiness group; `runtime.go:145-161` `Ready()` fails when
(1) a binding drain is in progress, or (2) `OldestPendingAuditGovernance` age
exceeds `MaxLagSeconds=900` (config default). `readyzHandler` (http.go:46) turns
any failure into 503. Helm deployment is single-replica SQLite (`replicaCount`,
`MaxOpenConns=1`). A single poison row (409/422-class fact) retries forever
(no dead state, relay.go:97-110) — so one bad row permanently red-flags readiness
→ k8s removes the pod → **the entire file platform is down** (restart loop,
matching distributed F3).

**Production impact**: the audit relay's backlog or one poison fact converts into
an availability incident for file storage — the opposite of "audit is a
non-blocking side effect." A sink outage >15 min takes vault down.

**Remediation**: split audit health from core readiness (report `degraded`,
keep serving; the draft's "never drop, business flow unaffected" §1.5 requires
this); convert the backlog check into a **warning metric + alert at
maxLag×0.5** instead of a readiness flip; implement dead state per draft §1.4.

**Recovery validation**: `[drill needed]` stop sink → observe readyz flip at
~900s (expected today); after fix, expect 200 + alert. Replay: restart sink,
watch `OldestPending` decay.

### H2 — No alerting exists in any production deploy asset for the audit pipeline [High]

**Evidence (Verified)**: sink `deploy/` = `docker-compose.verify.yml` +
`prometheus.verify.yml` (scrape only, no `rule_files`); `docs/SLO_ALERTS.md`
(2026-08-05, "Draft…必须用真实负载重新校准") defines 9 alerts (P0 integrity,
P1 error rate, P2 conflicts, P3 duplicates/quota) **that are not deployed
anywhere**. aero-id has zero alert rule files. vault `deploy/prometheus/alerts.yml`
covers HTTP 5xx/latency, AI cost, integrity — nothing for the governance relay.
The design draft's DLQ alert thresholds (`dead_count > 0` >15min, §1.4) and the
`audit.outbox.failure` / `audit.relay.dlq.replay` events are `[PROPOSED]` and
**do not exist in any codebase** (grep across aero-id/aero-vault/sink = 0 hits).

**Production impact**: queue growth, sink outage, poison rows, and dead letters
are all undetectable today; the draft's own P5 gate ("DLQ/alert monitoring live")
cannot pass.

**Remediation**: deploy Prometheus rules for all services (start from the draft
table — every expression maps to an existing sink metric); implement the two
failure events; add a "governance enabled" gauge so a misconfigured rollout
(zero traffic) is visible.

**Recovery validation**: `[drill needed]` inject 422-class fact → expect dead row
+ alert within 15 min (today: nothing).

### H3 — SLO/RTO/RPO: only the sink has numbers, and they are unvalidated Draft [High]

**Evidence (Verified)**: `SLO_ALERTS.md` — ingest availability 99.99%/mo, query
99.95%/mo, accepted p99<150ms, ledgered p99<2s, projection p99<5s, same-region
RPO 0 (production async path), cross-region RPO≤60s/RTO≤15min **explicitly "未在
本机验证"**. `BENCHMARKS.md` header: "参考实现基线，不是生产 SLO 承诺" — and it
documents the O(n²) cumulative write ceiling (see H5). No SLO exists for the IdP
(login availability), aero-id/vault write paths, or console. No error-budget
tracking implementation anywhere (the 50%-freeze rule is paper policy).

**Production impact**: no basis for launch gate, no rollback trigger thresholds
for the platform's most critical signal (audit integrity), no measurement of the
"ledgered/indexed" deltas the SLO table itself requires (metrics don't exist).

**Remediation**: per-service availability/durability SLOs (IdP login, vault
file ops, aero-id account ops, producer→sink delivery latency); implement
accepted→ledgered delta measurement; record RPO/RTO per data class; calibrate
thresholds with real load before launch.

**Recovery validation**: `[drill needed]` cross-region cutover drill must
measure RPO/RTO; today there is no DR topology to drill against (H5/M5).

### H4 — Dead-letter paths end in silent loss with no recovery tooling [High]

**Evidence (Verified)**:
- **aero-id**: publications dead-letter after `maxAttempts` (`callback_audit.go`
  `publicationOutcome` → `dead_letter`); `RunOutboxCleaner` **deletes** dead
  letters past `deadLetterRetention` (repository.go:127-152, new.go:355-365);
  no replay tool exists (distributed F8: ~2–3 min to dead-letter at 8 attempts).
  Draft's `audit.relay.dlq.replay` event: missing.
- **aero-vault**: **no dead state at all** — every failure (incl. 409/422 poison)
  retries forever with 300s cap (relay.go:97-110; distributed F3) and permanently
  red-flags readiness (H1).
- **sink relay**: permanent 4xx (except 429) → `failed` dead-letter immediately
  (outbox/http.go:47-49, maxAttempts 8) — no replay path for ingest outbox
  (the `/api/v1/operations/{id}/replay` endpoint replays admin operations, not
  outbox records).

**Production impact**: audit events are dropped silently (aero-id purge) or
stuck forever (vault) with no operator signal — the exact class of failure this
platform exists to prevent.

**Remediation**: implement draft §1.4 (dead status + replay + `audit.relay.dlq.replay`
event + alert); pause or gate the TTL purge on alert acknowledgement; add
dead-count/dead-age metrics to vault relay.

**Recovery validation**: `[drill needed]` — today: inject 422 → aero-id dead-
letters within minutes and purges after retention; vault retries forever. After
fix: dead row visible, replay restores event, alert fired.

### H5 — Sink store: O(n) full-snapshot rewrite per ingest, no conflict retry, no readyz coverage [High]

**Evidence (Verified)**: `store.go` — every `Update` clones + rewrites the whole
snapshot (file: no fsync; PG: single `audit_state_snapshot` jsonb row, optimistic
version `WHERE version = $2`, `postgres.go:29-33`); `ErrSnapshotConflict` is
**not mapped** to a domain error → `statusForError` default → **500** (server.go
statusForError; service.go has no conflict handling). `BENCHMARKS.md`:
"快照式控制面存储是 O(n²) 累计写入…不是生产形态". Relational `ledger_events`
schema (migration 001) is **unused dead code** (database F-01). `/readyz` probes
only the archive, not the store — a dead PG still reports ready (server.go:157).

**Production impact**: ingest p99<150ms SLO will be violated as the ledger grows;
multi-replica deployment produces 500 retry storms (producers retry, each retry
rewrites the snapshot); store outage is invisible to readiness; the draft's
high-volume classes (L1 aggregation) have no ceiling.

**Remediation**: adopt the database review's P0a sink-hardening phase (activate
`ledger_events`, single-writer or conflict retry with jitter, readyz store ping,
fsync decision), or gate production launch on the Kafka→relational production
path per ADR-0001/0003/0004.

**Recovery validation**: `[drill needed]` 2-replica concurrent ingest test —
expect 500s today; stop PG — expect `/readyz` 200 today.

### H6 — Producer-side audit observability is asymmetric; vault relay has zero metrics [High]

**Evidence (Verified)**: vault `internal/auditgovernance/*` contains **no
Observe/metrics calls** (grep = 0) — the relay and runtime only log; no
delivered/failed/dead/lag counters exist to feed H2's alerts. aero-id is better
(`ObserveOutboxPublished/Failed/Drain`, `ObserveAuditIngestFailure` in
control_plane.go) but its silent-loss path (service-layer `Record` post-commit
`_ =` discarded errors, account.go:155/184 — distributed F5) has no metric or
alert. IdP audit-svc default memory ring (100k) has no drop/eviction counter.
Sink headless workers emit only `handled=N` log lines (relay main.go).

**Production impact**: a stalled vault relay, a silently skipped IdP audit, or a
dead sink worker are indistinguishable from a healthy system.

**Remediation**: relay metrics (attempted/delivered/failed/dead/oldest-age,
vault + sink workers); ring-drop counter (IdP); publication-age alert (aero-id);
heartbeat/last-run-success metric for headless workers with dead-man's-switch
alert.

**Recovery validation**: `[drill needed]` pause relay → expect lag metric
growth today: only vault's readyz (via H1) would eventually fire — and that's
the wrong signal.

### H7 — Synchronous `wait_for=ledgered` publisher couples account-op latency to sink health [High]

**Evidence (Verified)**: aero-id publisher sets `wait_for=ledgered`
(connector/auditgovernance/publisher.go:40); distributed F6: inline publish adds
up to 5s latency to account requests; the sink's snapshot rewrite (H5) makes
sink latency grow with ledger size. The design's "never drop, business flow
unaffected" (§1.2) is implemented as a **request-path dependency**.

**Production impact**: sink degradation propagates to user-facing login/account
ops; overload cascades (slow sink → more in-flight publishes → bulkhead fills →
callbacks starve, P6-06).

**Remediation**: async relay + receipt polling for non-extreme-risk classes
(draft §1.2 sync-minimal-ack carve-out), load-shed at sink, and latency SLO for
the publish path.

**Recovery validation**: `[drill needed]` degrade sink → measure account-op
p99 today (expect SLO breach); after fix, expect no request-path impact.

### M1 — Console proxy and readiness are blind to API health [Medium]

**Evidence (Verified)**: k8s `readinessProbe`/`livenessProbe`/`startupProbe` all
`httpGet /` (k8s/base/deployment.yaml:81-89) — a static 200 regardless of
`SNAPLINK_UPSTREAM`; nginx.conf has no `proxy_connect_timeout`/`proxy_read_timeout`/
`proxy_next_upstream` tuning (defaults 60s, single upstream, no failover); SPA
has no runtime error telemetry; `SNAPLINK_ADMIN_OAUTH_RESOURCES` is build-time
baked (DEPLOY.md: "changing a container runtime environment variable does not
change this setting").

**Production impact**: API outage presents as a healthy green console; page
loads succeed while every action fails; config change requires image rebuild.

**Remediation**: probe a proxied path (e.g. `/health` through
`SNAPLINK_UPSTREAM`) for readiness; explicit proxy timeouts + retry budget;
client-side error reporting; treat the build-time OAuth resource list as a
release artifact in the change process.

### M2 — IdP readiness couples to service registry; audit-svc has no readiness at all [Medium]

**Evidence (Verified)**: `buildReadinessChecks` = session-store ping + `CheckRegistry`
(routes.go:1013-1020; registry.go:190-203) — Consul/etcd down → `/ready` 503 even
though auth flows don't need discovery; registry self-registration failure is
only a warning (good, main.go). audit-svc `/health` returns ok with a dead
backend (memory/file only; routes.go:15).

**Production impact**: registry blips convert into IdP availability incidents;
audit-svc memory-ring data loss on restart is invisible.

**Remediation**: registry non-critical in readiness (report degraded); audit-svc
`/ready` + backend-ping; drop/eviction metric (H6).

### M3 — IdP `/metrics` unbounded label cardinality [Medium]

**Evidence (Verified)**: `middleware_metrics.go:43-44` labels on
`r.URL.Path` (raw); routes `/admin/sessions/{id}`, `/admin/apps/{id}`,
`/admin/users/{id}` (routes.go:53-109) generate one series per resource id.
Sink metrics are label-free counters (bounded, server.go:182-203); aero-id and
aero-im use bounded route labels.

**Production impact**: Prometheus memory growth proportional to session/app/user
ids; slow scrape, tsdb bloat, possible target death — the classic
high-cardinality incident.

**Remediation**: normalize to the mux pattern (or a small route-class label);
drop or aggregate `/admin/sessions/` per-id series.

### M4 — Sink store durability and readiness gaps [Medium]

**Evidence (Verified)**: file backend writes without fsync (database F-01);
PG snapshot row has no conflict retry (H5); `/readyz` skips store ping.
Vault SQLite: single file, no documented backup (M5).

**Production impact**: crash between write and OS flush loses accepted events
(same-region RPO 0 claim is not met by the file backend); store outage is
invisible until 500s.

**Remediation**: fsync (or document loss window), readyz store probe,
backup plan (M5).

### M5 — No backup/restore story for platform durable stores [Medium]

**Evidence (Verified)**: sink has an approval-gated restore workflow
(`/api/v1/restores*`, Verified by database review) — but restores into the same
snapshot store; no backup of the PG snapshot row, ClickHouse projection, or
vault SQLite file is documented anywhere; design §1.5 "Local DB loss" handling
("ledger gap detected by integrity spot-checks") is `[PROPOSED]`, unimplemented.

**Production impact**: disk loss on the sink store or vault = total audit/history
loss; the draft's own failure-mode table acknowledges it.

**Remediation**: pg_dump/PITR for PG-backed sink + aero-id/snaplink; ClickHouse
backup; vault SQLite WAL checkpoint + snapshot; document per-store RPO; restore
drill.

### M6 — Dev-grade defaults reachable in production posture [Medium]

**Evidence (Verified)**: sink `AUDIT_ALLOW_DEV_AUTH` defaults **true**
(audit-api/main.go:31; protocol F-1 Critical — dev tokens carry full
read/export/legal-hold powers); demo compose sets `AUDIT_ALLOW_DEV_AUTH: "true"`
and local-only secrets; aero-id config.yaml: `insecure: true` for SSO TLS,
`authz_required: false` (config.yaml:106-109); no CI gate rejects dev defaults.

**Production impact**: a config mistake yields an unauthenticated admin-equivalent
surface and untrusted audit ingest; no lint catches it at deploy time.

**Remediation**: fail-closed defaults for a production profile, config-lint gate
in CI (the sink's `--check-config` is a good pattern to replicate), and a
pre-launch checklist item asserting dev auth is off.

### M7 — "Fastest rollout" claim is deployment-false: no stock binary publishes [Medium]

**Evidence (Verified)**: database F-03 + my checks: aero-id `enabled: false`
(config.yaml:174-176), vault `AUDIT_GOVERNANCE_ENABLED=false`, aero-id k8s
manifest contains no governance configuration at all; sink requires dev tokens.
Following the deployment docs produces a platform with **zero audit traffic**
and no signal of that fact (H2/H6).

**Production impact**: rollout tickets following "pattern proven" will silently
ship without any audit events; first-event verification is the only guard.

**Remediation**: rollout runbook with explicit enable steps + first-event
verification + "governance enabled" gauge; document per-service enablement in
the migration skeleton (§2 of the draft).

### M8 — aero-id SSO degradation mode has no alert [Medium]

**Evidence (Verified)**: `readiness_critical: false` default, `stale_ttl: 300s`,
breaker threshold 5/cooldown 10s (config.yaml:105-121) — by design readiness
stays 200; `ObserveSSOCall` metric exists but no rule consumes it.

**Production impact**: SSO outage degrades identity validation for up to 5 min
(stale) then fast-fails — invisible to operators until user reports.

**Remediation**: alert on breaker-open and degraded readiness; consider
readiness_critical for the auth path.

### L1 — Sink metrics are label-free; no accepted→ledgered delta exists [Low]

**Evidence (Verified)**: `audit_http_requests_total`/`errors` are plain counters
(no route/status labels) — bounded but cannot compute per-route availability
(ingest vs query SLOs in H3's table are not separately measurable today);
SLO_ALERTS.md's ledgered/indexed deltas have no metric. Fixed histogram buckets
1ms–5s are fine for p99 at the stated thresholds.

### L2 — aero-im health design is the pattern to copy [Low/Info]

**Evidence (Verified)**: `/health`, `/health/live`, `/health/ready` probe
PG/Redis/NATS/blob with 2s timeouts and per-dep results; commercial readiness is
fail-closed on missing projection but deliberately not a probe dependency
("accepted tenants continue during a short billing/audit outage" — health.rs);
`/metrics` optional bearer token. The draft's aero-im connector (§1 per-service)
does not exist yet — when built, it should inherit this posture plus relay
metrics (H6).

### L3 — Graceful shutdown is well-implemented across services [Info, Verified]

snaplink: SIGTERM → 15s drain → OTel flush → registry deregister (main.go);
aero-id: gRPC health flips NOT_SERVING before drain, ordered stop of 14
background loops (app.go:107-135); sink: 10s drain (audit-api main.go); vault:
signal.NotifyContext; aero-im: `/health/ready` 503 while draining
(boot/shutdown.rs:38). This is the strongest area of the platform.

---

## 4. Failure Drills

Each drill lists expected behavior **today** (per verified code) and the
post-fix target.

### D1 — Outage: sink audit-api down for 60 min
1. aero-id: publications claim rows, fail, backoff; bulkhead bounds in-flight; after `maxAttempts` → **dead_letter**, then TTL purge deletes them → **silent loss** (H4). Account ops continue (H7 latency until timeout).
2. vault: relay retries forever (cap 300s); at ~15 min `Ready()` maxLag breach → **whole vault 503** (H1).
3. IdP: unaffected (no wiring).
4. Detection today: nothing until vault flips. **Target**: P1 alert at 5 min, vault stays serving, no dead-letter purge before alert ack.

### D2 — Saturation: ledger grows, snapshot rewrites slow down
1. Ingest cost scales O(ledger size) (H5); p99 accepted latency climbs past
   150ms (BENCHMARKS curve: 2.2ms @ ~1k events → linear; no data beyond 5k).
2. aero-id sync publisher waits `ledgered` → account-op latency follows sink
   (H7); bulkhead fills → callbacks starve.
3. Detection today: `audit_http_request_duration_seconds` histogram only (no
   per-route split, L1). **Target**: latency SLO alert + producer decoupling +
   L1 aggregation thresholds calibrated from real load (draft §3 open decision,
   still open).

### D3 — Bad rollout: enable aero-id governance with matrix-provisioned scope
1. Draft/scope-matrix say `audit:event:ingest`; implementation requires
   `audit:event:write` (distributed F2) → every publish 403 → dead-letter →
   TTL purge → silent loss (H4) — all before any alert exists (H2).
2. **Target**: rollout gate = first-event verification + dead-letter alert
   before retention window; scope-matrix corrected to `audit:event:write`.

### D4 — Stale state: two sink replicas + clock skew
1. Concurrent ingests on PG backend → optimistic-lock conflict → unmapped
   **500** → producer retries (retry storm, H5). Sink accepts any `occurred_at`
   (distributed F7 — skew policy unimplemented) → event ordering/attribution
   wrong without rejection.
2. Detection: `audit_ingest_conflicts_total` does **not** count snapshot
   conflicts (only content conflicts, server.go:233/273). **Target**: conflict
   retry with jitter; skew rejection per draft §1.5 (>5min, `[PROPOSED]`).

### D5 — Restore: sink snapshot corrupt / lost
1. Approval-gated restore workflow exists and is Verified (database review);
   but the source of truth is the same single snapshot row/file — restore
   requires the row itself to be intact; no backup of it (M5); file backend
   has no fsync.
2. Integrity verification (`POST /api/v1/integrity/verify`, segments +
   checkpoints) is the recovery validation tool — Verified present.
3. **Target**: PG PITR/backup of snapshot row + ClickHouse + S3 archive
   (S3 Object Lock is the durable copy — but only if archive is enabled and
   `readyz` shows it), documented RPO, and a full restore drill run before
   launch (SLO_ALERTS.md cross-region RPO/RTO remain unmeasured).

---

## 5. Launch Blockers, Rollback Triggers, Monitoring Gaps, Residual Risks

### Launch blockers (fail-closed gate for production cutover)
1. **B1**: dev auth must be disabled in the sink (`AUDIT_ALLOW_DEV_AUTH=false`)
   and verified via `--check-config`; dev-secrets flag off (H4/M6; protocol F-1).
2. **B2**: alerting must be deployed (H2) — the draft's P5 gate requires it and
   it does not exist.
3. **B3**: vault readiness blast radius must be decoupled (H1) — as-is, the
   audit pipeline can take down the file platform.
4. **B4**: dead-letter purge must be gated or replay implemented (H4) — silent
   loss is the one outcome the platform must never have.
5. **B5**: sink storage must move off the O(n) snapshot rewrite (H5) or
   production volume must be capped and SLOs scoped to that cap (BENCHMARKS.md
   explicitly disclaims production readiness).
6. **B6**: SLOs recalibrated with real load; cross-region RPO/RTO either
   validated by drill or removed from claims (H3).

### Rollback triggers (observable, monotone, actionable)
- `audit_integrity_checks_total{result="invalid"} > 0` — **P0, immediate**
  isolation (SLO_ALERTS.md §2).
- Ingest error rate >1% over 5 min (draft SLO mapping).
- Vault readyz 503 attributable to audit maxLag (H1) — roll back relay config,
  not the file platform.
- ledgered p99 >2s sustained (H7/H5).
- Dead-letter count/age above threshold on any producer (H4) — pause purge,
  roll back the triggering change.
- `sso_http_requests_total` series count growth (M3) — roll back deploy.

### Monitoring gaps (no signal today)
1. No Prometheus rules anywhere for the audit pipeline (H2).
2. Vault relay metrics: **zero** (H6).
3. Headless workers: no heartbeat/dead-man's switch (H6).
4. IdP audit ring drops/evictions: no counter (H6).
5. Sink store unavailability: not in `/readyz` (H5/M4).
6. accepted→ledgered/indexed deltas: no metrics (L1/H3).
7. Error-budget tracking: not implemented (H3).
8. aero-id SSO breaker-open/degraded: no alert (M8).
9. Console API-health: no signal (M1).
10. `audit.outbox.failure` / `audit.relay.dlq.replay` events: designed, absent
    in all codebases (H2/H4).

### Residual risks (accepted or requiring decisions)
- **R1**: reference-implementation O(n²) sink ceiling vs draft's high-volume
  classes — L1 aggregation thresholds have **no workload evidence anywhere**
  (database review). Decision needed before any volume commitment.
- **R2**: clock-skew policy unimplemented (distributed F7) — sink accepts any
  `occurred_at`; ordering/attribution integrity gap.
- **R3**: aero-id silent-loss window on service-layer `Record` (distributed F5)
  — crash between business commit and audit record = gap with no alert.
- **R4**: vault fact IDs are random UUIDs (distributed F4) — sink dedup and the
  draft's deterministic event_id are vacuous for vault facts; duplicates
  regenerate.
- **R5**: aero-id token invalidation on 401 missing (distributed F8) — ~24h
  stall after credential rotation.
- **R6**: console's "cannot be bypassed" self-audit guarantee unimplemented
  (database F-06) — `audit.event.read` never recorded.
- **R7**: cross-region DR topology does not exist; RPO/RTO unmeasured (H3/M5).
- **R8**: aero-im connector + moderation priority partition (draft §1 per-
  service, §3 open decisions) — not built; moderation events have no path.
- **R9**: operator-owned decisions: backfill policy, sync-ack list, connector
  hardening (draft §3) remain open; each has an operational default that is
  currently "not implemented" — the safe default is to ship with those features
  explicitly disabled and verified (M7).

---

## Appendix — Evidence Index (files read for this review)

| Claim | Path |
|---|---|
| IdP /live /ready /health + checks | snaplink `registry.go:63-215`, `cmd/server/routes.go:58-60,1013` |
| IdP metrics cardinality | snaplink `middleware_metrics.go:43-44` |
| IdP graceful shutdown / OTel / CB | snaplink `cmd/server/main.go:120-260` |
| IdP config defaults | snaplink `cmd/server/config.go:70-100` |
| audit-svc memory ring 100k | snaplink `cmd/audit-svc/config.go:25-27`, `main.go:111-147` |
| Console nginx proxy | console `nginx.conf` (TLS per upstream, no timeouts) |
| Console probes / build-time config | console `k8s/base/deployment.yaml:81-89`, `DEPLOY.md` |
| Sink readiness/metrics | sink `internal/httpapi/server.go:73-203` |
| Sink store snapshot + conflict | sink `internal/store/store.go:114-186`, `postgres.go:26-33` |
| Sink relay dead-letter | sink `internal/outbox/relay.go:17-63`, `http.go:44-49` |
| Sink deploy assets (no rules) | sink `deploy/*` |
| SLO draft | sink `docs/SLO_ALERTS.md`; `docs/BENCHMARKS.md` |
| aero-id readiness (critical/degraded) | aero-id `internal/handler/http/health.go`, `config.yaml:105-121` |
| aero-id publisher sync + dead-letter purge | aero-id `connector/auditgovernance/publisher.go:40`, `service/callback_audit.go:47-110`, `outbox/repository.go:127-185`, `app/new.go:355-365` |
| aero-id governance disabled default | aero-id `configs/config.yaml:174-176`; k8s manifest (no governance env) |
| aero-id drain (gRPC NOT_SERVING) | aero-id `internal/app/app.go:107-135`, `handler/grpc/ops.go` |
| Vault readyz + audit runtime coupling | vault `cmd/server/http.go:46-68`, `cmd/server/audit_governance.go:52-59`, `internal/auditgovernance/runtime.go:145-161` |
| Vault relay no dead state, no metrics | vault `internal/auditgovernance/relay.go:97-110`, `runtime.go` (no Observe calls) |
| Vault config defaults | vault `internal/config/config_audit_governance.go:55-75` |
| Vault helm probes | vault `deploy/helm/aero-vault/templates/deployment.yaml:77-89` |
| Vault alert rules (no governance) | vault `deploy/prometheus/alerts.yml` |
| aero-im health/metrics | aero-im `crates/aero-server/src/routes/health.rs`, `metrics.rs:286-330` |

*Advisory only — no files modified. All recommendations are proposals pending
human approval per the evidence standard.*
