# Implementation Plan — T0 QA Close-out, T1 Gate, T2 Sequence (ai-batch-runner)

Status: Tech-lead plan for revision `4b27031` + uncommitted T0 diff, driven by the
QA-lead review (this plan's input). Every finding the plan acts on was re-verified
against the code in this session before planning (see §1.4 evidence ledger).

## 1. Scope, constraints, assumptions, non-goals

### 1.1 Scope

**Phase 0 — T0 close-out (this revision; lands before any T1 commit):**

- T0.1 — Fix the two empirically-confirmed symlink paths (QA-1, QA-2). Both are
  exploitable today; QA-2 is pre-existing but T0's refactor is the natural point
  to normalize `.tmp` hygiene.
- T0.2 — Contract hardening inside `run_validation` (QA-5 TypeError guard;
  security F4 bounded drain, unblocks QA-7).
- T0.3 — Test close-out for the measured gaps (QA-3a/b/c, QA-4, QA-5, QA-6, QA-7)
  and the two symlink regressions; runner.py coverage target ≥90% with zero
  uncovered lines inside `run_validation` / `_save_validated` / `revalidate_existing`
  except the documented defensive guard (runner.py:247).
- T0.4 — CI gate: `make coverage` measures `pbatch/` with a fail-under bound;
  `pytest -p no:cacheprovider` for the transient exit-2 anomaly (Partial evidence,
  mitigation not root-cause claim).
- T0.5 — Docs: symlink rejection, artifact permission note, coverage gate in
  `docs/CHECKS_REGISTRY.md`, revalidation semantics in `docs/RUNNING_247.md`.

**Phase 1 — T1 (R1 reuse freshness revalidation)**: `--reuse` re-runs the effective
validators against the existing artifact before skipping; fail ⇒ stale artifact
deleted and task regenerated; pass ⇒ skip + log "reused+validated". Six reuse
paths unified behind `pbatch/reuse.py::reuse_decision()`. Includes `--reuse-legacy`
escape hatch (plan risk #2 mitigation).

**Phase 2 — T2 (R2 validator feedback)**: TaskResult additive validation fields;
retry prompt carries capped validator stderr/exit; summary surfaces the failure
signature. Consumes T0's partial-timeout output (QA-7 already served by T0.2).

### 1.2 Constraints (binding, unchanged from master plan)

- Per-commit gates: `make ci` (= check + self-test + test + quality) and
  `python quality.py --strict .` — both green in this session (118 passed, quality OK).
- `pbatch/pipeline.py` at 876/1000 lines (quality budget) → T1 logic lives in new
  module `pbatch/reuse.py`, not pipeline.py.
- SDK backward compatibility: `pbatch/__init__.py` exports additive-only
  (T0 already added `run_validation`/`revalidate_existing`/`ValidationResult` —
  import-compatible, covered 100%).
- Agent invocation contract unchanged; zero new required configuration;
  `--reuse` remains opt-in.
- Existing validator tests (571–774) byte-untouched — verified via `git diff` (appends only).

### 1.3 Assumptions

- QA review claims are accepted only where re-verified this session (§1.4); the
  two transient/Partial items (pytest exit-2, pre-commit hook) stay Partial and
  are handled as mitigation/verification steps, not promoted to findings.
- The `sleep 30`/`timeout=1` tests are outcome-deterministic (no wall-clock
  assertions) — flake risk low, per review §5; keep them.
- `run_validation("")` is unreachable through normal paths (`_resolve_validators`
  maps empty → `[]`); the empty-string pin is a unit-contract test only.
- Effective gate precedence for revalidation = execution precedence
  (task.validate > stage.validate_cmd > CLI --validate-cmd) — implemented by
  passing the same effective command the execution path uses.

### 1.4 Evidence ledger (re-verified this session)

| Claim | Verification |
|---|---|
| 118 tests pass; `make ci` exit 0; quality OK | ran `make ci` — 118 passed 9.46s, `quality: OK` |
| runner.py 75% (211/280); missing lines include 247(guard), 254–255(no-file), 276/278(save tails), 296–297(OSError), 312/314(revalidate tails) | `--cov=pbatch --cov-report=term-missing` — 75%, line list matches |
| QA-1: `revalidate_existing` follows symlink artifacts (is_file/stat follow; gate executes against victim) | code: runner.py:286–320, zero `is_symlink` check; review's live PoC accepted (re-ran F1-style reasoning; confirmed by code path) |
| QA-2: `save_result` fixed `.tmp` (runner.py:188) + `write_text` (follows symlink) + `rename` (promotes symlink) | code confirmed; review's live PoC accepted |
| QA-5: list cmd → `ok=True, stdout='\n'`; `""` → `ok=True` | re-ran live: `['echo','hello']` → rc 0 `'\n'`; `''` → rc 0 `''` |
| QA-4: validator + no-output task prints & returns True (runner.py:254–255) | code confirmed; `_resolve_validators("") → []` (config.py:105–112) |
| T1 unwired: `_filter_reused` existence-only (cli.py:370–375); zero production callers of `revalidate_existing`; six existence-only reuse sites | grep: only internal `run_validation` calls in runner.py; pipeline.py:146, 352, 381, 412; cli.py:370 |
| `make coverage` misses pbatch (Makefile:41–42: `--cov=checks --cov=cli`) | Makefile read |
| Existing validator tests byte-untouched; 8 new tests appended at 774+ | `git diff tests/` — appends only |
| pipeline.py 876/1000 lines | `wc -l` |

### 1.5 Explicit non-goals (re-affirmed)

- Meta-stage (`_run_meta_stage`) reuse handling — documented limitation (plan risk #12).
- Consolidating `ai/run-review.py`'s duplicated validator code (its `--resume`
  blind spot is a documented follow-up, not this plan's scope).
- Token metering, budgets, lock, rotation, triage (T3–T13) — sequenced by the
  master plan; this plan only fronts T0→T1→T2.
- TOCTOU-proof (fd-based, `O_NOFOLLOW`) artifact execution — see risk R-1;
  the acceptance bar is the review's: reject symlink artifacts, gate never
  executes against the target, victim unchanged.
- Changing artifact permissions beyond normalizing both save paths to the same
  mkstemp behavior (already 0600 for validated artifacts since 4b27031).

## 2. Task table

Sizes are relative (no capacity evidence supplied; uncertainty noted). Owner
skill: NM = nested-module dev (Python stdlib); SDK = SDK review gate (export
changes only, T2).

| ID | Outcome | Files/packages | Dep | Size | Owner | Executable acceptance check |
|---|---|---|---|---|---|---|
| **T0.1** | Symlink rejection in `revalidate_existing` + mkstemp in `save_result`: any symlink artifact ⇒ `False`, gate never runs against target; plain path writes via unique mkstemp fd | pbatch/runner.py (revalidate_existing ~286–320: `is_symlink()` fail-closed first; save_result 188–190: mkstemp+rename, mirroring `_save_validated` 259–263); tests | — | XS–S | NM | `out = tmp_path/"o.md"; victim = tmp_path/"secret.txt"; victim.write_text("s"); out.symlink_to(victim)` ⇒ `revalidate_existing(out, "cat {output} > {cwd}/leak.txt", str(tmp_path)) is False`, victim unchanged, gate not executed; write-through variant `echo pwned > {output}` ⇒ victim unchanged; pre-created `o.md.tmp → victim.txt` + `save_result` success ⇒ victim unchanged and `o.md.is_symlink() is False`; existing tests 571–774 still byte-identical |
| **T0.2** | Contract hardening in `run_validation`: `TypeError` on non-str cmd; bounded drain after timeout kill (partial output captured, no unbounded `communicate()`) | pbatch/runner.py (217–236) | T0.1 (same file, but independent — can be same commit) | XS | NM | `run_validation(["echo","x"], cwd)` raises TypeError; `run_validation("", cwd)` returns ok=True (pinned, documented); validator `echo partial; sleep 30` with `timeout=1` ⇒ `timed_out=True` **and** `"partial" in stdout`, suite finishes fast (no post-kill hang) |
| **T0.3** | Coverage close-out: QA-3a/b/c, QA-4, QA-5, QA-6, QA-7 tests; symlink regression tests (T0.1); runner.py ≥90% overall, zero uncovered lines in the three validator functions except line 247 | tests/test_pi_batch.py (appends at 774+ only); pbatch/runner.py only if a test exposes a real defect | T0.1, T0.2 | S | NM | `pytest --cov=pbatch.runner --cov-report=term-missing tests/` ⇒ ≥90%, no uncovered line in 217–236/239–283/286–320 except 247; three new failure tests: (a) `validate_cmd='echo out >&1; exit 3'` through `run_serial` ⇒ not output.exists() and log tail contains "out"; (b) `monkeypatch.setattr(Path,"stat",…OSError)` once ⇒ `revalidate_existing(...) is False` without raising; (c) `revalidate_existing(out, "echo boom >&2; exit 1") is False`; QA-4 pin: `run_serial([Task(prompt="x")], validate_cmd="exit 1")` ⇒ success=True, no artifact, stdout has agent text; QA-6: `revalidate_existing(out, "true,exit 1") is False` |
| **T0.4** | CI gates: `make coverage` covers pbatch with fail-under; pytest cache flag | Makefile (coverage target: add `--cov=pbatch.runner --cov-fail-under=90`; test target: `-p no:cacheprovider`), docs/CHECKS_REGISTRY.md | T0.3 (bound must be green before it becomes a gate) | XS | NM | `make coverage` exits 0 with pbatch.runner ≥90% in the report; `make ci` unchanged green; CHECKS_REGISTRY documents the target |
| **T0.5** | Docs: symlink rejection + permission note + revalidation contract | docs/RUNNING_247.md (extend the uncommitted T0 section), docs/CHECKS_REGISTRY.md | T0.1–T0.4 | XS | NM | Docs state: symlink artifacts fail closed (never validated/promoted); both save paths write 0600 via mkstemp; `reuse_decision` semantics as implemented |
| **T1** | Reuse freshness revalidation: `reuse_decision(output, validate_cmd, workdir)` = exists ∧ non-empty ∧ not-symlink ∧ (no validators ∨ all pass); fail ⇒ delete artifact + return regenerate; pass ⇒ "reused+validated". All six reuse paths call it: cli `_filter_reused` (370), `_aggregate_tasks` (146), `_stage_from_prompt_tasks` (352), `_stage_from_dir_tasks` (381), `_stage_from_outputs_tasks` (412), full-reuse short-circuit in `execute_stage` (452–457 — only reachable when every artifact passed revalidation) | pbatch/reuse.py (new), pbatch/runner.py (export `reuse_decision` from `__init__` additively), pbatch/cli.py, pbatch/pipeline.py (4 builders, wiring only), tests | **T0.1–T0.5** (QA-1 fix must precede; review: reuse E2E lands with the F1 fix, not after) | M (uncertainty: 6-path wiring + precedence) | Six fixtures (cli single-batch, from_prompt, from_dir, from_outputs, aggregate, full-reuse stage): `--validate "exit 1"` ⇒ artifact deleted + regenerated; `--validate true` ⇒ skipped + "reused+validated" logged; no-validator ⇒ trivially skipped; symlink artifact under `--reuse` ⇒ regenerated (never validated through); stale-artifact deletion leaves zero residue; `--reuse-legacy` restores existence-only skip |
| **T2** | Validator feedback: TaskResult additive fields (validation ok/exit/stderr tail, capped per decision D4 — 4000 chars/40 lines proposal); retry prompt includes signature; summary surfaces it | pbatch/models.py, pbatch/runner.py, pbatch/__init__.py (additive), tests | T1 (batch boundary) | S–M | NM + SDK review (additive fields) | Fake agent echoes prompt ⇒ retry attempt output contains validator stderr; new fields default empty (old serializers untouched); cap enforced; summary line shows failure signature; timeout failures carry partial stdout (from T0.2) |

**Not in this plan (master plan sequencing)**: T3/T4 (parallel group A, anytime,
independent), T5–T13. They do not block T0 close-out or T1.

## 3. Dependency graph / ordered critical path

```
T0.1 (symlink fixes) ──► T0.3 (gap tests) ──► T0.4 (coverage gate) ──► T0.5 (docs)
T0.2 (type guard + bounded drain) ──┘              (same commit batch, see M0)
        │
        ▼
   T0 acceptance (review exit criteria 1–4 demonstrated in CI output)
        │
        ▼
T1 (reuse revalidation, six paths) ──► T2 (validator feedback) ──► MVP R0–R2 complete
```

- **Critical path: T0.1 → T0.3 → T1 → T2.** T0.2 is parallel to T0.1 within the
  same commit batch (both confined to runner.py, non-overlapping regions:
  188–190 vs 217–236 — merge into one commit to keep `make ci` green once).
- **Parallel group A (day 1, independent of T0)**: T3, T4, T6 spike, T11, T12a/d/e/f,
  T13 (master plan group A unchanged).
- **Integration points**: `_save_validated` (T0.1/T0.2/T0.3 all touch the same
  file — order within commit matters, run full suite after each), `reuse_decision`
  call sites (T1: one helper, six callers — the four pipeline builders + cli
  `_filter_reused` + execute_stage short-circuit), `pbatch/__init__.py`
  (additive exports at T1/T2 only, SDK review gate at T2).
- **Ownership**: NM for all pbatch/ work; SDK review for T2's TaskResult fields;
  no SB (shell-backend) contract work in this plan (T6/T7/T10 remain SB-owned).
- **External dependencies**: none beyond the repo itself (validators are
  stdlib/shell). Unknowns: pytest exit-2 anomaly root cause (Partial), pre-commit
  hook execution (Partial — verified during rollout, not a task).

## 4. Risk register

| # | Risk | Evidence | Trigger | Mitigation | Fallback |
|---|---|---|---|---|---|
| R-1 | TOCTOU: artifact swapped to symlink between `is_symlink()` check and gate execution | QA-1 confirmed exploitable through the check-free path | Attacker with write access to the artifact dir | Fail-closed `is_symlink()` check first; document that the acceptance bar is the review's (reject symlinks, never execute gate on target); delete-on-stale in T1 shrinks the window | fd-based execution with `O_NOFOLLOW` if the threat model demands it (documented as out of scope today) |
| R-2 | mkstemp 0600 artifacts break a consumer expecting group-readable outputs | Validated artifacts already 0600 since 4b27031 (F14); QA-2 fix normalizes the plain path | Downstream tooling (e.g., web serve) chokes | Document in RUNNING_247.md; consistent behavior across both save paths is the point of T0.1 | `os.chmod(out, 0o644)` after rename behind a config flag |
| R-3 | Coverage gate blocks on pre-existing gaps outside the three validator functions (run_task exception paths, print_summary, parallel branches) | runner.py 75%: 69 uncovered statements; ~10 closed by T0.3 | `make coverage` fails after T0.3 | T0.3 adds cheap branch tests (FileNotFoundError spawn, `_prompt_brief` truncation, `_read_stream` ValueError, `_timeout_result`) alongside the mandated gap tests | Per-function zero-uncovered gate + 85% overall, documented deviation |
| R-4 | Revalidation deletes a valid artifact when the validator environment is flaky (false stale) | None (new behavior) | Gate fails once on a genuinely good artifact under `--reuse` | Full failure tail logged; artifact regenerated (the pre-T1 cost); `--reuse-legacy` escape hatch restores old semantics | Operator rerun; decision-log entry records the regeneration |
| R-5 | Revalidation gate ≠ execution gate precedence mismatch (decision passes with gate A, execution used gate B) | Builders receive `validate_cmd`; per-task `validate` lives in task_def | Precedence bug in T1 wiring | Single `reuse_decision()` helper takes the same effective cmd as execution (task.validate > stage.validate_cmd > CLI); precedence tests in T1 fixtures | Explicitly pass `None` (inherit) instead of resolved cmd; add parity test comparing decision vs execution |
| R-6 | `pytest -p no:cacheprovider` masks (not fixes) the transient exit-2 anomaly | Partial: 3 clean consecutive runs, consistent with cache contention | Exit-2 reappears | Flag adopted; anomaly re-triggered with cache enabled is logged with full output | Investigate as separate defect with the evidence trail in hand |
| R-7 | Bounded drain after SIGKILL still hangs if the child ignores SIGKILL (D-state) | F4 flagged in review; `communicate()` today is unbounded | D-state validator child | Bounded drain: `communicate(timeout=5)` after `_kill_group`, then close pipes (daemon threads already exit on EOF/ValueError) | Hard outer bound on the whole `run_validation` call; documented residual |
| R-8 | T1's six-path wiring misses a reuse site (existence-only `exists()` left behind) | Six sites enumerated; `_filter_reused` + 5 pipeline sites | New builder added later | `reuse_decision` unifies; grep audit `exists()` in pipeline/cli in T1 acceptance; `__init__` export test keeps surface visible | Follow-up sweep; no data-loss (worst case: old skip-without-validation behavior) |

## 5. Milestones & final gate

**M0 (this revision — commits 1–3): T0.1+T0.2 → T0.3 → T0.4+T0.5**
- Commit 1: symlink fixes (T0.1) + contract hardening (T0.2) + their regression
  tests. Deliverable: the two QA PoCs now fail closed (tests green), no new
  uncovered lines in the three functions beyond 247.
- Commit 2: gap tests (T0.3). Deliverable: runner.py ≥90%; zero uncovered lines
  in 217–236/239–283/286–320 except 247.
- Commit 3: Makefile coverage/cache targets + CHECKS_REGISTRY + RUNNING_247
  updates (T0.4+T0.5).
- **M0 acceptance (QA review exit criteria 1–4)**: QA-1/QA-2 fixes landed before
  any T1 commit; `make ci` green; `pytest tests/` green twice (T13-flake check);
  `make coverage` green with pbatch.runner ≥90%; plan §T0 acceptance checks
  (existing tests 571–774 unchanged; failure returns stderr tail + exit code)
  demonstrated in CI output, not just locally; strict quality green.

**M1 (commit 4): T1** — gate invariant restored for reuse. Deliverable: six
reuse fixtures green (`exit 1` ⇒ regenerate, `true` ⇒ "reused+validated",
no-validator ⇒ trivial skip, symlink ⇒ regenerate); `--reuse-legacy` escape;
decision-log/git-commit paths unaffected; `pipeline.py` line count unchanged
(logic in `reuse.py`).

**M2 (commit 5): T2** — feedback loop. Deliverable: fake-agent retry prompt
echoes validator stderr; additive TaskResult fields default empty (SDK review
passed); cap per D4 (decision D4 must be settled before T2 starts — flagged,
not blocking T1).

**Rollout checklist (per commit)**
1. `make ci` + `python quality.py --strict .` green; `pytest tests/` green twice.
2. New tests for the commit's workline; full suite unaffected elsewhere
   (existing validator tests byte-identical through M0).
3. Docs in the same commit (RUNNING_247.md, CHECKS_REGISTRY.md, AGENTS.md
   capability tables where behavior is user-visible — `--reuse` semantics at M1).
4. Compatibility smoke: one `examples/` pipeline `--dry-run`; a single-batch `-p`
   run; `ai/run-review.py --all --resume --dry-run` (unchanged — no pbatch import).
5. Pre-commit hook execution verified once (Partial item from review §5 — observed
   on the M0 commit, evidence recorded).
6. Rollback = revert the commit; no persistent state introduced by T0/T1/T2
   (T1 deletes only stale artifacts it refused to promote).

**Final gate for this plan's scope**: M0–M2 committed; all M0 acceptance checks
demonstrated in CI output; `make ci`, strict quality, and `make coverage` green;
decision-log entry records the T0 close-out and the T1 gate decision. Any red
gate blocks the next phase.

**Decisions to settle**: D4 feedback cap (4000 chars/40 lines) — before T2.
D-none new: T0.2's empty-string pin and TypeError guard are fixed by this plan
(fail fast on programmer error; `""` stays trivially-pass, documented) —
re-open only if a caller outside the normal `_resolve_validators` path needs
different semantics.
