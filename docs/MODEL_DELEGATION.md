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
`agent.agents.<binary>.no_tools_args`; otherwise the planner invocation is
rejected before spawn (fail closed). The shipped `pi` adapter uses
`["--no-tools"]`. Other CLIs must be configured with their equivalent no-tool
flag; do not assume prompt wording alone enforces the boundary.

`role_models` and `role_providers` are trusted pipeline configuration, not
fields accepted from the planner's JSON. A child cannot select arbitrary models
or providers. Role fan-out remains capped by `max_roles_per_iteration`, and
actual parallelism by `workers`.

This feature is the batch/pipeline planner-worker path. It does not claim to
change provider-native TUI subagent behavior or make a batch meta stage durable
across process termination; existing TUI actor events remain observational.
