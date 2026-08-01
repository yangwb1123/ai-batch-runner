All verification complete. Here is the architecture review.

---

# Architecture Review — 7×24 Governance Release Proposal

**Reviewer role**: senior architect (advisory only; no files modified)
**Inputs read**: `prompts/README.md` (evidence standard applied), `AGENTS.md`, proposal text, all of `pbatch/` (config/models/runner/pipeline/cli), `ai/run-review.py`, `pi-batch.yaml`, `Makefile`, installed pi docs (`session-format.md`, `sessions.md`, `compaction.md`), expansion rounds in `docs/requirements/`
**Checks run this revision**: `python -m pytest tests/` (110 passed), `python quality.py --strict .` (OK), grep-based symbol/line verification of all 15 cited "Verified" claims
**Note**: `docs/architecture/DIRECTORY_MAP.md` (cited as canonical in `prompts/README.md`) does **not exist** in this tree — I substituted direct code inspection.

## 1. Scope, Assumptions, Verified Architecture Summary

**Scope assessed**: the proposal's 11 worklines (R1–R11), its evidence table, ownership model, MVP boundary, and compatibility claims. I did not re-litigate the five expansion rounds' convergence (expansion-5 confirms ~95% overlap; the roadmap is stable and the delta trend supports "execute now").

**Architecture summary (verified)**:

| Layer | Components | Verified state |
|---|---|---|
| Entry | `pi-batch.py` (thin shim), `pbatch/cli.py` (parser, round loop, dispatch) | Round loop `_run_rounds` (cli.py:144–182); stdin consumed once (cli.py:166) |
| Orchestration | `pbatch/pipeline.py` (stage builders, meta, gates, decision log, archive) | Meta bounded at 3 iterations (models.py:31, pipeline.py:67 — *not* config.py:67 as cited); gate fail-closed (pipeline.py:624, 789) |
| Execution | `pbatch/runner.py` (spawn, failure signatures, retries, validators, summary) | Process-group kill (runner.py:107, 125); single retryable regex (runner.py:194); parallel has no retries (runner.py:376) |
| Config | `pbatch/config.py` (yaml lookup, session flags, validator registry) | Unknown validator names fall through to raw shell (config.py:105–112) |
| Parallel consumer | `ai/run-review.py` | **Standalone re-implementation, zero `pbatch` imports** (verified: grep count = 0) |

**Verification verdict on the proposal's evidence**: all 15 "Verified" claims check out against code (line citations accurate to ±2 lines; one citation error, §F7). Both "Proposed" pi-contract claims (R6 usage fields, R9 `--fork`) are now **confirmed in the installed pi docs** — `session-format.md` defines `AssistantMessage.usage {totalTokens, cost.total}` and `sessions.md:15` documents `pi --fork <path|id>`; auto-compaction exists (`compaction.md`). They can be upgraded from "Proposed" to "doc-Verified"; the remaining unknown is format stability across pi versions (§5).

**Assumptions accepted**: stock pi as read-only external contract; `make ci` + `quality.py --strict` as the release gate; conservative default-off for cost/scheduling-affecting behavior; advisory status of the proposal itself.

## 2. Findings Table

| # | Severity | Finding | Evidence | Impact | Recommendation |
|---|---|---|---|---|---|
| F1 | **High** | Proposal's SDK ownership model is factually wrong, and R1's scope misses a fourth reuse path | `ai/run-review.py` imports **nothing** from `pbatch` (grep count 0); it duplicates `_AGENT_ERROR_PATTERNS` (line 374), `agent_failure_reason` (419), `resolve_validators` (256), session-flag loading (207–222). Its `--resume` skips stages by output existence only (lines 631, 733) — the exact gate-bypass R1 fixes, untouched by R1's "pipeline.py, cli.py reuse paths" scope. Proposal claims "SDK = pbatch (imported by ai/run-review.py)" — not so | R1 lands in `pbatch`, but `run-review --resume` still promotes unvalidated artifacts; the "gate invariant" success indicator is not met tool-wide; error-signature/validator/session logic drifts in two copies (R2/R5/R11 touch these) | Amend R1 scope: revalidate on resume in `ai/run-review.py` too, or (better, §3 Option B) refactor run-review onto the shared runner before/with R1. At minimum fix the ownership-key text and add a run-review resume test |
| F2 | **High** | Reuse bypasses *two* gates, not one: the full-reuse short-circuit `return [], True` (pipeline.py:449–451) also skips `_run_stage_commands` — documented as "a failure gate for the stage" (pipeline.py:418) | Verified code path: `execute_stage` returns before `_run_stage_commands` | A resumed stage's command hooks (e.g. repo-scope gates, generation checks) silently never run again | R1 must re-run stage `commands` on full reuse (or explicitly document commands as first-write-only and assert it); acceptance: a test where a stage with `commands:` is fully reused and the command re-executes |
| F3 | **Medium** | Meta stages have **no reuse path at all** — orchestrator + roles re-run every round of any failing multi-round pipeline | `_run_meta_stage` (pipeline.py:226) takes no `reuse`; `_build_role_tasks` writes unconditionally | Cost exposure the proposal's R8 ("unify ~6 exists() sites") undercounts: it's 7+ sites including a missing one; also means R1/R5's "resume only re-runs failures" promise is false for meta stages | Add meta-stage reuse (skip roles whose output exists and passes revalidation) to R8 scope; state it explicitly in R1 acceptance |
| F4 | **Medium** | Proposal E5's framing is incomplete: retries exist **only** in single-batch serial | `_execute_stage_tasks` calls `run_serial(tasks, retries=0, …)` (pipeline.py:500); meta role tasks and `run_parallel` have none | The round loop is the *only* retry mechanism in pipeline mode; R5's circuit breaker must be specified against the round-loop level (in-process state is fine — rounds share a process, but note breaker state resets on restart, which is acceptable and should be documented) | R5 spec: classify at task level, count at round level; document the "no retries in pipeline/parallel/meta — rounds are the retry" asymmetry as a stated invariant, not an accident |
| F5 | **Low** | E10 is subtly imprecise: with `--max-rounds 0` the loop **exits when all tasks pass** (`_run_rounds` returns on `not round_failed`) — it does not spin on success | Verified cli.py:144–182 | The unbounded vectors are the external `for i in …` wrapper (documented rolling pattern) and poison-task spin — both still covered by R7/R5; only the wording is wrong | Re-word E10: "rolling analysis is unbounded by operator choice (`for` loop); `--max-rounds 0` is unbounded only under persistent failure" |
| F6 | **Low** | Decision log grows unboundedly in 7×24 (append per stage per round, `_append_decision_log`) | pipeline.py:781–802 | R3 rotates only `--log-file`; a months-long run accumulates a large DECISIONS.md | Add a line to R3: rotate/roll the decision log too (or accept and document; it is append-only by design) |
| F7 | **Low** | Citation error: `max_iterations` default 3 is at **models.py:31 / pipeline.py:67**, not config.py:67 (which is `_DEFAULT_SESSION_FLAGS`) | Verified | None (substance confirmed); evidence hygiene only | Fix citation |
| F8 | Info | Gate fail-closed **survives reuse** — `_gate_verdict` re-reads the artifact (pipeline.py:657) | Verified | Positive: R1 must preserve this (do not "revalidate" a gate stage into re-running the agent) | Note in R1 spec: gate verdicts are re-read from artifacts; only `validate_cmd`/`commands` re-run |
| F9 | Info | R6/R9 external contract confirmed at doc level: `usage.totalTokens`/`cost.total` per assistant message; `pi --fork`; auto-compaction | pi docs `session-format.md:87–115`, `sessions.md:15`, `compaction.md` | Spike risk downgraded: format exists; stability across pi versions remains the open question | Spike becomes "read real session files from this tool's own runs and diff"; also verify pi writes session files when **no `--session-id` is passed** (default `--session-mode new` passes none — the file must be located by workdir+timestamp scan) |
| F10 | Info | Doc drift: AGENTS.md claims 91 tests and a project `.pi/settings.json`; actual: 110 tests collected, no `.pi/` in this tree | Verified | None functional | Refresh docs when the release lands |

**Confirmed as proposed (no new finding)**: E1/E2 field plumbing gap (`_task_from_def` pipeline.py:96–113), E3 reuse gate bypass (449–451 + 146/352/381/424 + cli.py:375), E4 raw-shell validators, E6 no instance lock (tree-wide grep: zero hits), E7 parallel no retries (as far as it goes, see F4), E9 unbounded log, E11/E12 contract risk, E13 `_combine_outputs` bare concat (pipeline.py:113), E15 stdin fix (cli.py:166). R2's "validator failure invisible to retry" is verified — `_save_validated` logs the tail but `TaskResult.reason` becomes only `"validation failed"` and the retry prompt is byte-identical.

## 3. Decision Options

| Option | Description | Trade-offs | Verdict |
|---|---|---|---|
| **A — Adopt MVP as proposed, amended** | R1–R5 with amended scope: R1 += `ai/run-review.py` resume revalidation + stage `commands` re-run; R8 += meta-stage reuse; R5 specified at round level (F4); R3 += decision-log rotation (F6) | Small scope growth (~1 extra workline-equivalent, all inside existing worklines); keeps isolated-commit rollout; no new architecture | **Preferred.** The amendments are corrections, not expansions — they are required for the proposal's own success indicators ("gate invariant", "compat") to be true |
| B — Option A + unification refactor | Before R1, refactor `ai/run-review.py` onto `pbatch.runner` (delete duplicated error patterns/validators/session logic) | Removes drift risk permanently; but a refactor touching a 20-test consumer expands blast radius at the very commit meant to be the smallest; conflicts with "zero change to the existing agent/reporting contract" | Rejected for M1; **adopt as its own workline (R12, Could)** after R2 — it is the natural home for R11's per-agent signature generalization, which the proposal already defers |
| C — R1–R4 only, defer R5 | Ship freshness/feedback/rotation/lock, defer triage+circuit+stall | Cheaper now; but leaves the headline 7×24 promise (poison spin, `--max-rounds 0`) unaddressed and R5 depends on R2's capture which is already in MVP | Rejected |

## 4. Prioritized Implementation Sequence

Milestones are the proposal's own ordering (R1→R5 independent; R5 needs R2), amended per F1–F6. Every commit: `make ci` + `python quality.py --strict .` green.

| Milestone | Workline | Key risks | Executable acceptance checks |
|---|---|---|---|
| **M0 (spike, parallel to M1–M3)** | R6 metering basis: read pi session JSONL from this tool's own runs; diff usage per call | Format stability; locating files for default (no `--session-id`) sessions | Spike report: usage diff matches `AssistantMessage.usage` deltas for ≥3 real runs, serial + parallel + shared; parse failure path defined (degrade to attempts/rounds, never crash) |
| **M1** | **R1 amended** (F1, F2): revalidate existing artifacts on `--reuse` (pbatch paths + run-review `--resume` + stage `commands`); failure ⇒ regenerate, pass ⇒ "reused+validated" | Behavior change on opt-in path only; gate verdict must still be re-read, not re-run (F8) | New tests: single-batch (cli.py:375), all three pipeline reuse paths (pipeline.py:146/352/381/424), full-reuse-with-commands, run-review `--resume`; assert: no artifact promoted without a current validation pass (grep-able invariant, indicator #1) |
| **M2** | **R2**: capture validator exit/stderr into `TaskResult` (additive field, default — old serializers unaffected); inject摘要 into retry prompt; surface in summary | Prompt bloat (cap size — open decision §5); both `_save_validated` and run-review's `_run_validators` copies (F1) | Test: retry prompt contains validator stderr excerpt; field default keeps existing construction sites green (all use kwargs) |
| **M3** | **R3 + R4**: `RotatingFileHandler`; O_EXCL lock + stale recovery (PID liveness or age) | Stale-lock false positives on PID reuse | Tests: two rotation sizes; second-instance fail-fast in clean/crash/stale paths; lock released on exit |
| **M4** | **R5**: tri-state classification (transient/permanent/environment), task circuit, batch stall (K rounds, no progress) — specified at round level per F4 | Threshold tuning (conservative defaults); state resets on restart (document); environment-class must match on stable signatures (same exit+stderr across tasks/rounds, per E4) | Fixture tests per class; `--max-rounds 0` + poison task terminates via circuit/stall within thresholds; decision-log entries for each isolation event |
| **M5** | **R7 + R8** (needs M0): `--budget-max` attempts/rounds first, token-exact after spike; fingerprint `--reuse` 2.0 + sidecar, **including meta-stage reuse** (F3); distinct exit code on budget cap | Sidecar format freeze (open decision §5.1); R8's unify covers 7+ sites | Tests: caps honored serial+parallel; exit-code contract documented; empty/corrupt artifact never reused; meta role outputs skip when unchanged |
| **M6** | **R9–R11 + R12 (new)**: session fork/rotation (now doc-confirmed, small); hetero-routing plumbing; governance hardening batch (judge validator on gate `validate_cmd`, approval points, evidence fences, byte cap, `secretscan` registry hook); unify run-review onto `pbatch` | Each independently shippable; R12 absorbs F1 duplication permanently | Per-workline tests; indicator #7 (examples + run-review run unchanged) re-checked after R12 |

**Compatibility plan**: no flag removed; new flags default off except R3/R4; R1 only alters the opt-in `--reuse` path; `TaskResult`/`Task` additions additive; sidecar opt-in and versioned; decision log append-only (reverted experiments leave traces — feature, not bug).

## 5. Unknowns Needing Owner/Product Decisions

The proposal's seven unresolved decisions (§5 of the proposal) all stand and remain open. Additions from this review:

1. **R12 (run-review unification)**: approve the refactor workline? (I recommend yes, after R2; it is the prerequisite for any per-agent signature generalization.)
2. **Meta-stage reuse semantics** (F3): default-on or opt-in for M5? Affects existing pipelines' behavior on resume.
3. **Stage `commands` re-run on reuse** (F2): re-run always (correct, slower) vs. mark commands first-write-only (fast, weaker invariant) — needs the maintainer's call; I lean re-run, consistent with R1.
4. **pi session-format stability**: no version pin exists in this tool; decide whether M5's token-exact budget hard-depends on the format or treats it as best-effort with attempts/rounds fallback (proposal already leans the latter — confirm).
5. **Default-session metering** (F9): pi writes session files per workdir even without `--session-id` — must be confirmed in the spike; if not, R6 requires always passing `--session-id`, which *is* a (minor) change to the agent invocation contract and needs sign-off.

**Bottom line**: the proposal is the strongest artifact of the five rounds — every load-bearing claim verified, scope honestly bounded, MVP correctly shaped. It is approvable as advisory direction with the F1–F6 amendments: R1 must cover `ai/run-review.py` and stage `commands`, R8 must cover meta stages, R5 must be specified at round level, and the ownership key's "SDK" claim should be corrected (run-review is a parallel implementation, not a consumer — which is itself the root cause of F1 and the best argument for the R12 unification workline).
