Everything is in place. The pre-commit hook gates staged `.py` files at 500 lines (`runner.py` is now 471 — fine). I'll leave commits to the maintainer's workflow.

## Handoff

### 1. Outcome and behavioral changes

Implemented **T0 (R0 validator-execution refactor)** — the first commit of the 7×24 Governance Release plan's critical path (`T0 → {T1 ∥ T2} → T5`), the integration point that T1/T2/T12a all build on.

- **`run_validation(cmd, cwd, timeout=600)`** (new, replaces private `_run_validator`): runs a validator in its own process group with the same 600s hard-deadline/kill behavior, but now returns a structured `ValidationResult` named tuple `(ok, exit_code, stdout, stderr, timed_out)` instead of a `Popen` (or `None`). Spawn crashes fail closed with the exception text in `stderr`, never raising.
- **`_save_validated`** refactored onto `run_validation` with **byte-identical observable behavior**: same AND semantics, same temp-file→atomic-rename lifecycle, same "VALIDATION FAILED (exit=N|timeout)" warning, same 10-line stdout/stderr tail logging, same zero-artifact guarantee on failure.
- **`revalidate_existing(path, validate_cmd, workdir)`** (new): runs the effective gates against an already-written artifact — missing/empty artifacts fail closed, no validators ⇒ trivially reusable, `{output}`/`{cwd}` substitution preserved. This is the building block for T1's `--reuse` revalidation (per the plan, wiring into `--reuse` is T1, not this commit).
- **SDK**: `pbatch/__init__.py` exports add `run_validation`, `revalidate_existing`, `ValidationResult` (additive only; `ai/run-review.py` untouched — its independence is a stated plan non-goal).
- **Docs**: `docs/RUNNING_247.md` gained a paragraph documenting the centralized executor and `revalidate_existing` semantics.

No flags, config, exit codes, or agent-invocation contracts changed. No behavior change outside the validator-execution internals (failure signatures, gates, retries, reuse, sessions all behave as before).

### 2. Modified files

| File | Purpose |
|---|---|
| `pbatch/runner.py` | Extracted validator execution into `run_validation()` returning `(ok, exit, stdout, stderr, timed_out)`; `_save_validated` uses it; added `revalidate_existing()` for on-disk artifact revalidation |
| `pbatch/__init__.py` | Additive exports: `run_validation`, `revalidate_existing`, `ValidationResult` |
| `tests/test_pi_batch.py` | 8 new tests (below); existing validator tests untouched |
| `docs/RUNNING_247.md` | Documents the centralized validator executor + revalidation building block |

### 3. Tests and checks actually run

- `pytest tests/` — **118 passed** (110 baseline + 8 new: success/exit+stderr capture/spawn-crash/timeout-kill for `run_validation`; pass, placeholder, fail-closed missing/empty for `revalidate_existing`; no-temp-residue on gate failure through the refactored `_save_validated`). Existing validator tests (571–646 region) unchanged and green.
- `make ci` — **exit 0** (run twice; one transient non-zero observed once mid-session, unreproducible — each step `cli.py check` / `check-test` / `pytest` / `quality.py` passed individually and `make ci` passed twice consecutively afterward).
- `python quality.py --strict .` — **OK** (`runner.py` 471/1000 lines, all functions under budgets).

### 4. Remaining limitations, risks, unverified assumptions

- **Not yet wired**: `revalidate_existing` is exported and tested but not yet called by `--reuse` — that is T1 (reuse revalidation in `cli.py _filter_reused`, pipeline's four builders + `execute_stage` short-circuit, and the documented `ai/run-review.py --resume` blind spot, which is a plan non-goal for MVP). Until T1, `--reuse` still skips by output existence only.
- **`docs/agent-os/EVALUATION.md`** (cited in the task prompt) does not exist in this tree; I substituted the evidence standard from `prompts/README.md` and `AGENTS.md`, per the arch review's own note about `DIRECTORY_MAP.md`.
- **Timeout window**: validator deadline remains hardcoded 600s (matching prior behavior); making it configurable is not in the plan.
- **Unverified**: the earlier single `make ci` exit-2 anomaly — likely a transient (possibly pytest cache contention from my parallel invocation); not reproducible across three subsequent full runs. T13's known flaky SIGINT test did not flake in these runs.
- **Pre-existing untracked files** (`docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md`, `docs/requirements/`, `docs/results/`, `docs/full-sdlc-impl.yaml`) are planning-round artifacts from prior sessions; left untouched.
