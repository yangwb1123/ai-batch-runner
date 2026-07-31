# ai-batch-runner

Serial / parallel / pipeline batch executor for **any CLI-based coding agent** — not tied to
one vendor. Point it at `pi`, [Claude Code](https://github.com/anthropics/claude-code), Codex CLI,
Gemini CLI, OpenCode, or anything else that accepts:

```
<bin> -p PROMPT [--model M] [--provider P] [--thinking T] [--tools T] [--exclude-tools T]
```

and it will run one prompt, a batch of prompts (serial or parallel), or a multi-stage pipeline
against it, with per-task output capture, engineering validation gates, session reuse,
exponential-backoff retries, and optional auto git-commit after each stage.

> **Provenance**: this tree is a sync of `snaplink/ai-dev` (the living monorepo copy), carried
> over as-is including its monolith `pi-batch.py` and regression test suite. The earlier
> 4-file split (`pi_batch_config.py` / `pi_batch_task.py` / `pi_batch_pipeline.py`) was
> superseded by the reference's single-file version and removed. `tests/` (77 cases) is the
> authoritative behavior spec.

## Switching agents

The agent binary is a config value, not a hardcoded dependency — edit `agent.bin` in
`pi-batch.yaml`, or pass `--agent-bin` on the command line:

```yaml
# pi-batch.yaml
agent:
  bin: pi              # or: claude, codex, gemini, opencode, ...
  default_model: ""
  default_timeout: 300
  default_workers: 4
```

```bash
python pi-batch.py tasks.yaml --agent-bin claude --mode parallel --workers 4
python pi-batch.py tasks.yaml --agent-bin codex  --mode serial
```

## Quick start

```bash
# Single task
python pi-batch.py -p "analyze this project" -o output.md

# Batch from a YAML task file
python pi-batch.py tasks.yaml --mode parallel --workers 4

# Multi-stage pipeline (chained roles, e.g. architect -> tech-lead -> implementer -> qa -> security)
python pi-batch.py --pipeline pipelines/pipeline-code-impl.yaml

# 24x7 loop: rerun failed tasks until everything passes
python pi-batch.py tasks.yaml --max-rounds 0 --round-delay 60
```

Example `tasks.yaml`:

```yaml
tasks:
  - prompt: "Analyze the project expansion directions"
    output: docs/expansion.md
    model: claude-sonnet
    cwd: /path/to/project

  - prompt: "Review security edge cases"
    output: docs/security-review.md
    model: claude-sonnet:high
    cwd: /path/to/project
```

## Feature map

- **Task loading**: YAML / JSON / plain-text task files, `--from-dir` (one task per `.md`),
  stdin, inline `-p`. `@file.md` references in prompts are inlined automatically.
- **Execution**: serial or parallel (`--workers`), real-time streamed output, per-task
  `cwd`/`timeout`/`env`, summary report, `--dry-run`.
- **Failure detection**: provider/CLI error signatures (quota, rate limit, auth, billing,
  network/DNS/TLS) are matched against agent output — a non-zero exit, empty output, or such a
  reply marks the task failed and **never saves** the artifact.
- **Retries**: `--retries N` with exponential backoff (`--retry-delay`, `--retry-backoff`);
  rate-limit/network failures wait at least 30s so quota windows clear.
- **Rounds (24x7)**: `--max-rounds N` reruns the batch each round (0 = loop forever until all
  pass), `--round-delay` rest between rounds, `--log-file` appends run logs for supervision.
  `--reuse` skips tasks whose outputs already exist so rounds rerun only failures.
- **Sessions**: `--session-mode new|shared|per-stage` with `--session-name`; pi-style
  `agent.session_flags` in `pi-batch.yaml` (start/continue), overridable per agent CLI.
- **Validation gates**: `--validate NAME[,NAME...]` (registry in `pi-batch.yaml` `validators:`)
  or raw `--validate-cmd`; `{output}`/`{cwd}` placeholders; AND semantics; a failing gate
  deletes the temp artifact and marks the task failed (retried/regenerated); per-task
  `validate` and per-stage `validate_cmd` override/disable the CLI gate.
- **Pipelines**: chained stages via `from_prompt` (one-sentence starting point, no input file
  needed), `from_dir`, or `from_outputs`; template placeholders `{input_content}` /
  `{input_stem}` / `{input_path}`; `aggregate: true` merges all upstream outputs into one
  combined prompt per template (`input_stem` = `combined`); post-stage shell commands
  (`commands_parallel`) act as a **stage failure gate**; per-stage/per-pipeline `git_commit`;
  failed stages are reported and stop downstream stages.
- **Meta stages** (`meta: true`): dynamic role orchestration — the agent picks which roles
  still add value (either templates from `role_dir` or **ad-hoc roles** defined inline as
  `{"role": ..., "task": ...}` objects), the chosen roles run **concurrently** (each in its
  own agent session) against the aggregated evidence, their deliverables fold back in, and
  the loop repeats until the orchestrator says done or `max_iterations` is reached. Role
  names are sanitized and non-template names (incl. path traversal) are refused.
- **Git**: per-stage auto commit, single-batch commit, `--commit-prefix`, plus standalone
  `git-auto-commit.sh`.

## Layout

```
pi-batch.py              CLI entry point + engine (config, tasks, pipeline, meta stages)
pi-batch.yaml            agent.bin, session_flags, validators registry, commit prefix
AGENTS.md                agent-facing guide to the whole ai-dev toolkit (Chinese)
prompts/                 19 reference role-prompt templates (architect, tech-lead, qa-lead, ...)
pipelines/               example pipeline configs (code-impl, full-sdlc, generic example)
ai/                      AI-SDLC review framework: run-review.py + sdlc.yaml + 10 stage
                         prompts + shared prompt fragments (prompts-shared/)
examples/                pipeline examples incl. meta-review-pipeline.yaml (dynamic role
                         orchestration), SDLC case study, snaplink proposals
docs/                    usage guides incl. RUNNING_247.md (24x7 operation)
tests/                   77 regression tests (pytest): engine + run-review + CLI end-to-end
git-auto-commit.sh       interactive standalone commit helper (separate from pipeline git_commit)
```

## AI-SDLC review framework (`ai/`)

`ai/run-review.py` fills the 10-stage SDLC review templates (product discovery → architecture
→ security/RFC → distributed → implementation → performance → production readiness → sprint
planning → post-sprint review → CTO Go/No-Go) from a context YAML or CLI variables and invokes
the agent per stage, chaining outputs forward:

```bash
python ai/run-review.py --stage 01 --context ai/examples/oidc-logout-context.yaml --dry-run
python ai/run-review.py --all --context ai/examples/oidc-logout-context.yaml
```

Stage names/variables are declarative in `ai/sdlc.yaml`; agent binary, session flags and
validators are shared with `pi-batch.py` via `pi-batch.yaml`.

## Testing

```bash
python -m pytest tests/ -q     # 77 tests: config resolution, failure detection,
                               # retries/rounds, sessions, validation gates, aggregate,
                               # meta stages (ad-hoc roles, concurrency), from_prompt,
                               # run-review chaining
```

## Known open issues (carried over from the reference)

- `Task.output_path()` doesn't respect `task.cwd` for relative `output` paths
- subprocess timeout can effectively double under `_stream_and_wait`
- missing PyYAML degrades a structured `tasks.yaml` to one giant raw-text prompt silently
- `_git_commit_stage` / single-batch git commit don't check `git commit`'s exit code
- `git-auto-commit.sh` runs `git add -A` with no secret-file exclusions or protected-branch check
- PyYAML is a real, currently-undeclared dependency
- no `pyproject.toml`, no license chosen yet
