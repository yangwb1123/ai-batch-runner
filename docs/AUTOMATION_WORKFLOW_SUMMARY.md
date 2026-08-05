# AI-SDLC Automation Overview

`` contains an optional review orchestrator. It supplements the
repository's executable tests and does not replace them.

## Components

- `ai/run-review.py` — fills and runs one of the ten staged review
  prompts.
- `pi-batch.py` — executes task or pipeline YAML, serially or in
  parallel.
- `pi-batch.py campaign` — discovers modules, selects evidence-backed
  Top N directions, and drives direction-scoped SDLC pipelines with durable
  state and optional Git-worktree isolation.
- `pi-batch.py memory` — queries the progressive message index with
  incremental existing-session `ingest`, bounded `recent`/`find`, and
  raw-session `read` operations.
- `ai/prompts/` — product, architecture, security, distributed-systems,
  implementation, performance, readiness, planning, retrospective, and CTO
  review stages.
- `prompts/` — individual expert-role prompts.
- `git-auto-commit.sh` — interactive helper that stages **every**
  worktree change; see the safety warning in
  [`GIT_AUTO_COMMIT_GUIDE.md`](GIT_AUTO_COMMIT_GUIDE.md).

## Runner status

- `run-review.py` live runs persist each stage to `stage-NN.out.md` (only
  after validation: rejected or failed stages leave no file), and `--all`
  injects completed stage outputs into downstream paste-style variables;
  explicit context/CLI values win. `--all --resume` revalidates saved outputs
  with the current gates, skips only passing stages, and regenerates stale
  ones before chaining them downstream.
- Post-stage pipeline `commands` failures now fail the run with a non-zero
  exit, so configured build, vet, test, or `make ci` hooks act as failure
  gates when a pipeline defines them. Declared commands also rerun after full
  artifact reuse, preserving the stage-wide gate invariant.
- `from_outputs` stages accept `aggregate: true`, which merges every upstream
  artifact into one combined prompt per role template (`{input_stem}` becomes
  `combined`) instead of fanning each artifact into an independent task. A
  shared `evidence.max_bytes` budget (64KiB default) fairly samples up to
  `evidence.max_sources` candidates (64 default); omission is explicit, selected
  truncation markers retain full paths, and on-disk artifacts remain unchanged.
  `--reuse` also covers `from_outputs` tasks (aggregate and fan-out), but only
  after the current validator accepts the saved file; reused paths stay visible
  to downstream stages while stale artifacts regenerate.
  `from_outputs` may also be a list of stage names, forming an explicit DAG
  evidence join for implementation and acceptance stages.
- Pipeline `mode`, `workers`, and `timeout` are overridden by the matching
  top-level CLI flags when those flags are passed explicitly (`--mode`,
  `-w`/`--workers`, `--timeout`).
- Structured task/pipeline files fail closed before execution when mappings,
  stage dependencies, source types, positive numeric limits, task validators,
  or prompt-template paths are invalid. Invalid CLI numeric values also exit 2
  before logs, locks, or agents are created.
- `pi-batch.py` resolves `pi-batch.yaml` next to the script first, then the
  process working directory, so repository-root invocations pick up
  `pi-batch.yaml`. `--agent-bin` still overrides it explicitly.
- `--validate-cmd` runs an engineering gate against every agent result
  BEFORE its output is committed: the result is written to a temp file
  (`{output}` placeholder), the command must exit 0, then the file is
  atomically renamed into place; a failing gate deletes the temp file and
  marks the task/stage failed, so generated artifacts that do not pass
  project checks (e.g. `go build ./... && go vet ./...`, `gofmt -l`,
  `python cli.py check`) never land on disk. Works in serial, parallel,
  pipeline, and `run-review.py` modes, and integrates with retries/rounds.
- Validators are declared like the project's engineering gates: the
  `validators` registry in `pi-batch.yaml` maps short names to commands
  (`quick: python cli.py check`, `gofmt`, `build`, `config`, `root`), and
  `--validate NAME[,NAME...]` (or a task/stage `validate` field) references
  them with AND semantics, so gates stay declarative instead of repeated
  shell strings. Unknown names are executed as raw commands; `--validate-cmd`
  remains for one-off raw gates.
- Validation is optional per stage, not a global must: a task-level
  `validate` field (YAML tasks) and a stage-level `validate_cmd` (pipeline
  stages) override the CLI default; an empty value explicitly disables
  validation for that task/stage, so analysis-only tasks (e.g. "propose
  three new feature points") that produce no code skip the gate while code
  generation tasks keep it. Precedence: task > stage > CLI > none.
- Dynamic role orchestration (self-optimization): a pipeline stage with
  `meta: true` asks the agent which review roles the current deliverables
  still need, executes each chosen role against the aggregated inputs, folds
  the role deliverables back into the evidence, and iterates until the
  orchestrator reports no more roles or `max_iterations` is reached. Roles
  are discovered at run time: the orchestrator may pick a name from
  `role_dir` (predefined template) or define an ad-hoc role
  `{"role": ..., "task": ...}` whose task description plus the current
  context becomes the reviewer prompt — no template required. Chosen roles
  run concurrently, each in its own agent session; role names are sanitized
  for output paths and template lookups are confined to `role_dir` (path
  traversal rejected). See `examples/meta-review-pipeline.yaml` for a
  project-agnostic case.
- Meta role selection is relevance-bounded: `role_keywords` supplies a
  bilingual role index, the orchestrator sees scores plus matched keywords,
  and the runner enforces dedupe, `relevance_min_score`, and
  `max_roles_per_iteration` after parsing the untrusted plan.
- Repository campaigns persist transitions in `docs/auto/state.jsonl`, derive
  `SUMMARY.md`, and use content/routing fingerprints for `--reuse` and
  `--skip-passed`. Analyses share the parent lock and run in an internal pool;
  parallel implementations use separate Git worktrees and branches.
- 24x7 operation: `--retries` (serial mode) retries failed tasks with
  exponential backoff (`--retry-delay`/`--retry-backoff`; rate-limit and
  network failures wait at least 30s), `--min-interval` throttles successful
  tasks, and `--max-rounds` (0 = forever) reruns the batch until every task
  passes with `--round-delay` rest between rounds; combined with `--reuse`
  each round runs only the failures. `--log-file` appends a timestamped log
  for supervision. Model response bodies remain in artifacts/native sessions:
  `--stream-output auto` shows them live only on a TTY (`full|none` override),
  so nohup/systemd logs stay bounded and useful. See `RUNNING_247.md` for
  deployment details.
- Session reuse: `--session-mode shared` runs every task of a batch/pipeline
  in one agent session (the first call starts it with `--session-id` and
  `--name`, later calls continue it), `--session-mode per-stage` gives each
  pipeline stage its own session, and the default `new` starts a fresh
  session per call. Session ids are derived from `--session-name` (default:
  task source stem), so resumed runs continue the same session. Shared
  sessions require serial execution; the flags come from
  `pi-batch.yaml` `agent.session_flags` (pi-style by default) so other agent
  CLIs can be adapted. `run-review.py --all` supports the same with
  `--session-mode shared`.
- Progressive message memory keeps pi's raw JSONL sessions immutable and adds
  stable task/session attribution plus an append-only metadata index. A bounded
  prompt manifest tells the current LLM that `memory recent|find|read` is
  available; preliminary questions load no history by default. Validator,
  Gate, Campaign, artifact hash, failure, and archive-mapping events make old
  work reusable without injecting complete conversations into every prompt.
  Recent/read scan backward, find retains only streaming Top-K matches, and
  oversized metadata records are rejected independently of raw messages.
- Task results are validated before saving: non-zero exit, empty output, or
  a provider/CLI failure signature (quota, rate limit, billing, auth error
  codes such as `insufficient_quota` or `rate_limit_error`, `429 Too Many
  Requests`, offline/DNS/TLS/proxy failures such as `network is unreachable`,
  `connection refused`, `curl: (7)`, leading `ERROR:`/`fatal:` banners)
  marks the task failed and no output file is written; generic words like
  "error" or "timeout" are not treated as failures, so review prose is not
  misclassified. `run-review.py` also enforces a per-stage process-group
  deadline (`--timeout`, default 600s), bounded agent output, and the shared
  bounded validator executor, so a hung/noisy child cannot block or exhaust
  the coordinator.

`pipelines/pipeline-full-sdlc.yaml` is a long-running experimental
graph; its stages use `aggregate: true` so downstream roles see all upstream
evidence. Inspect it with `--dry-run` before executing.

## Recommended use

YAML task and pipeline loading requires PyYAML, which is managed in
`pyproject.toml`; install the project with `uv sync` (or
`pip install -e .`) before running the runners.

1. Put a bounded proposal in `docs/feature-spec-<name>.md` using
   `docs/templates/feature-spec.md`.
2. Dry-run the relevant review stage and inspect the filled prompt.
3. Run only the stages that add evidence for the change.
4. Reproduce every reported finding against the current code. AI-generated
   reports are not requirements, test results, or release approval.
5. Implement through the normal repository workflow and run the applicable
   package tests plus `make ci` directly, outside the pipeline runner.

Example:

```bash
python ai/run-review.py \
  --stage 02 \
  --context ai/examples/oidc-logout-context.yaml \
  --dry-run
```

Generated review directories are ignored by Git. Promote a verified conclusion
into an appropriate maintained document rather than committing the raw review
corpus. Pipeline auto-commit is disabled by default and must be enabled
explicitly only after inspecting the worktree.

## Sources of truth

- Product requirements: `docs/feature-matrix.md` and
  `docs/deferred-backlog.md`.
- HTTP contract: `docs/openapi.yaml`.
- Configuration: `docs/config-reference.md`.
- Architecture and gates: `AGENTS.md`, `docs/architecture/DIRECTORY_MAP.md`,
  and `docs/agent-os/`.

The service in this repository is a pure API backend. Any AI-SDLC proposal for
hosted login, admin, self-service, developer, or setup UI must target the
separate frontend project rather than recreating `interfaces/web`.
