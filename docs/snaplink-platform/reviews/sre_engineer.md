SRE review complete. Deliverable written to `docs/snaplink-platform/reviews/sre-engineer-review.md` (33KB), consistent with the sibling reviews from protocol_expert, distributed_engineer, and database_architect.

## What I verified

Read executable code, configs, and deploy assets across all six repos (snaplink IdP + audit-svc, aero-id, aero-vault, aero-im, sink, console proxy), plus the sink's `SLO_ALERTS.md`/`BENCHMARKS.md` drafts. Ran no live drills — those are marked `[drill needed]` with expected behavior per code path.

## Headline findings

**High (7):**
- **H1 Blast radius**: vault couples audit-relay backlog into `/readyz` (`audit_governance.go:52` + `runtime.go:145`). Sink outage >15 min (maxLag=900s) or one poison row → **entire file platform 503** — the opposite of "audit never blocks business."
- **H2 No alerting exists**: sink `deploy/` ships only a scrape config; `SLO_ALERTS.md` (Draft) defines 9 alerts that are deployed nowhere; aero-id has zero rule files; the draft's `audit.outbox.failure`/`audit.relay.dlq.replay` events exist in no codebase.
- **H3 SLO/RTO/RPO**: only the sink has numbers, all Draft and unvalidated; cross-region RPO/RTO explicitly "未在本机验证"; no error-budget tracking.
- **H4 Silent loss paths**: aero-id dead letters are **deleted by TTL cleaner with no replay tool**; vault has no dead state (retries forever); sink relay dead-letters permanent 4xx with no replay.
- **H5 Sink store**: every ingest rewrites the whole JSON snapshot (no fsync; PG single row, optimistic lock with **no conflict retry → unmapped 500**); O(n²) cumulative ceiling documented in BENCHMARKS.md; `ledger_events` relational schema is dead code; `/readyz` never probes the store.
- **H6 Observability asymmetry**: vault governance relay has **zero metrics**; headless workers (relay/consumer/projector) have no probes or heartbeats; IdP audit ring drops uncounted.
- **H7** aero-id's synchronous `wait_for=ledgered` publisher couples account-op latency to sink health.

**Medium (8):** console probes only hit static `/` (blind to API health); IdP `/ready` fails on registry outage; IdP `/metrics` uses raw `r.URL.Path` → **unbounded cardinality** on `/admin/sessions/{id}`-style routes; no backup story for sink snapshot/ClickHouse/vault SQLite; dev-grade defaults (`AUDIT_ALLOW_DEV_AUTH=true`, aero-id `insecure: true`, `authz_required: false`); rollout "fastest path" is deployment-false (all connectors off by default, k8s manifest never enables governance); aero-id SSO degradation has no alert; sink metrics can't separate ingest vs query SLOs.

**Strong areas (Verified):** graceful shutdown is excellent everywhere (aero-id flips gRPC health NOT_SERVING before draining; aero-im serves 503 while draining); aero-im has the best health design (2s dependency probes, fail-closed commercial readiness) — the pattern to copy; sink has `--check-config` and an approval-gated restore workflow.

The report includes the full readiness table (9 components), 5 failure drills with today-vs-target behavior, 6 launch blockers, rollback triggers, 10 monitoring gaps, and 9 residual risks — every claim labeled with evidence paths.
