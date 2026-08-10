Spec complete and verified. Summary of the deliverable:

**Output**: `docs/auto/runs/parallel-mode-silently-ignores-retries-validator-cc1b3e28/artifacts/requirements-10762e10/requirements.md` (228 lines)

**Evidence verification** — all 5 cited symbols confirmed live against the repo:
- `runner.py:886` — `run_parallel(tasks, workers=0, validate_cmd="")`, no retry/throttle params, nothing in body (886–935)
- `cli_batch.py:488` — `_execute_batch` def; the parallel call at **line 497** drops all four flags (serial branch at 492–495 passes them)
- `runner.py:837` — `run_serial` retry loop at 867–879 (`_retry_wait` + `_retry_task_with_feedback`) and `min_interval` sleep at 881–882
- `runner.py:367` — `_retry_wait` with 30–60s rate-limit jitter; only caller is serial
- `runner.py:645` — `_retry_task_with_feedback`; only caller is serial

**Requirements** (scope-confined to this direction):
- **R1** — `run_parallel` gains `retries/retry_delay/backoff/min_interval` with defaults identical to `run_serial`
- **R2** — per-task retry loop inside the worker, **reusing** `_retry_wait` + `_retry_task_with_feedback` (no reimplementation), preserving `parallel=True`, per-attempt recording, and the budget-cap `SystemExit(3)` → `None` contract
- **R3** — `min_interval` throttle between successful completions (first success undelayed, total-sleep-in-`[N, 2N)` discriminator)
- **R4** — CLI wiring at `cli_batch.py:497` + fix the two "(serial mode)" help strings in `cli.py`
- **R5** — `pipeline.py:559` / `campaign_analysis.py:149` unchanged (backward-compat defaults)

**Acceptance preserved & made testable**: T1a/T1b (flaky agent + `--mode parallel --retries 2` → PASS, unit parity + CLI wiring), T2 (validator stderr + `exit=3` asserted in retry prompt via args_log), T3a/T3b (`min-interval` throttle, deterministic sleep-recorder unit test + small real-interval CLI test), T4 (`make ci` + `quality.py --strict`). Explicit non-goals list the other two directions in the analysis file and pre-existing machinery that must not change.
