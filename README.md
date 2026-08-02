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
> over as-is including its regression test suite. The engine now lives in the `pbatch/`
> package with `pi-batch.py` as a thin entry shim (the reference's current layout; the
> earlier 4-file split and the 1389-line monolith were both superseded). `tests/` (169 cases)
> is the authoritative behavior spec.
>
> **Local adaptations** (everything else stays byte-identical): `pi-batch.yaml`
> `validators.pyquality` points at `{cwd}/quality.py` (reference used an `ai-dev/` prefix
> from snaplink's repo layout); `AGENTS.md` carries a top note mapping `ai-dev/`-prefixed
> commands to this repo's root layout.
>
> **Local fixes** (self-iteration round 1, driven by the meta-review pipeline in
> `docs/self-iteration.yaml` — see `docs/reviews/`):
> - `Task.output_path()` resolves relative outputs against `task.cwd` (was: process cwd);
>   `_filter_reused` follows suit (`pbatch/models.py`, `pbatch/cli.py`)
> - `_git_commit_stage` and single-batch git commit check exit codes and log real failures
>   (`pbatch/pipeline.py`, `pbatch/cli.py`)
> - `load_tasks` fails loud when PyYAML is missing for a `.yaml`/`tasks:` file
>   (`pbatch/pipeline.py`)
> - `save_result` writes atomically (tmp+rename) on the no-validator path too
>   (`pbatch/runner.py`)
> - `git-auto-commit.sh`: protected-branch guard + sensitive-file exclusions
> - tests: `tests/test_local_fixes.py` (10 cases); config-resolution tests run with a
>   clean env (`PBATCH_SCRIPT_DIR` can no longer hijack them)
>
> **Local fixes** (self-iteration round 2):
> - F1: `from_dir` constructors emit absolute output paths — the round-1 cwd-aware
>   `output_path()` otherwise double-prefixed them (`docs/inputs/docs/inputs/...`)
> - P0-1: stage outputs, meta role outputs and single-batch git-commit inputs now
>   propagate **resolved** paths (`output_path()`), so git add / `_combine_outputs` /
>   archiving / decision logs can no longer operate on decoy process-cwd files
> - F2: `run_task` and `ai/run-review.py::_run_agent` kill the child process group on
>   ANY abnormal exit (Ctrl-C/SIGTERM included) — no more orphaned agents
> - F3/F14: validator `{output}`/`{cwd}` substitutions are shell-quoted; temp files use
>   unique mkstemp names; validators run in their own process group with a killable
>   timeout (`_run_validator`)
> - F5: task log lines show the prompt truncated to 80 chars (prompt content no longer
>   shipped verbatim by `--log-file`)
> - F9: stdin is consumed once before the round loop — `--max-rounds` no longer dies
>   with "Provide a prompt" on round 2+ for stdin batches
> - F12: gate verdict regex is line-anchored (`^\s*VERDICT\s*:`) — prose mentions no
>   longer unlock a gate
> - `.coverage` tolerated by root-policy and gitignored; Makefile/docs test counts
>   refreshed (110)
> - tests: 9 more cases in `tests/test_local_fixes.py` (19 total)
>
> **Local fixes** (full-SDLC pipeline round, T1/T2 per `docs/IMPLEMENTATION_PLAN_T0_CLOSEOUT.md`):
> - T1 `pbatch/reuse.py::reuse_decision()`: reuse requires exists ∧ non-empty ∧ not-symlink
>   ∧ all effective validators still pass; stale artifacts are deleted for regeneration;
>   all six reuse sites wired; `--reuse-legacy` escape hatch
> - T2: `TaskResult` additive validation fields; validator stderr (capped 4000/40) + exit
>   flow into the retry prompt; summary shows the validator exit signature; `run_serial`/
>   `run_parallel` share `_run_with_gate`
> - `revalidate_existing` fails closed on symlink artifacts (QA-1 complete)
> - tests: 11 more cases (30 total in `tests/test_local_fixes.py`)
>
> **Local fixes** (7x24 governance, T3-T5 per `docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md`):
> - T3 log rotation (`RotatingFileHandler`, `--log-max-bytes`/`--log-backups`)
> - T4 single-instance lock (`pbatch/lock.py`, O_EXCL, stale detection, exit 5)
> - T5 triage (`pbatch/triage.py`): circuit isolation (`--circuit-max`),
>   stall detection (`--stall-rounds`, exit 4), residue markers
>   (`.pi-batch/state/`), environment-class halt (shared validator stderr)
> - tests: 12 more cases (42 total in `tests/test_local_fixes.py`)
>
> **Local fixes** (cost governance, T6-T8 per `docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md`):
> - T6 spike verified pi session files (`~/.pi/agent/sessions/--<cwd>--/`,
>   `message.usage` JSON pointers) — report in `docs/spike-pi-session.md`
> - T7 `pbatch/metering.py`: `--events-file` JSONL events (task_finish/
>   task_fail/budget_cap) with per-call session usage diff; `--webhook`
>   non-blocking delivery; usage:null degradation
> - T8 budgets: `--budget-max` (per run) + `--daily-budget` (UTC state
>   file); cap wins over `--max-rounds 0`, exit 3
> - tests: 3 more cases (45 total in `tests/test_local_fixes.py`)
>
> **Local fixes** (hardening, T11/T13 per `docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md`):
> - T11 hetero-routing: pipeline task defs pass provider/thinking/tools/
>   exclude_tools to the agent argv; stage-level model/provider for meta
>   stages; `--provider` CLI override (CLI > task > stage > default)
> - T13: SIGINT orphan test hardened with a pid/go handshake (no start race)
> - tests: 3 more cases (48 total in `tests/test_local_fixes.py`)
>
> **Local fixes** (governance close-out, T9/T10/T12 per plan):
> - T9 fingerprint reuse 2.0: `--reuse-fingerprint` + sidecar
>   (`<output>.meta.json`, sha256 of inputs/gate/routing); input change
>   regenerates; wired through all six reuse sites + single-batch
> - T10 session rotation: `--session-max-bytes` watermark forks the shared
>   session via pi's `--fork`; compaction warnings (`pbatch/session.py`)
> - T12 governance: validator JSON status (pass/warn/fail), bare
>   `VERDICT: PASS` fails closed, `<evidence>` fences in combined inputs,
>   output byte cap (2MB), `secretscan` validator registry entry
> - tests: 18 more cases (66 total in `tests/test_local_fixes.py`);
>   upstream gate/evidence fixtures updated for the new fail-closed and
>   fence semantics
> - T12c approval stages (decision D5): `approval: true` pauses the
>   pipeline at a human gate — PBATCH_APPROVE=1 env / `--approve-file` /
>   interactive TTY, fail-closed otherwise (5 tests; 71 local total)
>   
> These pbatch/ + ai/ edits will need re-applying after the next upstream sync (they are
> the only divergence from snaplink/ai-dev).

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
- **Gate stages** (`gate: true`): a stage whose deliverables must contain a `VERDICT: PASS`
  line — anything else (FAIL, missing verdict) fails closed and halts the pipeline.
- **Decision log** (`decision_log:` pipeline key or `--decision-log FILE`): one structured
  record per finished stage — status, extracted decision points (markdown headings) and
  evidence, gate verdicts.
- **Archiving** (`archive_dir:` pipeline key or `--archive-dir DIR`): after a fully
  successful run, deliverables move into a timestamped archive subdirectory so the worktree
  stays clean; failed runs never archive.
- **Git**: per-stage auto commit, single-batch commit, `--commit-prefix`, plus standalone
  `git-auto-commit.sh`.

## Layout

```
pi-batch.py              thin entry shim (re-exports the pbatch package)
pbatch/                  engine package: config.py, models.py, runner.py, pipeline.py, cli.py
pi-batch.yaml            agent.bin, session_flags, validators registry, commit prefix
AGENTS.md                agent-facing guide to the whole toolkit (Chinese; local adapt note)
quality.py               upstream-synced org scanner (func lines, complexity, dup bodies)
prompts/                 19 reference role-prompt templates (architect, tech-lead, qa-lead, ...)
pipelines/               example pipeline configs (code-impl, full-sdlc, generic example)
ai/                      AI-SDLC review framework: run-review.py + sdlc.yaml + 10 stage
                         prompts + shared prompt fragments (prompts-shared/)
examples/                pipeline examples incl. meta-review-pipeline.yaml, quickstarts,
                         full-sdlc-implement.yaml (real implementation w/ build gates)
scripts/                 upstream one-command flows: rolling-analysis.sh, full-flow.sh,
                         full-auto.sh (module-by-module SDLC automation), paths adapted
docs/                    usage guides incl. RUNNING_247.md (24x7 operation)
tests/                   169 regression tests (pytest): engine + run-review + CLI end-to-end
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
make ci                       # full gate set: gates + self-test + regression suite
python -m pytest tests/ -q    # 169 tests: config resolution, failure detection,
                              # retries/rounds, sessions, validation gates, aggregate,
                              # meta stages (ad-hoc roles, concurrency), from_prompt,
                              # gate verdicts, decision logs, archiving, run-review chaining
```

## Quality gates (modeled on snaplink's engineering harness)

The repo enforces the same discipline snaplink uses (`cli.py` + `checks/` +
`engineering.yaml` + `make ci`), adapted to Python with zero third-party
dependencies:

- **filesize** (500 lines/file), **complexity** (cyclomatic 15 / cognitive 20 per
  function, ast-based), **root policy** (root dir stays a thin launcher layer),
  **style** (trailing whitespace/tabs/final newline) — all declaratively
  configured in `engineering.yaml`
- The upstream-synced engine files (`pi-batch.py`, `ai/run-review.py`) are exempt
  via `ignore_patterns`; the gates bind on code authored here
- `make self-test` verifies the harness itself (34 unittest cases); `make test`
  runs the 77-case regression suite; `make coverage` reports check coverage
- Git hooks in `.githooks/` (enable: `git config core.hooksPath .githooks`):
  pre-commit gates staged files, pre-push runs the full harness
- CI: `.github/workflows/ci.yml` runs G1-G4 + self-test + regression + coverage

See `docs/CHECKS_REGISTRY.md` for the full registry.

## Known open issues

- no license chosen yet (needs compatibility check against the upstream snaplink/ai-dev
  license before picking; `engineering.yaml` already allows a root `LICENSE` file)

Resolved by self-iteration round 1 (see Local fixes above): `output_path()` ignoring
`task.cwd`, git-commit exit codes unchecked, PyYAML silent degradation, the stale
"timeout can double" README item (already fixed upstream via `_stream_proc`'s shared
deadline), missing `pyproject.toml`, and the unguarded `git add -A`.
