# ai-batch-runner

Serial / parallel / pipeline batch executor for **any CLI-based coding agent** — not tied to
one vendor. Point it at `pi`, [Claude Code](https://github.com/anthropics/claude-code), Codex CLI,
Gemini CLI, OpenCode, or anything else that accepts:

```
<bin> -p PROMPT [--model M] [--provider P] [--thinking T] [--tools T] [--exclude-tools T]
```

and it will run one prompt, a batch of prompts (serial or parallel), or a multi-stage pipeline
against it, with per-task output capture, timeouts, and optional auto git-commit after each stage.

## Switching agents

The agent binary is a config value, not a hardcoded dependency — edit `agent.bin` in
`pi-batch.yaml`, or pass `--agent-bin` on the command line, to switch between agents per run:

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

No code changes needed to retarget a different agent CLI — only `pi_batch_config.py` reads
`pi-batch.yaml`; the rest of the engine is agent-agnostic.

## Quick start

```bash
# Single task
python pi-batch.py -p "analyze this project" -o output.md

# Batch from a YAML task file
python pi-batch.py tasks.yaml --mode parallel --workers 4

# Multi-stage pipeline (chained roles, e.g. architect -> tech-lead -> implementer -> qa -> security)
python pi-batch.py --pipeline pipelines/pipeline-code-impl.yaml
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

## Layout

```
pi-batch.py              CLI entry point
pi_batch_config.py        config (pi-batch.yaml) + logging
pi_batch_task.py          Task model, serial/parallel engine, checkpoint-less resume via --reuse
pi_batch_pipeline.py      --pipeline mode: chained named stages, from_dir / from_outputs
pi-batch.yaml             agent.bin + defaults (edit this to point at your agent CLI)
prompts/                  18 reference role-prompt templates (architect, tech-lead, qa-lead, ...)
pipelines/                example pipeline configs (code-impl, full-sdlc, generic example)
docs/                     git-auto-commit usage guides, role summary
git-auto-commit.sh        interactive standalone commit helper (separate from pipeline git_commit)
```

## Provenance / known state

Extracted from a larger monorepo where it lived at `ai-dev/`. Carried over as-is, including
known open issues not yet fixed here:

- `Task.output_path()` doesn't respect `task.cwd` for relative `output` paths (`pi_batch_task.py`)
- subprocess timeout can effectively double under `_stream_and_wait` (`pi_batch_task.py`)
- missing PyYAML degrades a structured `tasks.yaml` to one giant raw-text prompt silently (`pi_batch_task.py`)
- `_git_commit_stage` / `_git_commit_single_stage` don't check `git commit`'s exit code (`pi_batch_pipeline.py`, `pi-batch.py`)
- `git-auto-commit.sh` runs `git add -A` with no secret-file exclusions or protected-branch check
- no `pyproject.toml` (PyYAML is a real, currently-undeclared dependency for `--pipeline`/`tasks.yaml` mode)
- zero test coverage

No license chosen yet.
