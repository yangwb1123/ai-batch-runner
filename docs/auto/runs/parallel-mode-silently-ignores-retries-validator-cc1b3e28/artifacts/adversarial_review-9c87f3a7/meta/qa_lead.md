QA lead review complete. Report written to `docs/auto/runs/parallel-mode-silently-ignores-retries-validator-cc1b3e28/artifacts/adversarial_review-9c87f3a7/meta/qa_lead.md`.

## Summary

**State**: The design doc says PROPOSED, but the implementation has already landed in the working tree (uncommitted) — `_run_parallel_one`, `_collect_parallel`, new `run_parallel` signature, CLI wiring at `cli_batch.py:497`, reworded help strings, docs updated, and all 7 planned tests present. I verified the design against the actual code.

**Commands run**: 5/7 new tests pass (T1a, T1a′, T2, T3a, T3a′); 78 unit tests green; test_campaign 31 passed (R5); budget/race 7 passed; `quality.py --strict` OK (runner.py 996 ≤ 1000). The 13 CLI-subprocess failures (incl. T1b/T3b) are all the same environment collision — the live campaign (pid 1684832) holds the repo lock (exit 5) — confirmed non-behavioral; verified T1b/T3b manually with `--no-lock` (RETRY logged, rc=0, throttle measured).

**Key findings**:

1. **F1 Medium** — Design claims "feedback accumulates exactly as in serial" (§3.3, FM5) is **false**: serial rebinds from the original task each attempt (no accumulation); parallel rebinds the task and **accumulates**. T2 only tests one retry, so the divergence is unpinned. Add T2′ with `retries=2`.
2. **F2 Medium** — `retries=-1` via API crashes the whole batch with `UnboundLocalError` (reproduced live); serial tolerates it. CLI blocks negatives but no test covers them. Fix: `max(0, retries)`.
3. **F3 Medium** — Design R5 promises "budget-cap parallel tests" but **none exist** (only a serial one). FM1 cap-mid-retry verified working manually (exit 3, exactly 1 spawn) but has zero automated coverage — the riskiest new code path. Add `test_parallel_budget_cap_stops_mid_retry`.
4. **F4 Low** — `run_parallel.__doc__` is `None` (docstring after assignment; live-probed).
5. **F5 Low/Info** — CLI-subprocess tests aren't hermetic under a concurrent campaign (pre-existing); F6/F7 info items on completion-side throttle semantics and exit-3 latency bounded by one retry-sleep window.

**Exit criteria**: current tests green + the three test additions (cap-mid-retry, negative retries, multi-retry feedback) before the direction is done.
