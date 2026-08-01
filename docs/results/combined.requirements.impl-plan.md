# Implementation Plan — 7×24 Governance Release

## 0. Verification summary (what I checked, what changed)

All 15 "Verified" evidence items from the proposal hold against the code. Corrections/elevations found during verification:

1. **Citation correction**: `max_iterations` default 3 lives in `pbatch/pipeline.py::_parse_stage_def`, not `config.py:67` (config.py has no such constant). The fact itself is verified.
2. **E16/E17 elevated → verified against installed pi docs**: `docs/session-format.md` confirms JSONL sessions at `~/.pi/agent/sessions/--<path>--/<timestamp>_<uuid>.jsonl` (workdir with `/`→`-`), per-message `usage {totalTokens, cost{...}}`, compaction entries with usage; `docs/sessions.md:15` confirms `pi --fork <path|id>`. Residual risk = runtime `--session-id`→file mapping + version stability → live spike (T6) still required.
3. **New finding (SDK surface smaller than assumed)**: `ai/run-review.py` does **not** import `pbatch` — it duplicates `resolve_validators`/`_run_validators` locally. So pbatch changes can't break it; but its own `--resume` has the same skip-without-revalidation blind spot and won't inherit R1/R2 → documented limitation + follow-up, not MVP scope.
4. **New constraint (file budgets)**: `pbatch/pipeline.py` is 876/1000 lines against `quality.py`'s budget (`make ci` + `python quality.py --strict .` are the binding gates on pbatch/). R1+R8 must extract reuse logic into a new `pbatch/reuse.py` module instead of growing pipeline.py.
5. **Baseline**: 111 tests green (twice); one flaky SIGINT test observed once (`test_sigint_kills_process_group` — timing race; green in isolation) → T13 hardening, not a blocker.
6. R5's residue detection gets a runner-side mechanism with zero pi dependency: per-task start markers in a `.pi-batch/` state dir (SIGKILL leaves a stale marker = the detection signal).

## 1. Scope, constraints, assumptions, non-goals

**Scope**: MVP = R0–R5 (Must). Release 2 = R6–R8 (Should). Hardening = R9–R13 (Could, independently shippable).

**Constraints**
- Per-commit gates: `make ci` + `python quality.py --strict .` (fn ≤50 lines, complexity ≤15, file ≤1000 lines; strict currently OK).
- `pbatch/pipeline.py` 876/1000 → new logic in new modules (`reuse.py`, `triage.py`, `metering.py`, `lock.py`).
- SDK backward compat: `pbatch/__init__.py` exports stay import-compatible; `TaskResult` changes additive-only.
- Agent invocation contract unchanged; session flags `start`/`continue` only until T10.
- New exit-code contract (documented in `RUNNING_247.md`): 0 ok · 1 max-rounds · 3 budget cap · 4 stall · 5 lock held · 130 interrupt.

**Assumptions**: pi remains the driven stock binary (read-only external contract); `--reuse` stays opt-in (R1 only affects explicit `--reuse`); T6 spike verdict decides R6/R7/R9 scope, with degradation (never crash) to invocation-count budgeting.

**Non-goals** (re-affirmed): CLI-output token accounting; resident agent/daemon; tier/routing engine; built-in secret scanner (hook only); any UI/frontend; changes to pi or its session format; consolidating run-review's duplicated validator code into pbatch.

## 2. Task table

| ID | Workline | Outcome | Files | Dep | Size | Owner | Executable acceptance check |
|---|---|---|---|---|---|---|---|
| T0 | R0 validator refactor | `run_validation()` returns (ok, exit, stdout, stderr, timed_out); `_save_validated` uses it; `revalidate_existing(path)` for on-disk artifacts | pbatch/runner.py, tests | — | S | NM Python | Existing validator tests (571–641) unchanged; failure returns stderr tail + exit |
| T1 | R1 reuse revalidation | `--reuse` re-runs effective validators on existing artifact; fail ⇒ regenerate (delete stale), pass ⇒ skip + log "reused+validated" | pbatch/reuse.py (new), runner.py, pipeline.py (4 builders + `execute_stage` short-circuit), cli.py `_filter_reused`, tests | T0 | M | NM pipeline | Fixture per reuse path (cli single-batch, from_prompt, from_dir, from_outputs, aggregate, full-reuse stage): `--validate "exit 1"` ⇒ regenerated; `--validate true` ⇒ skipped + logged |
| T2 | R2 validator feedback | TaskResult gains additive validation fields; retry prompt includes capped validator stderr/exit; summary shows signature | models.py, runner.py, tests | T0 | S-M | NM+SDK | Fake agent echoes prompt ⇒ retry output contains validator stderr; fields default empty; cap 4000 chars/40 lines (D4) |
| T3 | R3 log rotation | `RotatingFileHandler` | cli.py, config.py, tests | — | S | NM | maxBytes=1KB/backupCount=2 ⇒ >2KB logs ⇒ 2 backups, file bounded |
| T4 | R4 instance lock | O_EXCL `.pi-batch.lock` (cwd), PID+start JSON, stale detection (PID dead ⇒ break; alive+age>24h ⇒ refuse naming holder), release in `finally`; skip `--dry-run` | pbatch/lock.py (new), cli.py, tests | — | S | NM | Second process exits 5 with holder named; `kill -9` then restart recovers; clean exit removes |
| T5 | R5 triage+circuit+stall+residue | Tri-state classification; per-task circuit (isolate after N=5); environment-class (identical validator stderr ≥2 tasks) halts stage+loop; stall (K=6 rounds, no new successes, unchanged failures) stops loop w/ report+decision-log, exit 4; residue markers warn+isolate | pbatch/triage.py (new), runner.py (markers in `.pi-batch/`, gitignored), cli.py `_run_rounds`, config.py, tests | T0, T2 | L | NM concurrency | Poison ⇒ isolated, no burn; missing validator binary ⇒ environment halt; no-progress ⇒ stall; all three terminate under `--max-rounds 0` |
| T6 | R6 spike | Verdict report: session-file location for our `--session-id`, JSON pointers for usage/cost, per-call diff (new/shared), compaction detection, `--fork` behavior | docs/spike-pi-session.md only | — | S | SB research | Report: file glob hit, 3 verified JSON pointers from live run, diff sample, PASS/FAIL per dependency |
| T7 | R6 metering | Per-call usage-diff events; append-only JSONL `--events-file`; webhook (task_finish/task_fail/gate_reject/budget_cap/stall_stop), non-blocking, 5s timeout; parse failure ⇒ `usage:null` | pbatch/metering.py (new), runner.py, config.py, cli.py, docs | T6 | M | NM+SB | One event per task; diff matches session deltas; slow-webhook fixture proves non-blocking |
| T8 | R7 budget | `--budget-max N` (invocations; 1.0 needs no token parsing) + `--daily-budget` (UTC state file); cap ⇒ stop, report, decision-log, exit 3; budget wins over `--max-rounds 0` | cli.py, runner.py, config.py, docs | T7 (token-exact; 1.0 standalone) | M | NM | `--budget-max 3` + 5 tasks ⇒ 3 spawns (serial+parallel), exit 3, log entry |
| T9 | R8 fingerprint reuse 2.0 | Opt-in `--reuse-fingerprint`; sidecar `<output>.meta.json` (frozen format D1: version, sha256(inputs+task def+validator spec), created_at, model/provider); reuse = exists ∧ match ∧ non-empty; unify ~6 `exists()` sites behind `reuse_decision()` | reuse.py, pipeline.py, cli.py, models.py, tests | T1, D1 | L | NM+SDK | Input change ⇒ regenerate; unchanged ⇒ reuse; empty/corrupt ⇒ regenerate; old `--reuse` untouched |
| T10 | R9 session rotation | `session_flags` gains `fork` key; watermark rotation via `--fork <file>`; compaction-entry warning | config.py, runner.py, cli.py, tests | T6 | S | NM+SB | fork flags in fake-agent argv at watermark; shared session survives rotation |
| T11 | R10 hetero-routing | `_task_from_def` passes provider/thinking/tools/exclude_tools; stage-level model/provider for meta tasks; CLI `--provider`; precedence CLI > task > stage > default (D6) | pipeline.py, cli.py, tests | — | S-M | NM | Pipeline def with those fields reaches argv (recording fake agent); parity + precedence tests |
| T12 | R11 governance batch | 12a validator JSON protocol (pass/warn/fail) · 12b gate reason check (`VERDICT: PASS` w/o reason ⇒ FAIL) · 12c `approval: true` (blocked on D5) · 12d evidence fences in `_combine_outputs` + untrusted-data instruction · 12e output byte cap (2MB) · 12f `secretscan` registry entry | runner.py, pipeline.py, pi-batch.yaml, prompts/, docs | T0 (12a), D5 (12c) | S each | NM shell/validators | 12a JSON warn/fail fixtures; 12b reason-less PASS ⇒ gate FAIL; 12d fences asserted; 12f `--validate secretscan` runs |
| T13 | flaky SIGINT test (Could) | Stabilize `test_sigint_kills_process_group` | tests/test_local_fixes.py | — | XS | NM | 3 consecutive full-suite runs green |

## 3. Dependency graph / ordered critical path

```
            ┌── T3 (R3) ──┐
            └── T4 (R4) ──┘        (parallel group A, anytime)
T0 (R0)
 ├── T1 (R1) ──┐
 └── T2 (R2) ──┴──► T5 (R5) ──► MVP R1–R5
T6 (spike) ──► T7 (R6) ──► T8 (R7)            (release 2)
                 └──► T10 (R9)
T1 + D1 ──► T9 (R8)
T11, T12, T13: independent hardening group
```

- **Critical path (MVP): T0 → {T1 ∥ T2} → T5.**
- Parallel group A (day 1): T3, T4, T6 (spike), T11, T12a/d/e/f, T13.
- **Integration points**: `_save_validated` (T0/T1/T2/T12a all touch it — T0 lands first), `_run_rounds` (T5/T8), `execute_stage` + the four builders (T1/T9), `pbatch/__init__.py` (additive only).
- Ownership: NM for all pbatch/ work; SDK review gate for T2/T9; SB contract only for T6/T7/T10.

## 4. Risk register

| # | Risk | Evidence | Trigger | Mitigation | Fallback |
|---|---|---|---|---|---|
| 1 | pi session format/`--session-id` mapping differs at runtime | E16/E17 doc-verified, runtime unverified | T6 spike finds mismatch | Spike first; degradation built into T7 | Invocation-count budget (R7 1.0); drop T10 |
| 2 | R1 slows/changes existing `--reuse` workflows | Reuse only under explicit `--reuse` (cli.py:160) | Slower resume / unexpected regeneration | Log "reused+validated"; no-validator tasks unaffected | `--reuse-legacy` escape (added with T1) |
| 3 | Circuit/stall false positives stop legit 7×24 runs | `--max-rounds 0` unbounded | Threshold hit in legit run | Conservative defaults (5/6), configurable, exit 4 + report | `--no-stall`; restart with `--reuse` continues |
| 4 | Environment misclassification (real bug as validator typo) | Raw-shell fallback (config.py) | Identical stderr across tasks from real bug | Require identical stderr ≥2 tasks + same validator cmd; per-task circuit still applies | Classify permanent per-task (no halt) |
| 5 | Lock false positive (shared FS, PID reuse) | Zero locking in tree | Legit second instance refused | PID + start-time + age>24h; error names holder | Documented removal; `--no-lock` (off default) |
| 6 | Feedback bloats retry prompts / leaks secrets | Stderr not captured today | Prompt-size regression | 4KB/40-line cap (D4); full tail in logs only | Config `feedback_cap` |
| 7 | Webhook blocks runner | None exists | Slow endpoint | Non-blocking thread, 5s timeout, log-only failures; file written first | `--no-webhook` |
| 8 | Residue-marker false positive | SIGKILL leaves in-flight agent | Marker older than timeout on reuse | Warn-only + isolate-for-round; never delete artifacts | Documented manual cleanup |
| 9 | pipeline.py hits 1000-line budget | 876/1000 today | T1/T9 additions | Extract `pbatch/reuse.py` first (in T1) | Further split |
| 10 | SDK break for `pbatch/__init__.py` consumers | Exports enumerated | T2/T9 export changes | Additive-only; run-review independence preserved | Pin exports; import-compat test |
| 11 | Budget vs `--max-rounds 0` precedence | cli.py:98,148 | Both set | Budget wins; tested in T8 | Startup warning |
| 12 | Meta stages bypass reuse | `_run_meta_stage` has no reuse handling | Cost surprise | Documented; T5 circuit applies to role tasks | Meta reuse via T9 fingerprint |

## 5. Milestones & final gate

- **M0** (commits 1–3): T0, T3, T4 — refactor + bounded logs + instance lock. `make ci` green.
- **M1** (commits 4–5): T1 ∥ T2 — gate invariant restored + feedback loop; six reuse-path fixtures green.
- **M2** (commit 6): T5 — **MVP = R1–R5**. Poison/environment/stall fixtures terminate under `--max-rounds 0`; exit-code contract documented.
- **M3** (release 2): T6 spike → T7 → T8 — cost governance; spike verdict in `docs/spike-pi-session.md`.
- **M4**: T9–T13 hardening, each independently shippable.

**Rollout checklist (per commit)**: `make ci` + `python quality.py --strict .` green; new fixtures + full `pytest tests/` twice; docs in the same commit (`RUNNING_247.md`, `README.md`, `AGENTS.md`, `pi-batch.yaml`); compatibility smoke (`examples/` pipeline `--dry-run`, `run-review.py --all --resume --dry-run`, single-batch `-p`); new flags default off except T3/T4; rollback = revert commit (only persistent state: opt-in T9 sidecar, versioned; decision log append-only by design).

**Final release gate**: M0–M4 committed; all gates green; 111+ tests green 3×; docs updated; release decision recorded in the decision log. Any red gate blocks release.

**Unresolved decisions** (owner input before the flagged task): D1 sidecar format freeze (T9) · D2 budget semantics hard-stop vs degrade (T8) · D3 stall/circuit defaults, proposed 5/6 (T5) · D4 feedback cap, proposed 4KB/40 lines (T2) · D5 `approval: true` semantics (T12c) · D6 routing precedence, proposed CLI > task > stage > default (T11) · D7 secretscan hook vs scanner, proposed hook (T12f).

Full plan saved to `docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md`.
