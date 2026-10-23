# Serial in-tree campaign cutoff — fourth implementation batch

The first three batches bounded direction admission, isolated child attempts
and the analysis phase. This one extends the same coordinator cutoff to the
default serial in-tree implementation path (`--parallel-pipelines` unset/1),
which previously ran with only task timeouts and entry admission.

## Mechanism

`run_direction` executes the materialized pipeline through
`_execute_serial_supervised`. When the settings carry a frozen campaign
deadline, it creates a `threading.Event` and a `SerialDeadlineGuard`:

- the guard polls the shared epoch/monotonic deadline in an owned daemon
  thread; on expiry it latches `budget_exhausted`, sets the cancel event, and
  calls `runner.kill_active_procs()` (this process's registered agent child
  groups only), repeating on a short poll until the direction returns;
- the event is threaded through `run_pipeline` → stage execution →
  `run_serial`/`run_parallel`/meta role tasks/gate-fix rounds, reusing the
  existing cancellation contract: `run_task` refuses to start cancelled work,
  the drain loop kills an in-flight child when the event is set, `_run_retried`
  stops retrying, and `run_serial` fills the remaining tasks with cancelled
  results;
- stage finalization re-checks cancellation before every newly started
  operation (approval, each post-stage command, repo validators, gate commit),
  fails closed and skips archiving; artifact reuse revalidation is
  cancellation-aware, so a cutoff observed between artifacts cannot launch
  further validators; gate-fix saving passes the event into the validator so a
  fix cannot publish a report after expiry;
- the same event and reap-safe killing are used by a small guard-owned killer:
  each registered agent child is signalled at most once, and a handle already
  reaped by the runner is never signalled again, so repeated cutoff polls
  cannot target a reused PID;
- after the pipeline returns, the deadline is re-checked. A completion observed
  at/after the cutoff — including one observed only while collecting final Git
  evidence, or an ordinary pipeline exception raised after expiry — is **not**
  published as PASS: it becomes `BUDGET_BLOCKED` with `PRESERVED` recovery,
  skips `_recover_direction`, and the existing terminal exit-3 handling stops
  the round loop. Exceptions raised before the cutoff keep propagating to the
  caller's existing failure path.
- a campaign event and a native approval channel are combined (OR), preserving
  task-scoped routing, so neither source silently shadows the other.

Without a configured cutoff none of this engages: `run_pipeline` keeps its
historical call shape and behaviour (the guard and event are `None`).

## Truthful limits

- The provider-side request is not cancelled; only the local child process is
  killed. A spawn that races the expiry is stopped by the next check/kill poll
  (a fraction of a second), not by preemption during process creation.
- A stage command or repo validator already started when cancellation arrives
  is not killed by this guard (it is not an agent child); finalization refuses
  to start new ones, and the hard command timeout still applies. A parallel
  validator batch dispatches together, so its already-submitted members may
  still finish after the cutoff.
- A handle read as reaped is skipped, but reaping performed outside the Popen
  methods (some external reaper) would set no return code; this remains a
  residual process-ownership limit shared with the runner registry.
- Local filesystem/tree-digest work in the coordinator is charged against the
  deadline but cannot be forcibly preempted while it runs.
- The guard kills only children registered in this coordinator process. An
  agent that escapes its process group is not contained by this batch (that
  remains the isolated-attempt contract's scope).
- `kill_active_procs` is process-local and runs only while a serial direction
  owns the coordinator; a future change that runs unrelated agent work
  concurrently in the same process must revisit it.
- Analysis and isolated paths are unchanged by this batch.

Offline tests cover the guard firing, late-completion mapping without
recovery, event forwarding into the pipeline, the native serial runner kwargs,
meta-stage forwarding, and no-spawn-after-cancel. They are not provider,
production-campaign or complete-CI evidence.
