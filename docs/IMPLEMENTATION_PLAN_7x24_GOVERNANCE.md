# Implementation Plan — 7×24 Governance Release (ai-batch-runner)

Status: Tech-lead implementation plan for the "7×24 Governance Release" proposal.
Every claim below was re-verified against the code in this repo before planning.

## 0. Verification summary (what I checked, what changed)

All 15 "Verified" evidence items from the proposal hold against the code.
Corrections / elevations:

1. **Citation correction**: `max_iterations` default 3 lives in
   `pbatch/pipeline.py::_parse_stage_def` (`s.get("max_iterations", 3)`), not
   `config.py:67` (config.py has no such constant). The fact itself is verified.
2. **Elevation E16/E17 → verified against installed pi docs**:
   `docs/session-format.md` (installed pi package) confirms JSONL sessions at
   `~/.pi/agent/sessions/--<path>--/<timestamp>_<uuid>.jsonl` (path = workdir
   with `/`→`-`), per-message `usage {input, output, cacheRead, cacheWrite,
   totalTokens, cost{...}}`, compaction entries with usage; `docs/sessions.md:15`
   confirms `pi --fork <path|id>`. Residual risk = runtime mapping of our
   `--session-id` to the file and cross-version stability → live spike (T6)
   still required before R6/R7/R9 implementation.
3. **New finding (SDK surface smaller than assumed)**: `ai/run-review.py` does
   **not** import `pbatch` — it duplicates `resolve_validators`/`_run_validators`
   locally. SDK surface = `pbatch/__init__.py` exports + tests only.
   Consequence: (a) pbatch changes cannot break run-review; (b) run-review's own
   `--resume` has the same "skip without revalidation" blind spot and will NOT
   inherit R1/R2 — documented limitation + follow-up, not MVP scope.
4. **New constraint (file budgets)**: `pbatch/pipeline.py` is 876/1000 lines
   against `quality.py`'s file budget; binding gates on pbatch/ are
   `make ci` + `python quality.py --strict .` (engineering.yaml exempts pbatch/
   from the filesize/complexity/style checks, but quality.py applies). R1+R8
   must extract reuse logic into a new module (`pbatch/reuse.py`) instead of
   growing pipeline.py.
5. **Baseline**: 111 tests pass (`pytest tests/` green twice); one flaky SIGINT
   test observed once (`test_sigint_kills_process_group` — timing-sensitive;
   green in isolation and in both full reruns) → tracked as T13, not a blocker.
6. **R5 residue detection** needs a runner-side mechanism independent of pi:
   per-task start markers in a state dir (SIGKILL leaves a stale marker = the
   detection signal). Zero pi dependency.

## 1. Scope, constraints, assumptions, non-goals

**Scope**: MVP = R0–R5 (all Must worklines). Release 2 = R6–R8 (Should).
Hardening = R9–R13 (Could, each independently shippable).

**Constraints**
- Binding gates per commit: `make ci` + `python quality.py --strict .`
  (function ≤50 lines, complexity ≤15, file ≤1000 lines, dup-body detection;
  strict mode currently OK).
- `pbatch/pipeline.py` at 876/1000 lines → new logic lives in new modules
  (`reuse.py`, `triage.py`, `metering.py`, `lock.py`).
- SDK backward compatibility: `pbatch/__init__.py` exports stay
  import-compatible; `TaskResult` changes are additive-only.
- Agent invocation contract unchanged: `<bin> -p PROMPT [flags]`; session flags
  `start`/`continue` only until T10.
- Zero new *required* configuration for MVP. T3 (rotation) and T4 (lock) are
  safe to default on; T1 changes behavior only under explicit `--reuse`;
  T5 thresholds default conservative.
- New exit-code contract (document in `docs/RUNNING_247.md`):
  0 = success, 1 = max rounds reached, 3 = budget cap, 4 = stall stop,
  5 = lock held, 130 = interrupt.

**Assumptions**
- pi remains the driven stock binary; we only read its session files and pass
  its flags (read-only external contract).
- `--reuse` remains opt-in; R1 only affects runs that already pass `--reuse`.
- AI outputs remain proposals; nothing here changes evidence standards or the
  repo's own gates.
- T6 spike verdict decides R6/R7/R9 scope; parse/format failures degrade to
  invocation-count budgeting, never crash.

**Explicit non-goals** (re-affirmed against code)
- Token accounting via CLI output parsing (session usage diff supersedes).
- Resident agent reuse / daemon mode (conflicts with the process-group-kill /
  zero-orphan model).
- Tier/policy routing engine; per-agent adapter layer beyond field plumbing.
- Built-in secret scanner (registry hook is the deliverable).
- Any UI/frontend/`interfaces/web` assets; changes to pi itself or its session
  format.
- Consolidating run-review.py's duplicated validator code into pbatch
  (its independence is preserved; its resume blind spot is a documented
  follow-up).

## 2. Task table

| ID | Workline | Outcome | Files/packages | Dep | Size | Owner skill | Executable acceptance check |
|---|---|---|---|---|---|---|---|
| T0 | R0 validator-execution refactor | `run_validation()` returns (ok, exit, stdout, stderr, timed_out); `_save_validated` uses it; `revalidate_existing(path)` validates an on-disk artifact | pbatch/runner.py; tests/test_pi_batch.py | — | S | NM, Python stdlib | Existing validator tests (571–641) unchanged; new tests: failure returns stderr tail + exit code |
| T1 | R1 reuse freshness revalidation | `--reuse` re-runs the effective validators on the existing artifact; fail ⇒ regenerate (stale artifact deleted), pass ⇒ skip + log "reused+validated" | pbatch/reuse.py (new), pbatch/runner.py, pbatch/pipeline.py (4 builders + execute_stage short-circuit), pbatch/cli.py `_filter_reused`; tests | T0 | M | NM, pipeline semantics | One fixture per reuse path (cli single-batch, from_prompt, from_dir, from_outputs, aggregate, full-reuse stage): `--validate "exit 1"` ⇒ artifact regenerated; `--validate true` ⇒ skipped + "reused+validated" logged; no-validator tasks skip trivially |
| T2 | R2 validator feedback | TaskResult gains additive validation fields; retry prompt includes capped validator stderr/exit; summary surfaces the failure signature | pbatch/models.py, pbatch/runner.py; tests | T0 | S-M | NM + SDK (additive) | Fake agent echoes prompt ⇒ retry attempt's output contains validator stderr; new fields default empty (old serializers untouched); cap 4000 chars/40 lines (D4) |
| T3 | R3 log rotation | `RotatingFileHandler` replaces `FileHandler` | pbatch/cli.py, pbatch/config.py; tests | — | S | NM | Two thresholds: maxBytes=1KB/backupCount=2 ⇒ >2KB of logs ⇒ 2 backups, active file bounded |
| T4 | R4 instance lock | O_EXCL `.pi-batch.lock` (cwd), PID+start JSON, stale detection (PID dead ⇒ break; PID alive + age>24h ⇒ refuse w/ message naming holder), release in `finally`; skipped for `--dry-run` | pbatch/lock.py (new), pbatch/cli.py; tests | — | S | NM | Second process exits 5 with holder named; `kill -9` first ⇒ restart recovers; clean exit removes lock |
| T5 | R5 triage + circuit + stall + residue | Tri-state classification (transient/permanent/environment); per-task circuit (isolate after N, default 5); environment-class (identical validator stderr across ≥2 tasks) halts stage + loop; stall (K=6 default rounds, no new successes, unchanged failure set) stops loop with report + decision-log entry; residue markers warn + isolate for the round; exit 4 on stall | pbatch/triage.py (new), pbatch/runner.py (markers in `.pi-batch/` state dir, gitignored), pbatch/cli.py `_run_rounds`, pbatch/config.py; tests | T0, T2 | L | NM, concurrency/state | Three fixtures: poison task ⇒ isolated after circuit-max (no burn); missing validator binary ⇒ environment halt; no-progress pair ⇒ stall stop; all three terminate under `--max-rounds 0` within thresholds |
| T6 | R6 spike (pi session format) | Verdict report: session file location for our `--session-id` invocation, JSON pointer paths for usage/cost, per-call diff for new/shared modes, compaction detection, `--fork` behavior | docs/spike-pi-session.md (report only) | — | S | SB research, pi session format | Report contains: file glob hit, 3 verified JSON pointers from a live run, usage-diff sample, PASS/FAIL verdict per R6/R7/R9 dependency |
| T7 | R6 metering impl | Per-call usage-diff events; append-only JSONL `--events-file`; webhook hook (task_finish / task_fail / gate_reject / budget_cap / stall_stop), non-blocking, 5s timeout, failures logged only; parse failure ⇒ `usage: null` degradation, never crash | pbatch/metering.py (new), pbatch/runner.py, pbatch/config.py, pbatch/cli.py; docs/RUNNING_247.md | T6 | M | NM + SB contract | Exactly one event per task in a test run; diff matches session-file delta (spike-validated); slow-webhook fixture proves non-blocking; missing session file ⇒ event with usage:null |
| T8 | R7 budget circuit breaker | `--budget-max N` (agent-invocation count; 1.0 needs no token parsing) + `--daily-budget` (UTC, state file); on cap: stop rounds, report, decision-log entry, exit 3; budget wins over `--max-rounds 0` | pbatch/cli.py, pbatch/runner.py, pbatch/config.py; docs | T7 for token-exact; 1.0 standalone | M | NM | `--budget-max 3` + 5 tasks ⇒ exactly 3 spawns (serial + parallel), exit 3, decision-log entry; precedence over max-rounds 0 tested |
| T9 | R8 fingerprint reuse 2.0 | Opt-in `--reuse-fingerprint`; sidecar `<output>.meta.json` (frozen format per D1: version, fingerprint = sha256(canonical inputs + task def + validator spec), created_at, model/provider); reuse = exists ∧ fingerprint match ∧ non-empty; unify the ~6 `exists()` sites behind `reuse_decision()` | pbatch/reuse.py, pbatch/pipeline.py, pbatch/cli.py, pbatch/models.py; tests | T1, D1 | L | NM + SDK (sidecar format) | Input change ⇒ regenerate; unchanged ⇒ reuse; empty/corrupt artifact ⇒ regenerate; old `--reuse` semantics unchanged |
| T10 | R9 session rotation | `session_flags` gains `fork` key; watermark rotation (session file size/messages via T6) uses `--fork <file>`; compaction-entry log warning | pbatch/config.py, pbatch/runner.py, pbatch/cli.py; tests | T6 | S | NM + SB | fork flags observed in fake-agent argv at watermark; shared session keeps working after rotation |
| T11 | R10 hetero-routing | `_task_from_def` passes provider/thinking/tools/exclude_tools; stage-level `model`/`provider` for meta orchestrator + role tasks; CLI `--provider`; precedence CLI > task > stage > config default (D6) | pbatch/pipeline.py, pbatch/cli.py; tests | — | S-M | NM | Pipeline task def with provider/thinking/tools reaches argv (recording fake agent); parity with single-batch; precedence tests |
| T12 | R11 governance hardening batch | 12a validator JSON protocol (status pass/warn/fail); 12b gate reason check (`VERDICT: PASS` without reason ⇒ FAIL, fail-closed kept); 12c `approval: true` stage points (blocked on D5); 12d evidence fences in `_combine_outputs` + untrusted-evidence instruction; 12e output byte cap (default 2MB); 12f `secretscan` registry entry + docs | pbatch/runner.py, pbatch/pipeline.py, pi-batch.yaml, prompts/, docs | T0 (12a); D5 (12c) | S each (batch M) | NM, shell/validator semantics | 12a: JSON warn⇒allow + fail⇒reject fixtures; 12b: `VERDICT: PASS` w/o reason ⇒ gate FAIL; 12d: `<evidence>` fences asserted; 12e: >cap rejected/truncated per decision; 12f: `--validate secretscan` runs the grep hook |
| T13 | flaky SIGINT test hardening (Could) | Stabilize `test_sigint_kills_process_group` (signal-delivery race) | tests/test_local_fixes.py | — | XS | NM | 3 consecutive full-suite runs green |

## 3. Dependency graph / ordered critical path

```
                 ┌── T3 (R3 rotation) ──┐
                 └── T4 (R4 lock) ──────┘   (parallel group A, anytime)
T0 (R0 validator refactor)
 ├── T1 (R1 reuse revalidation) ──┐
 └── T2 (R2 feedback) ────────────┴──► T5 (R5 triage/circuit/stall) ──► MVP R1–R5
T6 (R6 spike) ──► T7 (R6 metering) ──► T8 (R7 budget)                   (release 2)
                    └──► T10 (R9 rotation)
T1 + D1 ──► T9 (R8 fingerprint reuse 2.0)
T11 (R10), T12 (R11), T13: independent hardening group
```

- **Critical path (MVP): T0 → {T1 ∥ T2} → T5.**
- Parallel group A (can start day 1): T3, T4, T6 (spike), T11, T12a/d/e/f, T13.
- Parallel group B (after T0): T1 ∥ T2.
- Release-2 chain: T6 → T7 → T8; T10 rides on T6's session-file knowledge.
- T9 waits on T1 + decision D1; T12c waits on decision D5.
- **Integration points**: `_save_validated` (T0/T1/T2/T12a all touch it — T0
  must land first), `_run_rounds` (T5/T8), `execute_stage` + the four task
  builders (T1/T9), `pbatch/__init__.py` (export changes stay additive).
- Ownership: NM (nested module) for all pbatch/ work; SDK review gate for
  T2/T9 (TaskResult fields, sidecar format); SB contract ownership only for
  T6/T7/T10.

## 4. Risk register

| # | Risk | Evidence | Trigger | Mitigation | Fallback |
|---|---|---|---|---|---|
| 1 | pi session format / `--session-id`→file mapping differs at runtime | E16/E17 doc-verified, runtime unverified | T6 spike finds mismatch | Spike first; degradation path built into T7 | R7 1.0 invocation-count budget; drop T10 |
| 2 | R1 slows or changes existing `--reuse` workflows | Reuse exists only under explicit `--reuse` (cli.py:160) | User reports slower resume / unexpected regeneration | Log "reused+validated"; no-validator tasks unaffected | `--reuse-legacy` escape hatch (added with T1) |
| 3 | Circuit/stall false positives stop legitimate 7×24 runs | `--max-rounds 0` unbounded (cli.py:98,148) | Threshold hit in a legit run | Conservative defaults (circuit 5, stall 6), all configurable, exit 4 + full report | `--no-stall`; restart with same `--reuse` continues |
| 4 | Environment-class misclassification (real content bug read as validator typo) | Unknown validator names run as raw shell (config.py `_resolve_validators`) | Identical stderr across tasks from a real bug | Require identical stderr across ≥2 distinct tasks AND same validator cmd; per-task circuit still applies | Classify as permanent per-task (no halt); operator reviews report |
| 5 | Lock false positive (shared FS, PID reuse) | Zero locking in tree (grep) | Legit second instance refused | PID + start-time + age>24h check; error names holder + lock path | Documented manual removal; `--no-lock` escape (default off) |
| 6 | Validator feedback bloats retry prompts / leaks secrets | Validator stderr not captured today (runner.py) | Prompt-size regression | 4KB/40-line cap (D4); full tail stays in logs only | Config `feedback_cap` |
| 7 | Webhook/events block the runner | None exists yet | Slow/misbehaving endpoint | Non-blocking thread, 5s timeout, failures logged; events file written first | `--no-webhook` |
| 8 | Residue-marker false positive (stale marker, task actually done) | SIGKILL leaves in-flight agent (runner.py `start_new_session`) | Marker older than timeout on reuse | Warn-only + isolate-for-round; never delete artifacts | Documented manual cleanup |
| 9 | pipeline.py hits the 1000-line quality budget | 876/1000 today | T1/T9 additions | Extract `pbatch/reuse.py` first (part of T1 design) | Further split (reuse_inputs.py) |
| 10 | SDK break for `pbatch/__init__.py` consumers | Exports enumerated in `pbatch/__init__.py` | T2/T9 export changes | Additive-only fields; run-review independence preserved | Pin exports; add import-compat test |
| 11 | Budget cap vs `--max-rounds 0` precedence ambiguity | cli.py:98,148 | User sets both | Budget wins; documented + tested in T8 | Startup warning when both set |
| 12 | Meta stages bypass reuse (orchestrator re-runs every round) | `_run_meta_stage` has no reuse handling | Cost surprise in meta pipelines | Documented; T5 circuit applies to role tasks | Future: meta reuse via T9 fingerprint |

## 5. Milestones & final gate

**M0 (commits 1–3): T0, T3, T4** — validator refactor + bounded logs +
instance lock. Deliverable: `make ci` green; lock/rotation fixtures green.

**M1 (commits 4–5): T1 ∥ T2** — gate invariant restored + feedback loop.
Deliverable: all six reuse paths revalidate (fixtures); retry prompt carries
validator stderr; `TaskResult` additive fields.

**M2 (commit 6): T5** — **MVP complete (R1–R5)**. Deliverable:
poison/environment/stall fixtures terminate under `--max-rounds 0` within
thresholds; exit-code contract documented in `docs/RUNNING_247.md`.

**M3 (release 2): T6 spike → T7 → T8** — cost governance. Deliverable:
per-call metering events; budget caps honored (serial + parallel); decision-log
trail; spike verdict recorded in `docs/spike-pi-session.md`.

**M4 (hardening): T9–T13** — each independently shippable behind `make ci`.

**Rollout checklist (per workline commit)**
1. `make ci` + `python quality.py --strict .` green.
2. New fixtures for the workline; full `pytest tests/` green twice (T13 flake).
3. Docs in the same commit: `docs/RUNNING_247.md` (flags, exit codes,
   thresholds), `README.md` command reference, `AGENTS.md` capability tables,
   `pi-batch.yaml` (secretscan entry, logging defaults).
4. Compatibility smoke: one `examples/` pipeline `--dry-run`;
   `ai/run-review.py --all --resume --dry-run`; a single-batch `-p` run.
5. New flags default off except T3/T4; T1 only under explicit `--reuse`;
   T5 conservative defaults.
6. Rollback = revert the commit. No persistent state except the opt-in T9
   sidecar (versioned) and the append-only decision log (a reverted experiment
   leaves a trace — by design).

**Final release gate**: M0–M4 committed; `make ci` green; strict quality green;
111+ tests green (3 consecutive runs); docs updated; decision log records the
release decision. Any red gate blocks release.

**Unresolved decisions** (must be settled before the flagged task starts):
D1 sidecar format freeze (before T9) · D2 budget semantics, hard-stop vs
degrade (before T8) · D3 stall/circuit defaults, proposal 5/6 (before T5) ·
D4 feedback cap, proposal 4000 chars/40 lines (before T2) · D5 `approval: true`
semantics (before T12c) · D6 routing precedence, proposal CLI > task > stage >
default (before T11) · D7 secretscan hook vs built-in scanner, proposal hook
(before T12f).
