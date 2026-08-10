Design written to `docs/auto/runs/parallel-mode-silently-ignores-retries-validator-cc1b3e28/artifacts/design/design.md`. Summary:

## Evidence verification — all 5 claims confirmed live (commit b552e10)

| Claim | Verdict |
|---|---|
| `runner.py:886` `run_parallel(tasks, workers=0, validate_cmd="")`, empty body | ✅ exact; body 886–935 has only budget-cap + collector |
| `cli_batch.py:488`/`497`: parallel call drops all four flags, serial passes them | ✅ serial 492–495 passes `retries/retry_delay/backoff/min_interval`; parallel drops all |
| `runner.py:837` serial retry loop 867–879 + `min_interval` sleep 881–882 | ✅ `_retry_wait`@869, `_retry_task_with_feedback`@876 |
| `runner.py:367` `_retry_wait` 30–60 s jitter, serial-only caller | ✅ sole prod caller is 869; jitter docstring was written for parallel herds |
| `runner.py:645` `_retry_task_with_feedback`, serial-only caller | ✅ sole prod caller is 876 |
| `pipeline.py:559` / `campaign_analysis.py:149` positional calls | ✅ both positional → safe under appended kwargs |
| Two "(serial mode)" help strings | ✅ `cli.py:158` literal, `cli.py:164` "successful serial tasks" |

## Design highlights

**API (R1)**: `run_parallel(tasks, workers=0, validate_cmd="", retries=0, retry_delay=10.0, backoff=2.0, min_interval=0.0)` — defaults identical to `run_serial`, params appended after `validate_cmd` so all three positional call sites are untouched (R5).

**Retry loop (R2)**: `_run_one` wraps each attempt in the existing `SystemExit(3) → None` catch (budget cap never retried past), calls `_retry_wait` + `_retry_task_with_feedback` verbatim, keeps `parallel=True` per attempt (`record_task` inside `_run_with_gate` ⇒ per-attempt recording for free). Key insight: `_retry_wait`'s 30–60 s herd-spread was *designed for* parallel workers failing together.

**Throttle (R3)**: `min_interval` sleeps in the collector thread between successful completions — first success undelayed, k successes ⇒ exactly k−1 sleeps ⇒ total ∈ `[N, 2N)` discriminator. Noted intentional divergence from serial (which sleeps after the first success too).

**Failure modes**: 8 enumerated (FM1 cap-mid-retry, FM3 herd re-sync, FM4 cap-delay bounded by min_interval, FM5 accumulating feedback, …).

**Two constraints the evidence missed, surfaced in design**:
1. `runner.py` is **969/1000 lines** — this change lands at ~999; zero headroom, must extract before adding if over.
2. `tests/test_campaign.py:246` monkeypatches `campaign_analysis.run_parallel` with a 2-positional-arg fake — compatible, but pinned as an R5 regression test.

**Acceptance mapping**: T1a/T1a′/T1b (flaky agent unit + CLI), T2 (`exit=3` + stderr asserted in retry prompt via `args_log`), T3a/T3a′ (deterministic sleep-recorder: `[5.0]` and `[5.0, 5.0]`), T3b (real-interval CLI ≥1 s), T4 (`make ci` + `quality.py --strict`), plus R5 regression on existing parallel/cap tests.
