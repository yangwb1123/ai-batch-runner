All evidence gathered. Here is the staff-engineer review.

---

# Staff Review — T0 (R0 validator-execution refactor)

## 1. Change / subsystem summary and affected contracts

**Subsystem**: `pbatch/runner.py` validator-execution internals; SDK surface (`pbatch/__init__.py`); docs (`docs/RUNNING_247.md`); regression tests.

| Contract | Status |
|---|---|
| Private `_run_validator(cmd, cwd) -> Optional[Popen]` | **Removed**; replaced by `run_validation(cmd, cwd, timeout=600) -> ValidationResult`. Grep-verified: no other callers existed in-tree. |
| SDK (`pbatch/__init__.py` `__all__`, re-exported by `pi-batch.py` `from pbatch import *`) | **Additive only**: `run_validation`, `revalidate_existing`, `ValidationResult`. Verified importable. |
| `_save_validated` observable behavior | Preserved (Verified by diff + tests): AND semantics, mkstemp→atomic-rename, fail-closed zero-artifact (temp unlinked), `VALIDATION FAILED (exit=N|timeout)` warning, 10-line stdout+stderr tails, `shlex.quote` on `{output}`/`{cwd}` (F3/F14 fixes retained), 600s hard deadline + process-group SIGKILL. |
| `ai/run-review.py` | Untouched (Verified: plan's explicit non-goal, `docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md` §1). |
| Agent invocation, flags, exit codes, retry/reuse/session semantics | Unchanged (no diff outside the 4 files). |

The refactor is faithful: the only observable difference is that a spawn **crash** now logs `(exit=-1)` with the exception text in the stderr tail, where the old code logged a misleading `(timeout)` and nothing else (see F2).

## 2. Findings (by severity)

No Critical/High/Medium findings. One design-level gap and several Low/Info items.

**F1 — Low** — `pbatch/runner.py:286` `revalidate_existing(path, validate_cmd, workdir="")`
- **Current behavior**: `workdir or os.getcwd()` silently falls back to process cwd, whereas `_save_validated` uses `task.workdir()` (`models.py:113`: `self.cwd or os.getcwd()`). A task with a custom `cwd` revalidated without an explicit workdir runs gates against the wrong tree and substitutes the wrong `{cwd}`.
- **Impact**: none today — zero production callers (unwired until T1). Footgun for the T1 wiring.
- **Minimal correction**: in T1, pass `task.workdir()` explicitly at every reuse call site; consider removing the default or asserting on it.
- **Focused test**: task with `cwd=tmp_path` → `revalidate_existing(out, 'test "$(pwd)" = <cwd>', workdir=task.workdir())` is True.

**F2 — Low (log-label drift vs. the "byte-identical" claim)** — `pbatch/runner.py:273-277`
- **Current behavior**: spawn crash → `VALIDATION FAILED (exit=-1)` + exception text tail; pre-refactor logged `(timeout)` with no tail. Timeout → `(timeout)` unchanged; non-zero exit → `(exit=N)` unchanged.
- **Impact**: benign, strictly more accurate diagnostics; artifact semantics identical (fail closed, temp deleted). The handoff's "byte-identical" wording is approximately true.
- **Correction**: none required; optionally note in the commit message.

**F3 — Low (local duplication)** — `_save_validated` (267-284) and `revalidate_existing` (295-315)
- ~20 lines of identical resolution/substitution/tail-logging structure. `quality.py` does not flag it (normalized fingerprints differ).
- **Correction (quick win)**: extract `_run_gates(path, commands, wd, label: str, fail_verb: str) -> bool` used by both; T2's feedback path will be the third consumer.
- **Focused test**: existing 8 new tests cover both paths — a pure extraction needs no new coverage.

**F4 — Info** — `pbatch/runner.py:232-234` timeout branch: after `_kill_group`, `proc.communicate()` is called but its return is **discarded**, so partial validator output is lost on timeout (`stdout=""`, `stderr=""`). Pre-existing behavior, but directly relevant to T2 (validator feedback): timeout failures will carry no signature text into retry prompts.
- **Correction**: keep the communicate result (`stdout, stderr = proc.communicate()`).

**F5 — Info (handoff factual drift)** — "runner.py is now 471": observed **474** lines by `splitlines()` (the same counting `checks/filesize.py` uses). Within the 500 budget either way.

**F6 — Info (pre-existing doc drift)** — `engineering.yaml:20` comment and `docs/CHECKS_REGISTRY.md:64` say `pbatch/pipeline.py` is "853 lines"; actual is **876** (plan §0.4 already records 876). Stale provenance comments, one-line fixes.

## 3. Gate table

Authoritative rules: `engineering.yaml` + `docs/CHECKS_REGISTRY.md` (G1–G5) and `quality.py` budgets (func ≤50 lines, complexity ≤15, file ≤1000, dup-body ≥8 stmts; doubled for tests). No threshold drift introduced; where the two gate systems overlap on `pbatch/` (filesize 500 vs quality 1000), the stricter (500) is the binding one and passes.

| Gate | Authoritative rule | Observed | Evidence | Status |
|---|---|---|---|---|
| G1 filesize | 500 lines/file; `pbatch/` subject (only `pipeline.py` ignored) | runner.py **474/500**; `checks/filesize.py pbatch/runner.py` exit 0 | ran directly | **PASS** |
| G2 complexity | cyclomatic ≤15 / cognitive ≤20 per function | `cli.py check` exit 0 (`pbatch/` exempt by policy; `quality.py` applies it anyway → OK) | ran | **PASS** |
| G3 root policy | ≤15 root files, allowed list, no `*_test.py` | exit 0 | ran | **PASS** |
| G4 style | line ≤100, trailing ws/tabs/final newline | `checks/style.py pbatch/runner.py` exit 0 | ran directly | **PASS** |
| G5 quality | func ≤50 / cx ≤15 / file ≤1000 / dup bodies | `python quality.py --strict .` → `quality: OK`, exit 0; no func >50 in runner.py (AST scan) | ran | **PASS** |
| Regression | `pytest tests/` | **118 passed** (110 baseline + 8 new), 9.56s | ran | **PASS** |
| `make ci` | check + self-test + test + quality | exit 0, **twice consecutively** | ran | **PASS** |
| Pre-commit hook | staged `.py` → filesize 500 + style | underlying gates pass on the changed files | **Partial**: hook itself not executed (nothing staged); its two component gates verified directly | **PASS (inferred)** |

## 4. Technical-debt register (verified only)

| ID | Debt | Evidence | Dependency | Priority |
|---|---|---|---|---|
| D1 | **Unwired public API**: `revalidate_existing` exported + tested, zero production callers (grep: only `__init__.py` re-export + tests). The plan's stated T1 integration point | `grep -rn revalidate_existing` | T1 (`cli.py _filter_reused`, 4 pipeline builders, `execute_stage` short-circuit) | **High — resolve next** (it is the critical path's next step) |
| D2 | **Run-review duplicates the validator contract with weaker safety**: fixed `.tmp` name (the exact symlink-precreation hazard pbatch fixed in F14), unquoted `{output}` (pbatch F3), `subprocess.run` without `start_new_session`/group-kill on timeout (orphan risk), stdout-only 5-line tail | `ai/run-review.py:538 _run_validators` vs `runner.py:217` | Plan non-goal for MVP ("its independence is preserved"); revisit post-T5/T9 | **Medium** (security-adjacent gap in a parallel tool; no live pbatch path affected) |
| D3 | Two divergent implementations of one contract (pbatch vs run-review) — drift already visible (quoting, tmp naming, kill, tail length) | F2/D2 evidence | D2's consolidation decision | Medium |
| D4 | Validator deadline hardcoded 600s default | `runner.py:217` | Plan non-goal; fine as-is | Low |
| D5 | Timeout path discards partial validator output | F4 | T2 (feedback) should decide capture semantics | Low |

## 5. Maintainability assessment

**Positive**: clean extraction — private `Popen`-returning helper replaced by a well-named public function with a structured, immutable result type that is exactly the shape T2's retry feedback needs; spawn failures fail closed with the exception text instead of `None` (strictly more diagnosable); `revalidate_existing` fail-closed semantics (missing/empty/vanished-race → False) are consistent with the runner's zero-artifact doctrine; no shared mutable state, safe under `run_parallel`'s threads; docstring quality is high and `docs/RUNNING_247.md` accurately describes the new executor (no overclaiming that `--reuse` is wired — Verified).

**Watch items**: `runner.py` is now 474/500 against the *binding* filesize gate and T1+T2 will grow it further — the plan's constraint #4 (new logic in `pbatch/reuse.py`) is essential, not optional. The 8 new tests are good: they pin the fail-closed contract (spawn crash, timeout kill, missing/empty artifact, zero temp residue) and the placeholder semantics.

**Quick wins**: (1) F3 shared `_run_gates` helper; (2) F4 capture partial timeout output; (3) F6 stale "853" comments; (4) F1 explicit `task.workdir()` in T1.

**Unresolved evidence gaps**:
- The earlier single `make ci` exit-2 anomaly: **unreproducible** — two consecutive full `make ci` runs exit 0, `pytest` 118 passed. Consistent with the handoff's assessment (plausible pytest-cache contention); T13's flaky SIGINT test did not flake in my run (single run, so not conclusive).
- Pre-commit hook execution itself unobserved (no staging performed); component gates verified directly.
- `docs/agent-os/EVALUATION.md` confirmed absent (no `agent-os/` in `docs/`); the substitution of the evidence standard from `prompts/README.md` + `AGENTS.md` is sound — both are committed and authoritative in this tree.
- `--reuse` revalidation remains **untested end-to-end** (by design, T1); only the building block is unit-tested.

**Verdict**: approve the refactor as-is. It is faithful to the plan's T0 acceptance check ("existing validator tests unchanged; new tests: failure returns stderr tail + exit code"), satisfies the stricter of every overlapping gate, and introduces no new runtime risk. The only mandatory follow-through is T1 wiring `revalidate_existing` before it accrues SDK-compatibility obligations, and keeping `runner.py` growth out of the 500-line budget via `pbatch/reuse.py`.
