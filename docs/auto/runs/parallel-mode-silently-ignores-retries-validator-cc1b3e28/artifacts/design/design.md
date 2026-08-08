# Design: Parallel mode retries, validator-feedback retry, and `--min-interval`

Direction: *Parallel mode silently ignores --retries, validator-feedback retry, and --min-interval*
Module: `pbatch` · Status: PROPOSED (design, not yet implemented)

## 0. Evidence verification status

All 5 cited symbols were checked live against the repo (commit b552e10):

| Claim | Verified? | Actual |
|---|---|---|
| `runner.py:886` `run_parallel(tasks, workers=0, validate_cmd="")`, no retry/throttle params, empty body 886–935 | ✅ | Signature exact; body has pre-submit budget check, `_run_one` worker, collector loop, `SystemExit(3)` on cap. No retry/throttle logic anywhere. |
| `cli_batch.py:488` `_execute_batch`; parallel call at **497** drops all four flags; serial branch 492–495 passes them | ✅ | Serial: `run_serial(..., retries=args.retries, retry_delay=args.retry_delay, backoff=args.retry_backoff, min_interval=args.min_interval, ...)`. Parallel: `run_parallel(tasks, args.workers, validate_cmd=cli_validate)` — all four dropped. |
| `runner.py:837` `run_serial` retry loop 867–879 (`_retry_wait` + `_retry_task_with_feedback`), `min_interval` sleep 881–882 | ✅ | Retry loop: `_retry_wait` at 869, `_retry_task_with_feedback` at 876, `time.sleep(wait)` at 877, re-run via `_run_with_gate` at 879; `min_interval` sleep at 881–882. |
| `runner.py:367` `_retry_wait` with 30–60 s rate-limit jitter; only caller is serial | ✅ | `_RETRYABLE_REASON` regex; rate-limit class gets `max(wait, 30.0) + uniform(0, min(wait, 30.0))`, others ±50 %. Sole production caller: `runner.py:869` (serial loop). |
| `runner.py:645` `_retry_task_with_feedback`; only caller is serial | ✅ | Capped stderr (4000 chars / 40 lines) + `exit={validation_exit}` appended to prompt via `replace(task, ...)`. Sole production caller: `runner.py:876`. |
| `pipeline.py:559` / `campaign_analysis.py:149` positional `run_parallel` calls | ✅ | `run_parallel(tasks, stage.workers, validate_cmd=stage_validate)` and `run_parallel(pending, jobs)` — both positional, safe under appended keyword params. |
| Two "(serial mode)" help strings in `cli.py` | ✅ (one is literal, one paraphrase) | `cli.py:158` `--retries` ends "(serial mode)"; `cli.py:164` `--min-interval` says "successful **serial tasks**". Both need rewording. |

Supporting facts confirmed: `run_task` raises `SystemExit(3)` from `budget_try_consume()` (runner.py:111–118); `_run_with_gate(task, validate_cmd, index, total, session_flags=None, parallel=False, session_name="")` calls `record_task(result)` on every run — so per-attempt recording is automatic per `_run_with_gate` call; parallel rejects shared/per-stage sessions at `cli.py:369–372`; `_flaky_agent`/`_recording_agent`/`load_batch` test helpers already exist in `tests/test_pi_batch.py`.

## 1. Problem statement

In `--mode parallel` the four CLI flags `--retries / --retry-delay / --retry-backoff / --min-interval` are accepted but silently ignored (`cli_batch.py:497`). Consequences:

1. A flaky agent failure in a parallel batch is final for that round — the round loop must re-run everything, wasting budget on tasks that already passed.
2. Validator-feedback retry (T2 machinery: capped stderr fed into the retry prompt) is unavailable in parallel — a gate rejection cannot self-heal.
3. `--min-interval` throttling for 24×7 runs is unavailable in parallel; a long-running parallel batch can hammer the provider.

## 2. API changes

### 2.1 `run_parallel` signature (R1) — `pbatch/runner.py:886`

```python
def run_parallel(tasks: list[Task], workers: int = 0, validate_cmd: str = "",
                 retries: int = 0, retry_delay: float = 10.0, backoff: float = 2.0,
                 min_interval: float = 0.0) -> list[TaskResult]:
```

- Defaults **identical to `run_serial`** (`retries=0, retry_delay=10.0, backoff=2.0, min_interval=0.0`).
- New params appended **after** `validate_cmd` with defaults → all three positional call sites unchanged (R5).
- Docstring extended to describe per-task retry + completion throttle.

### 2.2 Per-task retry loop in the worker (R2)

Rewrite `_run_one` inside `run_parallel`:

```python
def _run_one(task: Task, index: int) -> Optional[TaskResult]:
    for attempt in range(retries + 1):
        try:
            result = _run_with_gate(task, validate_cmd, index, total, parallel=True)
        except SystemExit as e:
            # T8: budget cap trips mid-attempt (incl. mid-retry); translate
            # to the None marker the collector understands. Never retry
            # past a cap — the cap means stop everything.
            if e.code == 3:
                return None
            raise
        if result.success or attempt >= retries:
            return result
        wait = _retry_wait(result, attempt + 1, retry_delay, backoff)
        log.warning("RETRY %d/%d for task [%d/%d] in %.0fs (reason: %s)",
                    attempt + 1, retries, index, total, wait,
                    result.reason or f"exit {result.returncode}")
        time.sleep(wait)
        if result.validation_stderr:
            task = _retry_task_with_feedback(task, result)
    return result
```

Preserved contracts (R2):
- **Reuse, no reimplementation**: `_retry_wait` + `_retry_task_with_feedback` called verbatim. `_retry_wait`'s 30–60 s jitter was explicitly designed for parallel herds (its docstring says so) — reusing it is the fix, not a new risk.
- **`parallel=True` on every attempt**: `record_task` inside `_run_with_gate` fires per attempt → per-attempt recording preserved automatically.
- **Budget-cap contract**: `SystemExit(3) → None` per attempt; a cap abandons further attempts for that task (correct semantics — a cap means "stop consuming budget").
- **Zero-residue**: final failure leaves no output (existing `_save_validated` gate path unchanged); `result.reason` from the last attempt is preserved.

### 2.3 `min_interval` completion throttle (R3)

Collector loop (main thread), after the `result is None` cap check:

```python
successes = 0
...
for fut in as_completed(fut_map):
    result = fut.result()
    if result is None:
        capped = True
        ...existing cap handling...
    else:
        if result.success:
            if successes > 0 and min_interval > 0:
                time.sleep(min_interval)
            successes += 1
        results.append(result)
        log.info("PROGRESS: %d/%d done", len(results), total)
```

- Throttle sits **between successful completions**: first success undelayed; k successes → exactly k−1 sleeps of `min_interval`.
- **Testable discriminator**: 2 successful tasks with `min_interval=N` → total recorded sleep ∈ **[N, 2N)**. This distinguishes "between completions" (N) from "sleep after every success" (2N) and "no throttle" (0).
- Sleeps in the main collector thread only — worker threads keep running agents; the throttle never occupies a pool slot.
- Cap handling runs **before** the sleep on the success path? No — cap is detected only via `result is None` futures; a success-path sleep can delay processing of an already-arrived None future by at most `min_interval` (bounded, success-path only). Documented as accepted.
- **Divergence from serial noted**: serial sleeps after *every* success including the first (runner.py:881–882). The parallel spec deliberately defines between-completions semantics (first undelayed). `run_serial` is untouched; the asymmetry is intentional and pinned by T3a.

### 2.4 CLI wiring (R4) — `pbatch/cli_batch.py:497`

```python
results = run_parallel(tasks, args.workers, validate_cmd=cli_validate,
                       retries=args.retries, retry_delay=args.retry_delay,
                       backoff=args.retry_backoff, min_interval=args.min_interval)
```

Help strings (`pbatch/cli.py`):
- 158 `--retries`: drop "(serial mode)" → "Retry failed tasks up to N extra attempts with exponential backoff".
- 164 `--min-interval`: "Minimum seconds between successful tasks (throttle for 24x7 runs)".

### 2.5 Explicitly unchanged (R5 / non-goals)

- `pipeline.py:559` and `campaign_analysis.py:149` — `run_parallel` positional calls now resolve `retries=0` default; pipeline parallel-stage retry semantics are exactly today's (stage failure handled by round/gate machinery). No edits.
- `run_serial` — untouched.
- `cli.py:369–372` shared/per-stage session rejection in parallel — untouched; the parallel retry loop stays sessionless (consistent with today's `_run_one`).
- Budget/metering (`metering.py`, `kill_active_procs`) — untouched.
- No new session flags, no new config keys, no `pi-batch.yaml` schema change.

## 3. Compatibility constraints

1. **Signature**: new params appended after `validate_cmd` with defaults → all positional callers (`pipeline.py:559`, `campaign_analysis.py:149`, `cli_batch.py:497`) and any external `run_parallel(t, w, v)` callers are source- and behavior-compatible.
2. **Behavior parity with serial**: identical helpers, identical defaults, identical log format/level (`RETRY n/N for task [i/total]`); `retries=0` and/or `min_interval=0` are exact no-ops (current behavior byte-for-byte).
3. **Thread-safety**: `_retry_wait` uses `random.uniform` (CPython random is thread-safe per call); `_retry_task_with_feedback` is a pure `dataclasses.replace`. No shared mutable state added. Feedback accumulates across attempts exactly as in serial (each retry appends to the already-feedbacked prompt).
4. **Line budget**: `runner.py` is currently **969 lines**; the change adds ≈28–30 lines → ≈997–999, under the `quality.py --strict` budget of 1000 but with **zero headroom**. If the implementation runs over, extract the collector loop or `_run_one` helper before adding code (do not exceed 1000).
5. **Test infrastructure**: `test_campaign.py:246` monkeypatches `campaign_analysis.run_parallel` with `fake_parallel` — signature stays compatible (its fake must accept the new kwargs or the test needs `**kwargs`; existing fake called with 2 positional args → unaffected).
6. **CLI flag surface unchanged**: no new flags; the four existing flags simply become effective in parallel mode.

## 4. Failure modes

| # | Mode | Behavior | Rationale / mitigation |
|---|---|---|---|
| FM1 | Budget cap trips during a retry attempt | `SystemExit(3)` raised inside `run_task` → caught per attempt → `None` → collector sets `capped` → `kill_active_procs()` → final `raise SystemExit(3)` | Per-attempt catch preserves the T8 contract; no retry past a cap (a cap means stop consuming). |
| FM2 | Cap trips in one worker while others are mid-retry-sleep | Others' next `run_task` call raises `SystemExit(3)` → `None`; collector stops appending; `kill_active_procs` kills agent procs (sleeps are bounded, ≤ `retry_delay*backoff^retries` + 30–60 s jitter) | Bounded by the current attempt; same exposure as serial's retry loop under a cap. |
| FM3 | Rate-limit herd re-sync | All workers fail with `rate|429|quota` → `_retry_wait` floor 30 s + uniform(0, min(wait,30)) spreads retries across 30–60 s | This is exactly the scenario `_retry_wait` was built for (docstring); reuse, don't reimplement. |
| FM4 | Collector sleeps on success path while a `None` future arrives | Cap handling delayed ≤ `min_interval` | Bounded, success-path only; accepted and documented in docstring. |
| FM5 | Validator stderr feedback grows across attempts | Prompt grows by ≤ 4000 chars + 40 lines per retry (capped per attempt) | Same accumulation semantics as serial; final prompt still under `prompt.max_bytes` gate (retries are small integers). |
| FM6 | Interleaved RETRY log lines in parallel | Concurrent warnings interleave | Pre-existing condition for all parallel logs (PROGRESS lines); no new hazard. |
| FM7 | `workers=0` / `min_interval` with all-failed batch | `min_interval` never sleeps (only successes counted) | Matches "between successful completions" semantics; serial parity on the success-only condition. |
| FM8 | Race: retry loop + `kill_active_procs` | `_run_with_gate` → `run_task` spawns agent, records into `_ACTIVE_PROCS`; kill only affects in-flight agents | Pre-existing T8 mechanism; no new interaction surface (retry adds no new process handling). |

## 5. Migration steps

No data migration, no config migration (no new keys). Sequencing:

1. **Edit `pbatch/runner.py`**: new `run_parallel` signature (2.1); rewrite `_run_one` with the retry loop (2.2); collector throttle (2.3). Re-check line count ≤ 1000 (`python quality.py --strict .`).
2. **Edit `pbatch/cli_batch.py:497`**: pass the four flags.
3. **Edit `pbatch/cli.py:158,164`**: reword the two help strings.
4. **Add tests** to `tests/test_pi_batch.py` per §6 (reuse `_flaky_agent`, `_recording_agent`, `load_batch`, `monkeypatch.setattr(mod.time, "sleep", recorder)`).
5. **Verify**: `python -m pytest tests/test_pi_batch.py` → `make ci` → `python quality.py --strict .`.
6. **Docs**: update the two help strings only; AGENTS.md §2.2/§2.11 already describe retries without a serial qualifier. If `docs/RUNNING_247.md` or `docs/AUTOMATION_WORKFLOW_SUMMARY.md` claim retries are serial-only, adjust one line each (check during implementation).
7. **Release note**: `--retries/--min-interval` now effective in `--mode parallel`; behavior otherwise unchanged.

## 6. Testable acceptance mapping

| Acceptance | Test (file: `tests/test_pi_batch.py`) | Pass condition |
|---|---|---|
| **T1a** unit parity: flaky agent + `--mode parallel --retries 2` → PASS | `test_parallel_retry_recovers_after_transient_failure` — `_flaky_agent(fail_times=1)`, `run_parallel([Task(prompt="x", output=o)], workers=2, retries=2, retry_delay=0)`, `time.sleep` monkeypatched to no-op | `results[0].success is True`, `o.exists()`, counter shows 2 invocations (1 + 1 retry) |
| **T1a'** exhaust path | `test_parallel_retry_exhausts` — `fail_times=999` | `success is False`, `reason` set, `not o.exists()`, counter == 3 (1 + 2 retries) |
| **T1b** CLI wiring | `test_cli_parallel_retries` — subprocess `pi-batch.py tasks.yaml --mode parallel --retries 2 --agent-bin <flaky>` | rc == 0, output written |
| **T2** validator stderr + `exit=3` in retry prompt | `test_parallel_retry_prompt_carries_validator_stderr` — `_recording_agent` + `args_log`; validator: first run `exit 3`, second run `exit 0`; `retries=1` | args_log's second invocation prompt contains the validator stderr text and the literal `exit=3`; output saved |
| **T3a** deterministic throttle unit | `test_parallel_min_interval_between_completions` — 2 success tasks, `min_interval=5`, sleep recorder list | recorded sleeps == `[5.0]` (exactly one; first success undelayed) ⇒ total ∈ [5, 10) |
| **T3a'** 3-success discriminator | same recorder, 3 tasks | sleeps == `[5.0, 5.0]` ⇒ total ∈ [10, 15) |
| **T3b** real-interval CLI | `test_cli_parallel_min_interval` — subprocess, `--mode parallel --min-interval 1`, 2 quick tasks | wall clock ≥ 1.0 s (and < 10 s so the suite stays fast) |
| **T4** gates | `make ci` + `python quality.py --strict .` | all green; `runner.py` ≤ 1000 lines |
| **R5 regression** | existing `tests/test_campaign.py::test_analysis_retry_removes_invalid_artifact` (fake `run_parallel`), `test_pi_batch.py::test_shared_session_parallel_rejected`, budget-cap parallel tests | all still pass unmodified (proves pipeline/campaign call sites and cap contract untouched) |

## 7. Non-goals (explicit)

- Pipeline parallel-stage retries (`pipeline.py:559` stays `retries=0`; stage retry remains round/gate machinery's job).
- Campaign-analysis parallel retries (`campaign_analysis.py:149` stays default).
- Shared/per-stage session continuity inside parallel retries (rejected at CLI already).
- Changing serial `min_interval` semantics (first-success delay stays as-is in serial).
- Any changes to budget metering, `kill_active_procs`, or validator plumbing.
