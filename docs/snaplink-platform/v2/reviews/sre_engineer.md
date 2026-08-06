SRE review complete and written to **`docs/snaplink-platform/v2/reviews/sre-review-v2.md`** (29KB). I applied `prompts/README.md` (evidence standard, severity scale, advisory-only) and verified every claim against current code across the 6 repos plus the console proxy boundary. No tests executed, no code modified.

## Summary

**Service/dependency map (1)**: browser → nginx proxy (console `k8s/base/deployment.yaml` → `sso-server.sv-sso.svc.cluster.local`) → SDK IdP (`workspace/demo/snaplink cmd/sso-server`, image `snaplink/sso-server:latest`) → memory/sqlite/postgres stores; producers aero-id/aero-vault/aero-im → outbox relay → sink (file/PG snapshot + WORM/S3 archive). The defining hazard: **two IdP codebases** — the console wiring proves the SDK is the production trust anchor (already stamps `kid`), but batch B4 rows and protocol review pin the legacy `github.com/opensso/sso` tree (no `kid`, wrong discovery codified by a passing test).

**Readiness table (2)**: every component has a signal, but (a) console probes only static `/`, (b) sink `/readyz` probes archive but **not its own store**, (c) vault `/readyz` 503s on audit backlog (side-channel gates the file platform), (d) **no alert rule is deployed anywhere** — only a draft `SLO_ALERTS.md` and a scrape-only prometheus config.

**Findings (3)**, 13 total, headlined:
- **SRE-01 Critical**: `AUDIT_ALLOW_DEV_AUTH` defaults `true` + dev-only config accepted — unsigned `dev:platform:platform-admin` bypasses everything (verified `main.go:45`, `auth.go:44-48`).
- **SRE-02 Critical**: trust anchor unpinned; B4 rows mix two trees; legacy tokens fail sink's `kid`-requiring verifier.
- **SRE-03 High**: IdP ships memory stores + audit off by default — restart loses identity plane and the platform's own login audit.
- **SRE-04/05/06/07 High**: vault readyz coupling + zero relay metrics; aero-id 8+ `_ =` swallowed audit writes; sink no-fsync O(snapshot) store with CAS→500; envelope `tenant_id` silently overwritten instead of 422.
- SRE-08..12 Medium: unbounded metric labels + proxy-blind per-IP rate limit (legacy), draft-only SLO/alert/runbook layer, orphan/undeclared sink surfaces (gRPC, relay, Kafka), console probes/localStorage audit/refresh-replay, legacy startup gaps.

**Drills (4)**: 7 proposed drills (D1 sink outage, D2 IdP outage, D3 saturation, D4 bad rollout, D5 stale state, D6 restore, D7 refresh replay) — none exist today (**Missing**).

**Launch/rollback (5)**: blockers = SRE-01 → SRE-02 → SRE-03 → SRE-04 → SRE-09 in order; rollback triggers per service; monitoring gaps (no rules files, no vault relay metrics, no console telemetry); residual risks (unverified RPO/RTO ≤60s/≤15min, no BenchmarkIngest, legacy twin as standing misconfiguration trap).

Bottom line: failure modes are enumerable, but today **no alert would page anyone, no runbook exists, no drill has run, and default configs of both sink and IdP are unsafe for production**. Consistent with the sibling reviews (DS-01/02, V2-2/5, F-13) — with the two-tree pinning (SRE-02) independently confirmed from the console deployment manifests.
