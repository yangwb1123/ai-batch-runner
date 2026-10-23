# Execution metrics: configuration is not provider effort

This is an Evidence projection change, not a scheduler, new CLI, completion rule,
or authorization. Task success, validation, retry/budget policy and routing
fingerprints are unchanged. Related contracts: [model delegation](MODEL_DELEGATION.md),
[task center](TASK_CENTER.md), and [architecture](ARCHITECTURE.md).

## Requested thinking configuration

`agent_ledger.record_result` records the requested value selected by the same
expression as `Task.to_cmd`: `config.AGENT_THINKING_OVERRIDE or task.thinking`.
A truthy but invalid override does **not** fall back to the task setting.

New optional ledger fields for a recognized value:

```json
{"thinking":"max","thinking_origin":"requested_configuration"}
```

Only exact strings `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`
are tracked (at most seven ASCII characters). Unknown, empty, non-string,
malformed, differently cased, or oversized values are omitted, not normalized or
truncated into a recognized setting. `off` is an explicit requested setting;
an absent value does not mean `off`. New provider-specific levels require an
explicit tracking-contract change; they are not guessed from arbitrary text.

This metadata is **not** provider-confirmed/clamped effort, reasoning tokens,
thinking duration, evidence that the provider honored the setting, or a quality
claim. Adapter configuration can translate the request; defaults or flags embedded
in adapter/session/extra arguments are not reverse-engineered. Recording does not
build argv, expand prompts, inspect credentials, or add raw configuration text to
logs. Existing bounded task briefs and fingerprints keep their previous behavior.

`record(..., thinking=...)` is additive and optional. A direct `record` call without
it does not inherit a process override. Old rows are not rewritten or backfilled;
`query` and `query_snapshot` retain their existing read contracts. Ledger bounds,
rotation, locking, and observational best-effort warning/false write failures remain.
The existing task-center adapter reads `thinking`; public `llm` entries retain
`origin="recorded_configuration"` and use `thinking=null` when unknown. No API or
UI-side inference is necessary.

## Three different time facts

- **Cumulative task elapsed wall** sums `TaskResult.elapsed` in the returned result
  list. Overlapping tasks may therefore exceed execution wall. Serial tasks do
  not make the largest individual elapsed value a batch wall measurement.
  Returned results usually contain final attempts only; this sum is not complete
  retry/validation accounting. Reused/synthetic results retain their historical
  elapsed values. Invalid/missing elapsed makes the cumulative value unknown.
- **Batch/pipeline execution wall** comes only from an actual monotonic interval,
  never the sum or maximum of task elapsed. CLI batch measurement encloses the
  entire serial/parallel dispatcher call, including its task gates, retry attempts,
  backoff, admission and throttling. It excludes source loading/reuse preflight,
  summary rendering, and the later batch-level repository validators, decision
  logging, commit and archive steps. Pipeline measurement encloses `run_pipeline`,
  including stages, commands, validations, retries and any finalization within
  that call. Neither label claims the whole CLI invocation or multiple rounds.
- **CPU time** is unknown: no process-tree CPU instrumentation is introduced.
  Wall duration, model thinking configuration, tokens and cost cannot supply it.

`pbatch.runner_summary` owns this read-only execution-report projection and timing
format validation. Execution remains in the runner/pipeline; CLI owns the clock
boundaries. `pbatch.runner.print_summary` re-exports the same projection, preserving
existing imports and `print_summary(results)` calls. No execution logic was moved
to make room for display code.

Direct callers may provide measured evidence explicitly:

```python
print_summary(results, wall_seconds=elapsed, wall_scope="batch")
```

Only finite nonnegative numeric durations are displayed; zero is known zero,
whereas absent, boolean, negative, string, non-finite or overflowing values are
unknown. `wall_scope` labels `batch` or `pipeline`; other scopes use `execution`.
A one-argument call outside a measured CLI hook reports execution wall as unknown.
The CLI binds timing only during its synchronous one-argument summary hook, using
context-local state restored even on exceptions. Historical `_entrypoint_override`
and pipeline one-argument monkeypatch hooks are not passed new keywords or results
wrappers. Hooks that choose not to render a report remain free to do so.
Dry-runs have no execution measurement. Summary prompt excerpts use the existing
secret-redaction helper before shortening. A passing task summary explicitly does
not authorize or attest repository gates, merging, or goal acceptance.

## Regression and acceptance boundary

`tests/test_execution_metrics.py` uses private temporary ledgers, the existing
task-center projection, synthetic results and injected monotonic clocks. It covers
precedence, invalid/bounded/redacted settings, direct-record compatibility,
fingerprint stability, unknown/zero/serial/overlapping timing, CLI timing and
legacy hooks. No model calls or provider effort measurements are involved.

```bash
python -m pytest tests/test_execution_metrics.py tests/test_agent_ledger.py tests/test_routing_eval.py tests/test_task_center.py -q
python quality.py --strict .
git diff --check
```

These are local regression/quality checks, not actual-phone, external-provider,
seven-day autonomy, production acceptance, or release approval. Unknown costs,
usage, goal acceptance, CPU and provider effort remain unknown.
