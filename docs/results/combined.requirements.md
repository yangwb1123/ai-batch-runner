All key claims verified against the code. The five expansion rounds converged on a single roadmap; I've confirmed the load-bearing facts myself. Here is the release proposal.

---

# Release Proposal: 7×24 Governance Release — ai-batch-runner

**Status**: Proposal (advisory; requires evidence review and `make ci` before any implementation)
**Scope**: Standalone ai-batch-runner repo. Snaplink frontend constraint applies vacuously — no `interfaces/web` assets exist in this tree; none are planned (non-goal).

## 1. Problem, Users, Desired Outcome, Evidence, Assumptions, Non-goals

### Target users and problems

| User | Problem workflow | Material pain |
|---|---|---|
| Unattended operator | Rolling analysis (`for i in …`-style loops), 7×24 `--max-rounds 0` | Unbounded cost exposure; no accounting; no stop condition on poison tasks |
| Pipeline author | SDLC pipelines with meta orchestration + gates | Validation bypassed on resume; validator failures loop silently; cost not attributed |
| Batch user | `--reuse` resume, parallel batches | Stale artifacts trusted silently; parallel batch failures re-sync into "thunderstorm" retries |
| Tool maintainer | 7×24 deployment | Unbounded log growth; no multi-instance safety; no observability events |

**Desired outcome**: a 7×24 unattended run is *bounded* (budget cap), *self-governing* (failures triaged, isolated, and reported, not blindly retried), *fresh* (nothing silently stale), *accountable* (per-call token/cost events), and *safe under concurrency* (instance lock, bounded logs) — with zero change to the existing agent/reporting contract.

### Evidence (this revision, verified against code)

**Verified (paths + symbol):**
1. `Task` already carries `provider/thinking/tools/exclude_tools` and `to_cmd` emits them — `models.py:88-108`.
2. Pipeline templates drop them: `_task_from_def` copies only `model/cwd/timeout` — `pipeline.py:96-113`; meta-role tasks carry neither model nor provider.
3. `--reuse` skips the validation gate on resume: full-reuse short-circuit `return [], True` — `pipeline.py:449-451`; existence-only checks at `pipeline.py:146/352/381/424` and `cli.py:375`. Validator chain effectively runs once at first write.
4. Reuse is opt-in: `--force` exists (`cli.py:51`) and `reuse_outputs = args.reuse and not args.force` (`cli.py:160`) — stale reuse requires explicit `--reuse`, so risk class is "opt-in semantic blind spot", not silent system defect.
5. Parallel mode has no retries: `run_parallel(tasks, workers, validate_cmd)` signature — `runner.py:376`; rate-limit failures fail the task and the next round re-syncs at the `--round-delay` boundary (default 60s, `cli.py:100`).
6. Log file grows unboundedly: `logging.FileHandler` — `cli.py:112`.
7. Retryability is a single regex: `_RETRYABLE_REASON` matches `rate|429|quota|network|connect|unreachable|timeout` — `runner.py:194-201`.
8. Gate is fail-closed on missing verdict: `_GATE_VERDICT_RE` (`pipeline.py:624`); missing line ⇒ FAIL (`pipeline.py:790`). But it checks line *presence* only — no reason check, no contradiction check (fake-pass possible; gate stages already accept `validate_cmd`).
9. Meta iterations are bounded: `max_iterations` default 3 — `config.py:67`. Unbounded cost comes from rolling-analysis loops and `--max-rounds 0` (`cli.py:98,148`), where every round legitimately "succeeds".
10. Unknown validator names are executed as raw shell commands — `config.py:105-112`; a typo or missing binary fails deterministically per task per round, entering retry.
11. `_spawn_agent` uses `start_new_session=True` (`runner.py:124-125`), `_kill_group` (`runner.py:107`) — orphan-free by design; a SIGKILLed runner leaves an in-flight agent to finish (billed, output lost) — double-billing on restart + `--reuse`.
12. `_combine_outputs` is bare concatenation — `pipeline.py:115-125` (untrusted-evidence injection surface).
13. Session flags support only `start`/`continue` keys — `config.py:82-91`.
14. No instance lock anywhere in the tree; two 7×24 instances on one working directory race freely.
15. stdin is consumed once before the round loop (fix present) — `cli.py:162-169`.

**Proposed (from pi documentation, not verified in this repo — spike required):**
16. pi session files (JSONL, per-workdir) carry per-message `usage {totalTokens, cost}` — basis for exact per-call metering.
17. pi has default auto-compaction and a `--fork` CLI capability — basis for session rotation.

**Missing/Unknown:** pi session file format stability across versions; real-world cost data for any workflow; stall-detection thresholds.

### Assumptions
- pi remains the driven stock binary; we only read its session files and pass its flags (external contract, read-only).
- Existing 91 regression tests, `quality.py --strict`, and `make ci` remain the release gate for every workline.
- Default-on behavior changes must be conservative and safe with defaults off for anything affecting cost or scheduling; log rotation and instance lock are safe to default on.
- The five review rounds are advisory proposals; this document is the product translation, not an approval to implement.

### Non-goals (explicit)
- Token accounting by parsing CLI output (superseded by session usage diff; fragile).
- Resident agent process reuse / daemon mode (conflicts with the process-group kill / zero-orphan model).
- Tier/policy routing engine; per-agent adapter layer beyond field plumbing.
- Built-in secret scanner implementation (registry hook is the deliverable; scanner is out of scope).
- Any UI, frontend, or `interfaces/web` assets; changes to pi itself or its session format.
- Compliance certification, staffing, adoption, latency, or delivery targets (unknown — not invented here).

## 2. Prioritized Scope (Must / Should / Could / Won't)

| Priority | Workline | Rationale |
|---|---|---|
| **Must** | **R1 — Reuse freshness validation**: when `--reuse` skips a task/stage, re-run its configured validators on the existing artifact; failure ⇒ mark for regeneration. | Verified correctness hole (E3) with the smallest fix (~50 lines) and zero new config surface; restores the "validation gate is invariant" promise. |
| **Must** | **R2 — Validator failure feedback**: capture validator stderr/exit into `TaskResult`; inject into the retry prompt. | Highest ROI: converts silent deterministic retry loops into self-correcting iterations; unlocks R5's classification. |
| **Must** | **R3 — Log rotation**: `RotatingFileHandler` (one line). | Verified unbounded growth (`cli.py:112`); the only continuously growing entity in 7×24 (archives rotate artifacts). |
| **Must** | **R4 — Instance lock**: O_EXCL lock file at startup, released on exit; second instance refuses with a clear error. | Verified zero locking; two instances corrupt a shared working directory and interleave git commits. |
| **Must** | **R5 — Failure triage + task circuit breaker + batch stall detection**: tri-state classification (transient/permanent/environment), isolate poison tasks, halt the round loop when K consecutive rounds change nothing. | The core "7×24 must not run away" promise; closes the blind spot where only poison tasks remain and rounds spin (`--max-rounds 0`). |
| **Should** | **R6 — Metering base**: per-call session usage diff (JSONL `usage` fields), JSONL event stream, webhook hook. | Foundation everything else hangs on (R7, R8, R9); dependency on pi's format (spike first). |
| **Should** | **R7 — Budget circuit breaker**: `--budget-max` (attempts/rounds dimension first; token-exact once R6 lands) and optional daily budget; on cap: stop rounds, report, decision-log. | Headline cost-control feature for unattended operation; deliberately bounded to avoid token-parse fragility. |
| **Should** | **R8 — Fingerprint cache `--reuse` 2.0 + sidecar**: content-addressed reuse (skip only if inputs unchanged), provenance fields, format settled once. | Converts `--reuse` from "exists ⇒ skip" to "unchanged ⇒ skip"; fixes stale-reuse and damaged-file blind spots; larger change (unify ~6 `exists()` sites). |
| **Could** | **R9 — Session rotation**: `session_flags` gains `fork` key + watermark-based rotation + compaction monitoring. | Tiny; blocked on pi `--fork`/compaction verification; context management mostly exists in pi. |
| **Could** | **R10 — Hetero-routing plumbing**: `_task_from_def` passes through `provider/thinking/tools`; meta roles carry `model`; CLI `--provider` override. | Verified plumbing gap (E1, E2); low risk, but design decisions on precedence/strategy — defer. |
| **Could** | **R11 — Structured validator JSON protocol** (pass/warn/fail), gate fake-pass hardening (judge validator on gate stage), `approval: true` stage points, evidence fences in `_combine_outputs`, output byte cap, `secretscan` registry entry. | All small, mostly registry/config-level; grouped as "governance hardening" batch; each independently shippable. |
| **Won't** | Token accounting via CLI parsing; resident agent reuse; tier engine; built-in scanner; UI/frontend; per-agent signature table generalization. | See Non-goals; E.g., per-agent error-signature tables are deferred until a second agent binary is actually adopted (no demand evidence). |

**Ordering note**: R1–R4 are independent and shippable as separate commits with tests (the rounds' own proposal order C1→C2→C3+A); R5 depends on R2's capture; R6 is the dependency hub for R7–R9. MVP = R1–R5 + R6's log rotation (R3) — see §5.

## 3. User Stories (Given/When/Then + ownership)

Ownership key: **NM** = nested module (this repo: `pbatch/`, `pi-batch.py`, `quality.py`, `ai/`), **SDK** = `pbatch` public API surface (imported by `ai/run-review.py`, tests; backward compatibility required), **SB** = stock binary pi (external contract; read-only dependency).

**US-1 (Must, R1) — Resume never bypasses the gate.** *Owner: NM (pipeline.py, cli.py reuse paths)*
- Given a task whose output exists and whose configured validators have since changed (e.g. `cli.py check` upgraded), When the user resumes with `--reuse`, Then the validators re-run against the existing artifact; if they fail, the task is regenerated; if they pass, the task is skipped and reported as "reused+validated". Acceptance: `--reuse` output must never be trusted without a current validation pass; single-batch (`cli.py:375`) and all three pipeline reuse paths (`pipeline.py:146/352/381`) covered by tests.

**US-2 (Must, R2) — Validation failure is visible to the agent on retry.** *Owner: NM (runner.py retry branch) + SDK (TaskResult gains a field, additive)*
- Given a task whose validation fails with exit code and stderr, When the runner retries it, Then the retry prompt includes the validator's stderr/exit摘要, and the final summary surfaces the failure signature. Acceptance: retry prompt contains the validator output; field addition is backward-compatible (old serializers unaffected).

**US-3 (Must, R3) — Logs are bounded.** *Owner: NM (cli.py:112)*
- Given a 7×24 run with `--log-file`, When the log reaches the rotation size, Then it rotates (configurable count), and the disk footprint is bounded. Acceptance: two size thresholds exercised in tests; default rotation on, no new required config.

**US-4 (Must, R4) — One instance per working directory.** *Owner: NM (cli.py entry)*
- Given an already-running instance's lock file, When a second instance starts on the same working directory, Then it exits with a clear error naming the holder; when the first instance exits (including crash), the lock is released/stale-detected. Acceptance: O_EXCL creation; stale-lock recovery path tested (PID liveness or age).

**US-5 (Must, R5) — Poison tasks cannot burn the budget forever.** *Owner: NM (runner.py, cli.py _run_rounds) + SDK (TaskResult gains failure class, additive)*
- Given a task failing permanently (e.g. deterministic validation error) or an environment-class failure (e.g. validator typo), When rounds continue, Then the task is isolated after N attempts (circuit open), environment-class failures halt the stage, and a batch stall (K rounds, no new successes, unchanged failure distribution) stops the loop with a report — all recorded in the decision log. Acceptance: each of the three classes has a fixture-based test; `--max-rounds 0` loops demonstrably terminate under stall; thresholds configurable with conservative defaults.

**US-6 (Should, R6) — Every call is metered and observable.** *Owner: NM (runner.py) + SB (pi session file format — contract risk)*
- Given a completed task, When the run finishes (or a call completes), Then a JSONL event records per-call usage diff (tokens, cost) keyed by session id, plus a webhook hook fires on configured events (task fail, gate reject, budget cap). Acceptance: usage diff matches session-file deltas in the spike; event stream is append-only and never blocks the runner.

**US-7 (Should, R7) — Budgets stop the machine.** *Owner: NM (cli.py, runner.py)*
- Given `--budget-max` or a daily budget, When the cap is reached, Then the round loop stops, a report and decision-log entry are written, and the exit code signals "budget exhausted" distinctly from failure. Acceptance: caps honored in both serial and parallel modes; exit-code contract documented.

**US-8 (Should, R8) — Reuse means "unchanged inputs", not "file exists".** *Owner: NM (unify exists() sites) + SDK (sidecar format)*
- Given a rolling/pipeline run with `--reuse 2.0` semantics, When upstream inputs are unchanged, Then the task is skipped; when inputs changed (or the artifact is empty/corrupt), it regenerates. Acceptance: fingerprint includes input content hash + validator config version; empty/damaged artifact never reused; format frozen (one metadata format, not manifest/sidecar/event-stream triplication).

**US-9 (Could, R10) — Pipeline templates honor heterogeneous agents.** *Owner: NM (pipeline.py:96-113, meta task construction) + SDK (Task unchanged — fields already exist)*
- Given a pipeline task definition with `provider`/`thinking`/`tools`, When the pipeline runs, Then those fields reach the agent command (as they already do in single-batch, `models.py:101-108`), and meta-role tasks can declare a model. Acceptance: field parity between single-batch and pipeline paths; precedence (CLI override > template) documented and tested.

## 4. Edge-Case and Dependency Table

| # | Edge case / dependency | Evidence | User impact | Fallback / mitigation |
|---|---|---|---|---|
| 1 | Reused artifact stale vs validators changed | Verified (E3; `pipeline.py:449-451`) | Resume silently bypasses gate → bad artifact promoted | R1 revalidates on reuse; failure ⇒ regenerate |
| 2 | Reused artifact empty/corrupt (external writer) | Verified (tool never writes empty outputs; `exists()` blind) | Downstream stages consume garbage | R8 fingerprints + emptiness check; R1 validation catches |
| 3 | Rate-limit burst at round boundary in parallel | Verified (no retries in `run_parallel`, `runner.py:376`) | Whole batch fails, re-syncs at `--round-delay` | R5 triage; R7 budget; jitter + provider token bucket (R11/Could) |
| 4 | Validator name typo / missing binary | Verified (`config.py:105-112` raw-shell fallback) | Deterministic per-task-per-round failure, retry burn | R5 environment-class circuit (same exit/stderr across tasks ⇒ environment) |
| 5 | In-flight agent orphaned by runner SIGKILL | Verified (`runner.py:124-125` process groups) | Double billing on restart (billed, output lost, rerun) | R5 running-state residue detection ⇒ warn, don't silently rerun (per round-5 N1) |
| 6 | Two 7×24 instances, one workdir | Verified (no lock in tree) | Interleaved writes, git commit races, validator half-reads | R4 O_EXCL lock (fail-fast, stale recovery) |
| 7 | Concurrent meta roles × repo-scope validators | Verified (roles share cwd; `pyquality` scans repo) | False validation failures → retries → cost | R11 `scope: repo` convention: repo-scope validators belong at stage level (serial `commands`), documented first |
| 8 | Gate fake-pass (VERDICT line without substance) | Verified (line-presence regex, `pipeline.py:624`) | Gate approves contradiction | R11 judge validator on gate `validate_cmd` (registry hook works today) |
| 9 | Unbounded log growth (7×24 months) | Verified (`cli.py:112` FileHandler) | Disk exhaustion | R3 RotatingFileHandler (Must, trivial) |
| 10 | Rolling analysis: every round "succeeds" | Verified (`--max-rounds 0`, `cli.py:148`; archiving per round) | Unbounded spend by design | R7 budget cap; R5 stall detection (no progress ⇒ stop) |
| 11 | **Dependency**: pi session JSONL usage fields | Proposed (pi docs; unverified here) | R6/R7 grounded on external contract | Spike first; fallback: attempts/rounds-dimension budget (R7 1.0) works without it |
| 12 | **Dependency**: pi `--fork` / auto-compaction | Proposed (pi docs; unverified here) | R9 depends on it | R9 is Could; drop without harm if contract differs |
| 13 | Untrusted evidence injection via `_combine_outputs` | Verified (bare concat, `pipeline.py:115-125`) | Prompt-injection surface | R11 fences + "untrusted data" instruction (mitigation, not elimination) |
| 14 | Parallel × shared session | Already rejected (`pipeline.py:463-467`) | — | Keep rejection; document in R6 event ordering |
| 15 | stdin tasks across rounds | Already fixed (`cli.py:162-169`) | — | Regression test kept |
| 16 | Migration: existing users' `--reuse` workflows | Behavior change only when `--reuse` given | Reuse slower (revalidation) | R1 logs "reused+validated"; R8 opt-in flag keeps old semantics available |

## 5. Success Indicators, MVP Boundary, Rollout/Rollback, Unresolved Decisions

### Measurable success indicators (no invented adoption/latency targets)
1. **Gate invariant**: in every `--reuse` resume scenario in the test suite, no artifact is promoted without a current validation pass (assert-based; count = new + existing 91 tests green).
2. **Bounded spend**: a fixture scenario with a poison task under `--max-rounds 0` terminates via circuit/stall within configured thresholds; budget cap halts rounds and exits with the documented distinct code.
3. **Bounded disk**: log footprint stays within rotation limits over a scripted long-run fixture.
4. **Single instance**: second-instance start fails fast with actionable error in all paths (clean exit, crash, stale lock).
5. **Observability**: each executed task in a test run emits exactly one JSONL event with usage diff matching session-file deltas (spike-validated).
6. **Quality gates**: `make ci` + `python quality.py --strict .` green at each workline commit; no `exists()`-based reuse site left unguarded (grep-able invariant).
7. **Compatibility**: `ai/run-review.py --resume` and all `examples/` pipelines run unchanged (SDK backward compatibility).

### MVP boundary
**MVP = R1, R2, R3, R4, R5** (all Musts): the correctness/safety core, no new required configuration, each a separate commit with tests. R6 spike (pi session format validation) runs *in parallel* with MVP as an investigation, so R7/R8 can start immediately after. R6–R8 form release 2 ("cost governance"); R9–R11 are the hardening batch, each independently shippable. Nothing in MVP changes the agent invocation contract or existing flags.

### Rollout / rollback
- **Rollout**: every workline lands as an isolated commit behind `make ci` + `quality.py --strict`; new flags default **off** except R3 (rotation, safe) and R4 (lock, fail-fast). R1 changes behavior only when the user already opted into `--reuse`. R5 thresholds default conservative (high N, K) so existing 7×24 runs are unaffected until thresholds are hit.
- **Rollback**: revert the commit (no persistent state migration; the optional sidecar in R8 is opt-in and versioned). The decision log is append-only — a reverted experiment leaves a trace, which is a feature.
- **Contract risk**: R6/R7 must not silently fail if pi session format changes — treat parse failure as a logged degradation to attempts/rounds budgeting, never a crash.

### Unresolved product decisions (need owner input before R7/R8/R11)
1. **Metadata format freeze**: one sidecar/provenance format (fields, location, versioning) to avoid manifest/sidecar/event-stream triplication — decide before R8 starts.
2. **Budget semantics**: per-batch vs daily vs both; on cap: hard stop vs degrade (e.g. stop meta roles only); who is notified via webhook.
3. **Stall thresholds**: K rounds and category-stability window defaults; acceptable tradeoff between false-positive stops and burn.
4. **Validator feedback volume**: how much stderr enters the retry prompt (size cap) — quality vs prompt-bloat.
5. **`approval: true` semantics**: marker file vs interactive confirm; timeout default; fail-closed behavior — needs an operator workflow statement.
6. **Hetero-routing precedence**: CLI `--provider` vs template field vs meta default — decide with R10, no demand evidence yet (Why R10 is Could).
7. **`secretscan`**: registry hook now (zero code) vs built-in scanner — scanner deferred; confirm hook + documentation is sufficient for the trust story.

**Bottom line**: the five expansion rounds converged on a stable roadmap, and my code verification confirms the load-bearing claims (reuse bypasses validation, parallel has no retries, unbounded logs, dropped provider fields, no instance lock). The release is bounded to six Must worklines that restore the tool's own invariants — "no unvalidated promotion, no unbounded spend, no unbounded growth, no concurrent writers" — followed by the cost-governance layer once the pi session-format spike validates the metering contract.
