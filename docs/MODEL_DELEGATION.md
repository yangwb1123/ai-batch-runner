# Meta planner / worker model routing

A `meta: true` pipeline stage can now keep planning on a stronger model while
running selected roles on cheaper or task-specialized models. Role execution
remains parallel and uses the existing independent task sessions, validators,
retry policy, and runtime ledger.

```yaml
- name: review
  from_outputs: implementation
  meta: true
  role_dir: prompts
  output_dir: docs/reviews
  workers: 4

  # High-capability controller; only emits a bounded JSON role plan.
  orchestrator_model: "provider/flagship-model"
  delegation_only: true
  meta_resume: true  # use --reuse to recover the named-role plan after interruption

  # Default child model, with optional role-specific overrides.
  worker_model: "provider/fast-model"
  role_models:
    security_engineer: "provider/security-model"
    architect: "provider/reasoning-model"
```

Routing precedence:

- Planner: CLI `--model` > `orchestrator_model` > stage `model` > agent default.
- Worker: CLI `--model` > `role_models[role]` > `worker_model` > stage `model` > agent default.
- Providers use the corresponding `orchestrator_provider` / `worker_provider` /
  `role_providers[role]` fields, then stage `provider`; CLI `--provider` overrides
  all pipeline-stage provider selections.

`delegation_only: true` prepends a planning-only instruction **and** disables
all tools at the adapter boundary. This is opt-in to preserve existing pipelines.
The selected agent adapter must declare a tool-disable argv in
`agent.no_tools_args` or `agent.agents.<binary>.no_tools_args`; otherwise the
planner invocation is rejected before spawn (fail closed). The shipped `pi` adapter uses
`["--no-tools"]`. Other CLIs must be configured with their equivalent no-tool
flag; do not assume prompt wording alone enforces the boundary.

`role_models` and `role_providers` are trusted pipeline configuration, not
fields accepted from the planner's JSON. A child cannot select arbitrary models
or providers. Role fan-out remains capped by `max_roles_per_iteration`, and
actual parallelism by `workers`.

### Optional structured task DAG

Set `meta_task_dag: true` to require a bounded, validated task graph instead of
a flat role list:

```yaml
meta: true
meta_task_dag: true
max_roles_per_iteration: 8
workers: 4
```

The planner must return objects such as
`[{"id":"design","role":"architect","task":"...","depends_on":[]},
{"id":"review","role":"security_engineer","task":"...","depends_on":["design"]}]`.
IDs and references are validated, cycles/unknown dependencies and fan-out
excess fail closed before worker dispatch, and task descriptions are bounded.
Ready nodes run in declaration-order waves with at most `workers` parallel
executions. A dependent receives the original evidence plus its direct
prerequisites' artifacts; a failed prerequisite blocks its descendants while
independent nodes can still finish. Blocked nodes count as failures for
`meta_max_failed_roles`. `meta_role_retries` and validators remain code-enforced.
Task DAG plans are not checkpointed as named-role lists: with `meta_resume`, an
interrupted DAG is replanned and completed output is reused only when its
fingerprint and validators still match.

### Recovering an interrupted meta stage

Set `meta_resume: true` on a meta stage to checkpoint the selected **named-role
plan** and its iteration state under the workspace's private runtime data
folder. Restart the pipeline with `--reuse` (or `/pipeline FILE --reuse` in the
TUI): a matching saved plan is replayed, completed worker outputs are rechecked
against their fingerprints and validators, and missing/failed workers are
rerun. Iterations use distinct output names after the first so earlier evidence
is not overwritten. State is bounded, atomically written, and stores hashes and
role names—not prompts, deliverable content, or ad-hoc task text. Plans with
ad-hoc assignments are deliberately replanned after restart.

`meta_resume` alone is stage-level recovery, not a durable always-on scheduler.
With `--persistent-pipelines`, the orchestrator-only TUI starts a detached
worker that can continue after the UI closes. A bounded private job record holds
the pipeline/controller fingerprints, lifecycle/progress state, artifact path
references, and at most 8 KiB of stdout-only evidence; it does not store prompts
or artifact bodies. A live TUI polls the job, can request cooperative
cancellation with Ctrl+C when no controller turn is active, and returns the
result to the same session. On the next TUI launch an interrupted worker is
restarted with `--reuse`, or a pending handoff is replayed. Each job is capped
at eight worker executions, including its initial attempt. Resume fails closed
if the workspace, pipeline definition,
or controller session/model/provider/agent differs, or another live TUI owns
the job. Persistent workers reject pipelines with interactive approval stages.
The detached supervisor retries deferred locks and worker crashes within the
attempt budget even while the TUI is closed. If the supervisor itself exits,
recovery is picked up when the TUI is next opened with `--persistent-pipelines`.
This is not an OS-level service and does not survive host reboot without an
external service manager.
The TUI event journal remains observational and is not execution authorization.
Provider-native TUI subagent routing is also unchanged.

### Measuring routing outcomes

With `agent_ledger.enabled: true`, run `pi-batch routing-eval --json` to
summarize observed model/provider outcomes: task completion rate, execution
latency, retries/validator rejections, routing source/requested/final tier,
downgrade, and provider-reported token/cost usage. Filter auto-routing
observations with `--source difficulty --requested-tier high`; `--tier low`
filters the final tier after downgrade. Missing usage is reported as `null` with
explicit coverage; it is never treated as zero. The report reads a bounded
ledger snapshot, applies route/pair filters before the requested row limit,
marks truncation, and accepts `--since` with a timezone-aware ISO-8601
timestamp to scope the comparison window.

For a paired comparison, run equivalent work under both routes in the same
workspace, then specify the exact model names:

```bash
pi-batch routing-eval --baseline-model provider/fast \
  --candidate-model provider/strong --json
# If provider model IDs are ambiguous, select both sides explicitly:
pi-batch routing-eval --baseline-model shared --baseline-provider provider-a \
  --candidate-model shared --candidate-provider provider-b --json
```

When using provider selectors, specify both sides; the common `--provider` and
`--model` filters are rejected for paired comparisons to avoid silently
excluding one route. Without side-specific providers, a model selector includes
all providers that reported that model ID.

Pairing requires an identical task fingerprint (workspace, task prompt, stage,
role, task-level validator, output target, adapter, thinking/tools, and timeout).
The one-way hash excludes model/provider so routes can be compared and does
not duplicate prompt text; the existing ledger still keeps its bounded task
brief excerpt. Use the same pipeline/CLI-level validator
configuration for both runs, since that configuration is not part of the task
fingerprint. `validator_pass_rate` is the final per-task deterministic gate
proxy; `validator_attempt_pass_rate` measures every validator invocation.
Neither is a semantic quality score. Different planner-generated prompts or
changed inputs will not pair; small paired samples should not be treated as a
quality benchmark. Completion, latency, rework, and actual usage are reported separately
so model routing remains an empirical decision rather than a prompt-level claim.

### Tool-free TUI controller and same-session result return

For a managed Pi TUI session, `--orchestrator-only` disables Pi tools at process
startup and uses that session only for planning, review, and synthesis. Start a
pipeline explicitly with `/pipeline FILE` or at TUI startup with `--pipeline`:

```bash
pi-batch tui --orchestrator-only --model provider/flagship \
  --pipeline examples/meta-review-pipeline.yaml
```

On completion (including pipeline failure), bounded worker artifacts and task
status are sent as a follow-up turn to the same Pi RPC session. The controller
is asked to treat those artifacts as untrusted evidence and summarize gaps and
outcome; cancellation does not trigger a follow-up. The TUI model override is
kept for the controller and does not overwrite pipeline worker/model routing in
this mode. Workers remain owned by `run_pipeline` and its deterministic
concurrency, timeout, retry, and validator policies. The flag fails closed if
the selected Pi adapter cannot confirm a configured no-tools argv. Add
`--persistent-pipelines` to opt into a detached supervisor/worker that continues
while the TUI is closed; interactive approval stages are rejected in this mode.
Without the flag, pipeline execution remains owned by the TUI process.
