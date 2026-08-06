# Running ai-dev 24x7

How to keep `pi-batch.py` executing continuously — for example, feeding one
prompt template to an agent a thousand times to generate feature-requirement
proposals — with automatic retry, resumption, throttling, and supervision.

The machinery composes existing safeguards: outputs are saved only after
validation (quota/rate-limit/offline replies and timeouts are rejected),
`--reuse` skips only non-empty, non-symlink outputs that still pass current
validators, and every round reruns what is missing or stale.

## One-shot batch

Define the thousand proposals as tasks — either a YAML file or a directory of
prompt files:

```yaml
# proposals.yaml
tasks:
  - prompt: "Propose one feature requirement for the SSO SDK, evidence-backed."
    output: proposals/001.md
  - prompt: "Propose one feature requirement for the SSO SDK, evidence-backed."
    output: proposals/002.md
  # ... 998 more
```

```bash
python pi-batch.py proposals.yaml \
  --mode serial \
  --min-interval 5 \
  --log-file logs/pi-batch.log
```

`--min-interval 5` sleeps five seconds between successful tasks so a long
batch does not hammer the provider into a rate limit.

## Automatic retry with backoff

Transient failures (rate limit, quota, offline, timeout) are retried in
serial mode with exponential backoff. Two timeout layers matter for long
analysis tasks: the per-task hard timeout `--timeout` (default 900s, kills
the whole process group at an absolute deadline) and pi's own HTTP idle
timeout `httpIdleTimeoutMs` (default 300s; this repo raises it to 900s via
`.pi/settings.json`). Deep analysis routinely exceeds 300s, so the defaults
are aligned on 900s; lower `--timeout` per task when a fast turnaround is
needed:

```bash
python pi-batch.py proposals.yaml \
  --mode serial \
  --retries 3 \
  --retry-delay 30 \
  --retry-backoff 2
```

Rate-limit/network failures always wait at least 30 seconds per attempt so a
rate window can clear. `--max-rounds` (see below) covers parallel mode and
pipeline runs, where retry happens between rounds instead.

## Round loop until everything passes

`--max-rounds N` reruns the batch up to N times; `--max-rounds 0` loops
forever. Combined with `--reuse`, each round executes only the tasks that
failed or were rejected in the previous round:

```bash
python pi-batch.py proposals.yaml \
  --mode serial \
  --reuse \
  --retries 2 \
  --max-rounds 0 \
  --round-delay 300 \
  --log-file logs/pi-batch.log
```

- Every task that passes is saved and, while still valid, skipped afterwards (`--reuse`).
- Failures are retried in-round (`--retries`), then again in the next round
  after a `--round-delay` rest (default 60s; 300s above gives rate limits
  time to reset).
- The process exits 0 only when every task passes. With `--max-rounds 0` it
  keeps looping; interrupt with Ctrl-C (exit 130).
- Negative limits, zero worker/circuit/stall values, and NaN/Inf timing values
  exit 2 before a run log, instance lock, or agent process is created.
  Structured task/pipeline YAML is likewise schema-checked fail-closed.

The same loop applies to pipelines (`--pipeline file.yaml`), where `--reuse`
revalidates completed stage tasks and `aggregate: true` merges upstream outputs.

## Engineering gates on generated results

Generated artifacts are only committed after the project's engineering
checks pass. Validators are declared once in `pi-batch.yaml`, like
`engineering.yaml` declares the gates for `cli.py`, and referenced by name:

```yaml
# pi-batch.yaml
validators:
  quick: "python cli.py check"          # filesize + vet
  gofmt: 'test -z "$(gofmt -l {output})"'
  build: "go build ./... && go vet ./..."
  config: "python cli.py config-validate"
  root: "python cli.py check-root"
```

```bash
# named validators, AND semantics (all must pass)
python pi-batch.py code-tasks.yaml --validate quick,gofmt

# add a one-off raw command without touching the registry
python pi-batch.py code-tasks.yaml --validate-cmd "python cli.py check"

# review output must pass a doc-level gate before stage-NN.out.md lands
python ai/run-review.py --all --context ctx.yaml --validate gofmt
```

A `validate` value on a task or stage overrides the CLI default and may also
be a registry name (`validate: gofmt`) or a raw command; an empty value
disables the gate for that task/stage (see next section).

Heavy full gates (`make ci`) belong in pipeline stage `commands`, which run
once per stage; `--validate-cmd` runs per artifact and should stay light
(compile/vet/fmt/lint of the generated file). Combined with `--retries` and
`--max-rounds`, a generated artifact that fails validation is regenerated
automatically until it passes or the budget is exhausted.

Validator execution is centralized in `pbatch/runner.run_validation()`: each
command runs in its own process group with a 600s hard deadline (a hung
validator tree is killed), and the exit code plus captured stdout/stderr are
returned as a structured result (ok / exit_code / stdout / stderr /
timed_out) instead of being lost in the log stream. The same executor backs
`revalidate_existing()`, which re-runs the effective gates against an
already-written artifact (missing or empty artifacts fail closed) — the
building block for keeping `--reuse` from promoting outputs that no longer
pass their gates.

Post-stage `commands` reuse that executor instead of `subprocess.run` capture.
Their default deadline and per-pipe diagnostic budget come from
`pi-batch.yaml` `commands.timeout` and `commands.output_max_bytes`; a stage may
override them with `command_timeout` and `command_output_max_bytes`. Timeout or
diagnostic overflow fails the stage and kills the whole hook process group.
Stage commands run even when every artifact was accepted by `--reuse`, so a
repository-wide gate cannot be bypassed by an existence-only resume.

## Validation is optional per stage

Not every flow generates code. An analysis task ("analyze the project and
propose three new feature points") produces a markdown proposal, so forcing
`go build` on it is meaningless. The gate is therefore configurable at three
levels, with per-task overrides:

```yaml
# tasks.yaml — per-task gates
# precedence: task validate > stage validate_cmd > CLI --validate-cmd > none
tasks:
  # analysis-only: explicitly skip validation ("" = disabled)
  - prompt: "Analyze the project and propose 3 new feature points"
    output: proposals/analysis.md
    validate: ""

  # code generation: gate this task even when the CLI default is off
  - prompt: "Write a Go helper"
    output: gen/helper.go
    validate: 'test -z "$(gofmt -l {output})"'

  # unset: inherit the CLI --validate-cmd (or no gate at all)
  - prompt: "Draft an RFC"
    output: docs/rfc.md
```

```yaml
# pipeline.yaml — stage-level gates
stages:
  - name: analysis
    from_dir: docs/proposals
    validate_cmd: ""   # analysis stage: no engineering gate

  - name: implementation
    from_outputs: analysis
    aggregate: true
    validate_cmd: "go build ./... && go vet ./..."
```

A stage or task without any configuration simply inherits the CLI default
(no validation when `--validate-cmd` is absent), so the common case of
"analysis batches never validate, code batches opt in" needs no boilerplate.

## Self-optimizing role orchestration (meta stages)

For analyzing arbitrary projects and ideas, a pipeline stage with `meta: true`
discovers its review roles at run time instead of fixing them in YAML. The
starting point can be a one-sentence prompt (no input files needed):

```yaml
stages:
  - name: kickoff
    from_prompt: "Analyze the idea: offline-first sync for the todo app."  # one sentence
    output: docs/reviews/kickoff.md

  - name: review
    from_outputs: kickoff
    meta: true                       # orchestrator picks the roles
    role_dir: prompts         # point at the target project's role templates
    role_keywords: pbatch/role_keywords.yaml
    output_dir: docs/reviews
    max_iterations: 3
    max_roles_per_iteration: 3       # runtime-enforced, not prompt-only
    relevance_min_score: 2
```

`from_prompt` is a full stage type: it runs as a single task, honors
`--reuse`/validation/sessions, and its output feeds downstream `from_outputs`
stages. `from_dir` remains available when the input is a directory of
documents.

`from_outputs` accepts either one stage name or a YAML list. With a list and
`aggregate: true`, the runner joins every named stage's artifacts in declared
order. Full-SDLC implementation and acceptance stages should use this form so
they receive requirements, design, review, and gate evidence—not only the
immediately preceding verdict.

Artifact content injected into aggregate and meta prompts is bounded by
`pi-batch.yaml` `evidence.max_bytes` (default 64KiB), while
`evidence.max_sources` (default 64) independently bounds path traversal, fences,
and the aggregate `{input_path}` manifest. The byte budget is distributed fairly
across selected sources; truncation and omitted-source markers remain explicit,
and each truncated fence reports shown/total bytes and the full artifact path.
Files on disk are never shortened. Meta iterations rebuild bounded context from
the upstream/role-output file list, preventing recursive prompt growth while
preserving on-demand access to complete evidence.
The final `-p` process argument is separately capped by `prompt.max_bytes`
(120KiB default), below Linux's usual 128KiB per-argument boundary. Oversized
prompts fail before spawning; put larger context in referenced artifacts.

Each iteration:

1. the orchestrator agent reads the current deliverables and `Available roles`
   from `role_dir`, and replies with a JSON plan: role names (e.g.
   `["security_engineer", "qa_lead"]`) and/or ad-hoc role objects
   (`{"role": "perf_reviewer", "task": "Analyze performance bottlenecks"}`),
   or `[]` when done;
2. every chosen role runs **concurrently, each in its own agent session**:
   named roles load their `role_dir` template, ad-hoc roles use their task
   description plus the current deliverables as context (no template
   needed);
3. the deliverables fold back into the evidence, so the next orchestrator
   round sees what previous roles concluded;
4. the loop stops when the orchestrator says `[]` or `max_iterations` is
   reached.

The orchestrator output is untrusted input: named roles must resolve to `.md`
files inside `role_dir` (path traversal rejected) and ad-hoc role names are
sanitized for output paths. `role_dir` may be absent — ad-hoc roles alone are
enough. All other machinery (retries, `--reuse`, rounds, validation,
sessions) applies to meta stages too.

Meta reuse is role-granular: the orchestrator still runs so history cannot
freeze the plan, but a role it selects again reuses its artifact only after
the current validator passes and, under `--reuse-fingerprint`, its prompt,
output, validator, model, and provider fingerprint still matches.

Before each orchestrator call, available role templates are scored against
the current evidence using the bilingual keyword map. The prompt shows scores
and matched keywords; after the model responds, the runner deduplicates the
plan, filters named roles below `relevance_min_score`, and enforces
`max_roles_per_iteration`. Ad-hoc roles remain available for concerns not
covered by the template catalog. Set `relevance_enabled: false` to disable
scoring while retaining the runtime fan-out cap.

## Repository campaigns

`pi-batch.py campaign` is the project-level layer above normal pipelines:

```bash
python pi-batch.py campaign --dry-run
python pi-batch.py campaign --jobs 4 --max-directions 2 \
  --reuse --retry-failed
```

It discovers bounded module directories, runs structured analyses in parallel,
scores and deduplicates directions, rejects candidates without existing-file
evidence or testable acceptance checks, then materializes one direction-scoped
SDLC pipeline per selection. `docs/auto/state.jsonl` is append-only authority;
`SUMMARY.md` is an atomic derived view. Freshness fingerprints cover repository
and module state, prompts, direction data, pipeline/role/config content, and
model/provider routing. State records use bounded line reads
(`state.line_max_bytes`, 256KiB default), and the coordinator caches only each
module/direction's latest transition for repeated reuse checks.

Pi session files remain append-only and untouched. Metering caches each file's
device/inode, byte offset, and cumulative numeric usage, then scans only newly
appended complete records. `session.line_max_bytes` (4MiB default) bounds a
single coordinator-side JSONL parse; an oversized record is skipped without
changing the raw file. Emitted task usage is the before/after invocation delta.

Analysis parallelism stays inside the one lock-owning process. Implementation
is serial by default. `--parallel-pipelines N` requires a clean Git worktree at
campaign start and creates one persistent branch/worktree/output/decision-log/
run-log namespace per direction. This prevents concurrent agents and validators
from observing each other's partial repository changes. Isolated branches are
not auto-merged; state records the branch, commit, and worktree evidence for the
repository's normal review/merge process. `implementation.timeout` (14400s by
default) is a wall-clock ceiling for each isolated direction; expiry kills the
whole child pipeline group and records a failed outcome. The Bash
`scripts/full-auto.sh` entry is only a compatibility wrapper.

### 多 campaign 串行协作（同仓库锁 + 排队）

实战教训（aero-vault 维护）：多个维护者/自动队列可能同时想跑同一仓库的
campaign。规则：

- **锁是仓库级的**（`.pi-batch.lock`，含 pid/start/host/argv/token）。活 PID
  的锁永不打破（7×24 运行可远超 24h）；死 PID 的 stale 锁由下一个实例自动
  破除；不可解析锁 fail-closed。
- **排队用内建 `--wait-lock MINUTES`**（campaign 与单批 CLI 均支持）：30s
  轮询、stale 锁立即破除、预算耗尽 exit 5。shell 版 `wait-for-run.sh`（
  examples/snaplink-platform/）留给非 pi-batch 命令，同样带 stale 检测。
- 多个 campaign 可共享 `docs/auto/state.jsonl`（事件按 campaign 名区分，
  SUMMARY.md 的 Counts 按 campaign 拆分），但不要并发写同一工作树——
  `--no-lock` 只用于故意分叉（不同 worktree）。
- 每次 campaign 启动会记录 `TOOL_VERSION` 事件（工具代码 digest + 工具仓库
  HEAD）：运行中工具被升级会失效旧指纹并可见，防止旧代码静默产出。

### 轮循环：--rounds N

"跑 N 次"是一等特性，无需外部 bash 循环：

```bash
python pi-batch.py campaign --rounds 10 --round-delay 60 \
  --round-commit --reuse --retry-failed --wait-lock 10080
```

- `--rounds N`（0 = 无限）：每轮重新发现模块；配合 `--reuse/--skip-passed`，
  指纹使后轮增量——已 PASSED 的分析/方向直接复用，只有被上一轮改动的模块
  重新分析（新鲜证据）。不带复用标志时每轮都是全量新跑。
- `--round-delay SECONDS`：轮间节流。
- `--round-commit`：每轮结束后 `git add -A` + 提交（尊重 .gitignore，提交
  失败只告警不影响轮次）；轮间工作树保持干净。
- dry-run 始终单次（规划不循环）；负值/NaN 在启动前拒绝（exit 2）。

### 超时配置（campaign 流水线）

实战教训（compose-017）：900s 默认超时对真实仓库的实现/审查任务偏紧，
56 个方向的 implement 阶段几乎全部超时失败。按阶段配置：

```yaml
# 模板示例：examples/repository-campaign-pipeline.yaml
stages:
  - name: adversarial_review
    meta: true
    meta_timeout: 2400   # meta 角色任务 + 编排者超时（读大代码库）
  - name: implement
    tasks:
      - prompt: "..."
        timeout: 1800    # 单任务超时（实现 + 全仓库门禁）
```

`--timeout N`（CLI）覆盖所有任务超时；`meta_timeout` > `--timeout`（meta
阶段专用）。

### 模块粒度建议

Go 仓库 `internal/` 下推荐 `max_depth: 2`（拆到 `internal/api/rest` 级）：
`max_depth: 1` 会把 rest+s3compat+webdav 捆成一个巨型模块，分析粗糙、方向
过宽。`fallback_top_level: true` 适合无固定层级的小仓库。

### 重试反馈闭环

被 `design_gate`/`acceptance` 拒的方向重跑时，`materialize_pipeline` 会把
上次 run_dir 里的 VERDICT 全文（≤24KiB、≤6 文件）折进 requirements/design/
implement prompt，重试必须逐条处置上次发现（裁决门会复查）。

## One session, many steps

By default every call starts a fresh agent session. When later steps should
see the conversation context of earlier ones — for example the stages of a
pipeline building on each other's discussion — reuse one session:

```bash
# one session for the whole pipeline (every stage continues it)
python pi-batch.py --pipeline sdlc.yaml \
  --session-mode shared --session-name sdlc-2026-07

# one session per pipeline stage (parallel roles inside a stage share it)
python pi-batch.py --pipeline sdlc.yaml \
  --session-mode per-stage --session-name sdlc-2026-07

# one session across all ten review stages
python ai/run-review.py --all --context ctx.yaml \
  --session-mode shared --session-name review-2026-07
```

- The first call starts the session with `--session-id <id> --name <name>`;
  later calls pass `--session-id <id>` only. Session ids are derived from
  `--session-name`, so a resumed run (`--reuse`/`--resume`) continues the
  same conversation instead of starting over.
- Shared sessions require serial execution (`--mode serial`); parallel calls
  would interleave inside one session and corrupt the conversation order, so
  the runner rejects that combination.
- The flags come from `pi-batch.yaml` `agent.session_flags` (pi-style by
  default). For another agent CLI, point those flags at its own
  continue-session option.
- Keep sessions small: one long-lived session accumulates context until the
  model window fills, so prefer `per-stage` over `shared` for long pipelines
  and start a new `--session-name` per campaign.

## Running detached (nohup)

```bash
nohup python pi-batch.py proposals.yaml \
  --mode serial \
  --reuse \
  --retries 3 \
  --max-rounds 0 \
  --min-interval 5 \
  --log-file logs/pi-batch.log \
  > /dev/null 2>&1 &

echo $! > run.pid
```

Watch progress via the log and the output directory:

```bash
tail -f logs/pi-batch.log
ls proposals/ | wc -l        # how many of the 1000 are done
```

## Running as a systemd service

```ini
# /etc/systemd/system/ai-proposals.service
[Unit]
Description=ai-dev 1000 proposals batch
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/u1/workspace/demo/snaplink
ExecStart=/usr/bin/python pi-batch.py /srv/proposals.yaml --mode serial --reuse --retries 3 --max-rounds 0 --min-interval 5 --log-file /var/log/ai-proposals.log
Restart=always
RestartSec=30
Environment=HOME=/home/u1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-proposals
journalctl -u ai-proposals -f
```

`Restart=always` restarts the process after a crash; because every saved
output is on disk and `--reuse` revalidates it before skipping, a restart
continues from exactly where the run stopped — no work is lost and stale or
tampered artifacts are regenerated.

## Generating a new batch each day

The runner reruns the same task list; it does not invent new proposals on its
own. To feed it fresh prompts daily, generate a new task file from a
template (cron or a systemd timer) and point the service at it:

```bash
# cron: 02:00 daily
python /srv/gen_proposals.py > /srv/proposals-$(date +%F).yaml
```

Then start one batch process per file, or rotate `proposals.yaml` before
restarting the service.

## Supervision notes

- Quota, rate-limit, offline, and timeout replies are never saved as outputs
  (see `AUTOMATION_WORKFLOW_SUMMARY.md`), so a failed run leaves no bogus
  proposal files behind.
- `--log-file` appends a timestamped log; combine with `tail -f` or a log
  collector for alerting. It records operational metadata, not complete model
  messages. `--stream-output auto` (default) shows response bodies only on an
  interactive TTY; use `full` or `none` to override. A task without `output`
  still prints its final answer once because stdout is its result channel.
  Missing parent directories for the log are created automatically.
- Budget guardrails: `--min-interval` throttles serial batches, `--workers`
  caps parallel fan-out, and `--retries`/`--max-rounds` bound how many times
  a failing task is re-attempted before the run reports failure.

## Reuse freshness revalidation (T1)

`--reuse` no longer trusts existence alone: every artifact is reusable only
when it exists, is non-empty, is not a symlink, and (when validators are
configured) still passes every effective gate (`pbatch/reuse.py::reuse_decision`).
A stale artifact is deleted so the round regenerates it; `--reuse-legacy`
restores the old existence-only skip. The same contract backs all six reuse
sites (single-batch `_filter_reused`, from_prompt/from_dir/from_outputs/
aggregate builders, and the fully-reused stage short-circuit).

Validator rejections now flow back into retries (T2): the capped stderr
(4000 chars / 40 lines) and exit code are appended to the retry prompt, so
the agent can fix the gate problem directly; the summary line shows the
validator exit signature.

## 7x24 governance (T3-T5)

- **Log rotation** (T3): `--log-file` uses a RotatingFileHandler
  (`--log-max-bytes` default 5MB, `--log-backups` default 3) — unbounded
  logs and disk-full crashes are gone. Streaming also stops at the configured
  output cap. Parallel Campaign worktrees use only the child handler for each
  logfile, preventing duplicate writes and rotation races; model bodies remain
  available in artifacts and native session JSONL.
- **Single-instance lock** (T4): an O_EXCL `.pi-batch.lock` in the cwd
  refuses a second concurrent instance (exit 5, holder named in the error);
  stale locks are broken only when the holder PID is dead (a live 7x24 holder
  is never displaced merely because it is old); released in
  finally; `--dry-run` and `--no-lock` skip it.
- **Triage** (T5): per-task circuit isolation (`--circuit-max` default 5,
  poison tasks stop burning quota), stall detection (`--stall-rounds`
  default 6 rounds without progress -> exit 4), residue markers
  (`.pi-batch/state/*.start`, a surviving marker signals a killed run and
  is regenerated), and environment-class halt (identical validator stderr
  across >=2 tasks stops the loop). Exit codes: 0 success, 1 max rounds,
  3 budget cap (T8), 4 stall/circuit stop, 5 lock held, 130 interrupt.

## Cost governance (T6-T8)

- **Metering** (T7): `--events-file FILE` appends JSONL events
  (task_finish / task_fail / budget_cap) with per-call usage read from the
  pi session file (`message.usage` JSON pointers, spike-validated in
  `docs/spike-pi-session.md`); a missing/unparseable session degrades to
  `usage: null`, never a crash. `--webhook URL` POSTs the same events
  non-blockingly (5s timeout, failures logged only).
- **Budgets** (T8): `--budget-max N` caps agent invocations per run;
  `--daily-budget N` caps per UTC day (state file
  `.pi-batch/daily-budget.json`, `--daily-state` overrides). On cap the
  run stops with exit 3 and a budget_cap event — the cap wins over
  `--max-rounds 0`. Exit codes: 0 success, 1 max rounds, 3 budget cap,
  4 stall/circuit stop, 5 lock held, 130 interrupt.

## Progressive message memory

`memory.mode: auto` preserves pi's original session JSONL while building a
small append-only index at `.pi-batch/memory/sessions.index.jsonl`. Independent
tasks receive explicit traceable session ids; shared/per-stage sessions retain
their configured ids. The index contains redacted prompt excerpts, domains,
status, output hashes and paths, validator/Gate outcomes, Campaign transitions,
and archive mappings. A failed index write is advisory and never fails a task.

The runner appends a bounded Memory Manifest to each pi prompt. Static routing
marks short questions as direct/discovery and does not include historical
records; resume requests keep a few recent rows, while implementation requests
select domain matches from only the newest `memory.manifest_scan` metadata
rows. Recent/read lookup scans backward from the append-only index tail in
bounded blocks, while find streams forward and keeps only its Top-K matches;
none first materializes the complete metadata history. Individual metadata
lines are capped by `memory.index_line_max_bytes` (256KiB default). The same
LLM decides whether to retrieve more—there is no mandatory extra planner call:

```bash
python pi-batch.py memory ingest
python pi-batch.py memory recent --limit 5
python pi-batch.py memory find "protocol acceptance failure" --limit 5
python pi-batch.py memory read SESSION_ID --max-bytes 65536
```

`memory ingest` incrementally scans the current project's existing pi session
directory. It indexes only new/changed files and extracts static metadata
(domains, roles/message count, token/cost, observed verdicts, and bounded
`errorMessage/stopReason` failures); it never copies or rewrites raw message
content. An imported PASS is deliberately named `GATE_PASS_OBSERVED`, while a
provider/CLI failure is `FAILED_OBSERVED`; neither is promoted to a currently
verified engineering result.

`read` follows only indexed files inside pi's session directory, refuses
symlinks/path escapes, caps returned bytes, and redacts likely secrets by
default (`--raw` is an explicit operator override). Read/import paths use
bounded line buffers and truncate or skip abnormally large JSONL records rather
than materializing them whole. Historical content is fenced by policy as
untrusted evidence. Use `--memory-mode off` for a run that must retain the
legacy invocation unchanged, or `on` to enable an explicitly adapted agent.

Prompt/task/template files are also read through a bounded UTF-8 reader
(`input.max_bytes`, 2 MiB by default). Oversized or invalid-UTF-8 sources fail
the stage before an agent starts; they are never silently treated as an empty
stage or copied into the operational log. Structured Campaign analysis opts
into the same execute hint so bounded prior metadata can inform future planning.
Aggregate evidence retains the separate `evidence.max_bytes` budget and points
to the full artifact for an explicit on-demand read.
Campaign analysis fingerprints include only recent failure-class metadata, so a
new validation/Gate failure triggers re-planning without invalidating reuse for
ordinary successful calls.

## Hetero routing (T11)

Pipeline task definitions may set `provider` / `thinking` / `tools` /
`exclude_tools` (reaching the agent argv like single-batch tasks do);
stages may set `model` / `provider` for their orchestrator and role tasks
(meta stages); CLI `--provider` overrides everything. Precedence:
CLI > task > stage > config default (D6).

## Reuse fingerprints, session rotation, governance (T9, T10, T12)

- **Fingerprint reuse 2.0** (T9): `--reuse-fingerprint` adds a sidecar
  (`<output>.meta.json`, format v1) with sha256(inputs + gate spec +
  routing). Reuse requires exists AND non-empty AND non-symlink AND sidecar
  fingerprint match AND validators still passing; input change -> stale
  artifact + sidecar deleted and regenerated. Old `--reuse` semantics
  unchanged.
- **Session rotation** (T10): past `--session-max-bytes` (default 2MB), a
  shared/per-stage session is forked via `--fork <session-file>` instead of
  growing an unbounded context; pi compaction entries are warned about.
- **Governance batch** (T12): validators may reply with a JSON status line
  (`{"status":"warn"}` allows with warning, `{"status":"fail"}` rejects);
  a bare `VERDICT: PASS` with no reason fails closed (12b); combined
  evidence is fenced as `<evidence source="...">` (12d); outputs over
  `OUTPUT_MAX_BYTES` (default 2MB) are rejected, not written (12e). Agent
  pipes are drained in fixed 8KiB character chunks, so a huge line without
  newlines cannot allocate the entire response before the cap is applied;
  `--validate secretscan` greps artifacts for likely secrets (12f,
  registry hook). Validator stdout/stderr use their own
  `validation.output_max_bytes` bound (256KiB default) and overflow fails closed.

## Approval stages (T12c, decision D5)

`approval: true` on a stage turns it into a human-in-the-loop point: after
its deliverables exist the pipeline pauses until approved. Channels, in
order: `PBATCH_APPROVE=1` env, `--approve-file FILE` (exists = approved,
CI-friendly), or an interactive y/n on a real TTY. Anything else fails
closed (the stage reports unapproved and the pipeline halts; deliverables
are preserved for review).
