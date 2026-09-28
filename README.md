# ai-batch-runner

An interactive terminal workbench and governed batch/pipeline runtime for **any CLI-based
coding agent** — local-first and not tied to one vendor. Point it at `pi`,
[Claude Code](https://github.com/anthropics/claude-code), Codex CLI, Gemini CLI, OpenCode,
or configure `agent.argument_flags` for another argv contract. The built-in adapter accepts:

---

## 快速开始（产品 CLI）

首次公开发行仍受许可证确认门禁阻断；当前请在源码 checkout 根目录安装：

```bash
pipx install .                     # 推荐：隔离安装当前源码
# 首次公开发行后：pipx install ai-batch-runner
pi-batch                  # 默认产品入口：交互式 TUI（真实终端）
pi-batch doctor           # 环境诊断：安装/目录/配置/agent/密钥/锁
pi-batch init             # 创建用户级工作目录骨架
pi-batch version          # 版本 + 环境
pi-batch -p "写一个订单接口，含支付审批，生产，go" --classify   # 一句话 → 全流程
pi-batch completion bash >> ~/.bashrc    # shell 补全（zsh 同理）
```

从源码开发安装使用 `pip install -e ".[dev]"`。维护者发布流程见 [`docs/RELEASING.md`](docs/RELEASING.md)。GitHub Release 会同时提供
Linux/macOS/Windows 的单文件二进制（provider CLI 仍需单独安装）。

手机查看会话和发送指令：

```bash
pi-batch ops serve --control --token-file ~/.config/pbatch/ops-token
# 手机访问 http://<主机>:8731/?token=<token>；跨网络必须再配置 --tls-cert/--tls-key
```

默认只读；`--control` 只允许操作服务端启动时绑定的 workspace/agent，历史会话不可
隐式 resume，详细边界见 [`docs/RELEASING.md`](docs/RELEASING.md)。

跨设备、跨实例会话使用 `pi-batch fleet`：启动 Agent Hub 与各实例的出站 Gateway，
通过 CLI、`fleet tui` 或 Flutter Console 查看授权会话、发送 prompt、接收事件和取消 turn。
`fleet compute` 提供设备资源目录、计算任务与调度 TUI；`fleet agent --compute-tools`
让当前会话 Agent 在授权范围内调度设备，见 [计算配置](docs/agent-hub/COMPUTE.md)。
NVIDIA 物理 GPU 支持按卡数和每卡空闲显存选机、分配展示与执行前复核，见
[GPU 计算](docs/agent-hub/GPU.md)。
支持 Snaplink JWT 和实例授权；本地试用、API 契约及生态接入边界见
[`docs/AGENT_HUB.md`](docs/AGENT_HUB.md)。Aero ID 成员门禁、IM 通知、Audit 入账回执与
`fleet integrations` 运维入口见 [生态集成](docs/agent-hub/ECOSYSTEM.md)。

构建 wheel 安装时会同时携带规则注册表、验证脚本、默认流水线、角色模板和
评估集；`pi-batch health --json` 会检查分发资产是否完整。

`pi-batch pool` 提供可选的 OpenCode Go 账号池：本地模型网关在明确配额拒绝后
切换到其他配额组，共享并发/冷却状态；可启用三窗口额度预检，提供 CLI 刷新及
只读 MCP 状态/额度查询；`pool init`、`pool account` 支持带预览和备份的账号配置管理。
`pool doctor` 提供只读本地接入诊断，可显式探测网关，不调用模型或查询供应商。
`doctor --gateway` 同时核验运行中的配置与启用凭据快照，识别改账号/轮换密钥后未重启的旧网关。
`pool run --model MODEL -- opencode` 为单次命令合并接入配置，支持脱敏预览，不修改
全局环境或认证文件；网关需单独启动。配置、凭据导入与接入见
[LLM 账号池](docs/LLM_ACCOUNT_POOL.md)。

### 用户级工作目录（XDG 兼容）

| 目录 | 用途 |
|---|---|
| `~/.config/pbatch/settings.yaml` | 用户配置（跨项目偏好） |
| `~/.cache/pbatch/` | 可丢弃缓存 |
| `~/.local/share/pbatch/keys/` | 设备身份密钥（跨项目复用） |
| `~/.local/share/pbatch/logs/` | 兼容的全局运行日志 |
| `~/.codex/pbatch/projects/<workspace-id>/` | 默认项目运行态：会话、记忆、被拒诊断、campaign 报告/状态、worktree、日志、实例锁 |

运行态默认放在用户目录，不会因为执行目录是项目根目录而生成 `.md`、日志或
`.pi-batch/` 数据。`PBATCH_RUNTIME_HOME` 可改变默认运行根；`PBATCH_DATA_DIR`
或 `--data-dir` 可精确指定单个项目运行目录。升级时不会自动删除旧 checkout
中的 `.pi-batch/`、`docs/auto/` 或 `logs/`，避免破坏未提交证据；确认后可人工迁移/清理。
**配置分层**：
CLI 参数 > 项目 `pi-batch.yaml` > 用户 settings.yaml > 内置默认。

仓库中的 `docs/`、`prompts/`、规范资产等是发行包必需的只读资源；这里的隔离针对
运行时生成物，不会删除或移动项目明确指定的交付物路径。

配置变更建议先做只读核验：

```bash
pi-batch config explain       # 来源、覆盖关系、脱敏后的最终配置与运行时值
pi-batch config check         # 版本/结构校验，适合 CI
pi-batch config check --file pi-batch.yaml --json
```

`pi-batch.yaml` 使用 `config_version: 1`。缺省版本按 legacy v0 兼容；高于当前支持版本
会 fail closed。`config explain` 不创建目录、不启动 agent；报告中的敏感键（token、password、
private key 等）只显示 `<redacted>`。

### 功能全景

- **50+ 子命令**：需求评估（assess/classify/profile/probe）→ 流程编排
  （campaign/org/advance/race/pareto）→ 规则知识（rules/learn/adr/context/
  memory/capsule/replay）→ 治理（truth/causal/temporal/pinned/metrics/
  budget/recovery/events/world）→ 设备织网（devices/serve/runner）→ **实战规律
  （evolution：形态分类/规律分级路由/项目宪法/管线形状推荐）**
- **1086+ 回归测试 + 42 eval 用例**；`pi-batch check` 8 门禁聚合自检
- 零 LLM 确定性核心：所有检测/路由/校验纯 stdlib + ast，可测试、fail closed
- Meta 流水线可把规划器与并行角色分配给不同模型；`delegation_only` 在适配器层禁用规划器工具，见 [`docs/MODEL_DELEGATION.md`](docs/MODEL_DELEGATION.md)
- 详见 `docs/`（AADM 理论四卷、AGENT_ORGANIZATION、CAMPAIGN_LESSONS）与 `AGENTS.md`

```
<bin> -p PROMPT [--model M] [--provider P] [--thinking T] [--tools T] [--exclude-tools T]
```

and it will run one prompt, a batch of prompts (serial or parallel), or a multi-stage pipeline
against it, with per-task output capture, engineering validation gates, session reuse,
exponential-backoff retries, and optional auto git-commit after each stage.

> **Provenance**: this tree is a sync of `snaplink/ai-dev` (the living monorepo copy), carried
> over as-is including its regression test suite. The engine now lives in the `pbatch/`
> package with `pi-batch.py` as a thin entry shim (the reference's current layout; the
> earlier 4-file split and the 1389-line monolith were both superseded). `tests/`
> is the authoritative behavior spec.
>
> **Local adaptations** (everything else stays byte-identical): `pi-batch.yaml`
> `validators.pyquality` points at `{cwd}/quality.py` (reference used an `ai-dev/`
> prefix from snaplink's repo layout); **all `ai-dev/` path prefixes were stripped
> from commands, prompts, docs and code comments** — this repo's root IS the
> `ai-dev` content, so `python ai-dev/pi-batch.py` → `python pi-batch.py`,
> `ai-dev/prompts` → `prompts`; upstream's `make ai-dev-test` → this repo's
> `make ci`. Only historical upstream-name mentions (`snaplink/ai-dev`) remain.
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
>   refreshed
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
>   stall detection (`--stall-rounds`, exit 4), residue markers in the
>   private runtime `state/`, environment-class halt (shared validator stderr)
> - tests: 12 more cases (42 total in `tests/test_local_fixes.py`)
>
> **Local fixes** (cost governance, T6-T8 per `docs/IMPLEMENTATION_PLAN_7x24_GOVERNANCE.md`):
> - T6 spike verified pi session files (`~/.pi/agent/sessions/--<cwd>--/`,
>   `message.usage` JSON pointers) — report in `docs/spike-pi-session.md`
> - T7 `pbatch/metering.py`: `--events-file` JSONL events (task_finish/
>   task_fail/budget_cap) with per-call session usage diff; each event carries
>   `schema_version`, `event_id`, `run_id`, `task_id`, `stage_id`, `attempt`
>   and `phase`; `--run-id` can supply an external correlation id; `--webhook`
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
>   (`<output>.meta.json`, sha256 of inputs/gate/agent/routing plus the
>   accepted artifact bytes); input, executor, or artifact change regenerates;
>   wired through all six reuse sites + single-batch
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

> **Repository campaigns and relevance-bounded meta roles**:
> - `pi-batch.py campaign` discovers project modules, runs parallel structured
>   analyses, scores/deduplicates evidence-backed directions, and executes the
>   selected directions through a full SDLC pipeline.
> - campaign state/artifacts default to `~/.codex/pbatch/projects/<workspace-id>/campaign/`;
>   `state.jsonl` is the append-only source of truth and `SUMMARY.md` is
>   regenerated from it. Input fingerprints make `--reuse`/`--skip-passed`
>   freshness-aware instead of trusting a non-empty Markdown file.
> - analysis parallelism stays inside the lock-owning process. Parallel
>   implementation uses one Git worktree, branch, output tree, decision log,
>   and run log per direction, all outside the repository by default. Isolated
>   children carry bounded process leases
>   and can use an evidence-based no-progress watchdog without changing
>   provider-side cancellation semantics.
> - `--auto-resume-on-signal` is opt-in: SIGTERM stops child groups, preserves
>   append-only state, and resumes the round loop after `--resume-delay`.
>   The latest campaign summary includes bounded wall-time, retry, gate-ratio,
>   token-per-successful-direction, stall, and concurrency-slot observations.
> - meta stages score role templates with `pbatch/role_keywords.yaml`, inject
>   matched-keyword evidence, deduplicate the model plan, and enforce
>   `max_roles_per_iteration` at runtime.

## Switching agents

The agent binary is a config value, not a hardcoded dependency — edit `agent.bin` in
`pi-batch.yaml`, or pass `--agent-bin` on the command line:

```yaml
# pi-batch.yaml
agent:
  bin: pi              # or: claude, codex, gemini, opencode, ...
  default_model: "gpt-5.6-luna:max"
  default_timeout: 300
  default_workers: 4
  argument_flags:          # per-agent overrides are also supported
    prompt: "-p"           # use "" for a positional prompt
    model: "--model"
```

Before a non-dry run starts, the CLI checks that the selected executable is
available. Project validators use `project.frontend_root`, `backend_root`, and
`report_root`; relative values resolve against the task working directory.
Regular wheel installs also carry the default pipelines, role prompts, rule
registries, eval suites and checker scripts. Project-local assets take
precedence; installed assets are the fallback, so `pi-batch rules`, `eval`,
`check`, `campaign`, and classifier routing work outside this checkout.
`pi-batch health` is also runnable from a wheel, but its default self-check
honestly reports unhealthy when the wheel has no source tests (tests are not
shipped); use `pi-batch health --dir /path/to/project` to assess a target project.

Configuration sections are type-checked at startup. Malformed section/value
types are reported and replaced with bounded built-in defaults, so a typo does
not crash the long-running coordinator during module import.

```bash
python pi-batch.py tasks.yaml --agent-bin claude --mode parallel --workers 4
python pi-batch.py tasks.yaml --agent-bin codex  --mode serial
```

## Quick start

```bash
# Install as a global CLI command (like pi/codex), then use it anywhere:
pipx install .             # from the current source checkout
# After the first public release: pipx install ai-batch-runner
pi-batch                    # interactive TUI on a real terminal
pi-batch tui                # explicit TUI entry
pi-batch -p "analyze this project" -o output.md
python -m pbatch ...        # same entry, no install needed
python pi-batch.py ...      # zero-install fallback from the repo root

# Canonical earendil-works/pi namespace (arguments are forwarded verbatim)
pi-batch pi                         # official Pi interactive mode
pi-batch pi --print "summarize this repo" # official Pi one-shot text mode
pi-batch pi --mode json "review"    # official Pi JSON event stream
pi-batch pi --mode rpc              # official Pi LF-delimited JSONL RPC
pi-batch pi -c                      # official Pi session continuation

# Task type classifier: decide BEFORE execution whether a task is frontend UI
pi-batch classify "用 Flutter 实现 ERP 排程列表页"
pi-batch classify tasks.yaml --json        # machine-readable

# Requirement assessment: prescription + productization + workflow level
pi-batch assess "企业订单管理：新建订单表单，含支付审批流程"
pi-batch assess --file requirements.md     # from a document
pi-batch assess "..." --prompt             # compiled instruction block for the agent
pi-batch assess "..." --json
pi-batch assess "..." --product-brief     # structured product direction proposal

# Product direction: deterministic brief for review (no silent business decisions)
pi-batch product "做一个多租户审批平台，支持登录和审批流程"
pi-batch product --file requirements.md --json -o docs/product-brief.json
# TUI 中也可用：/product [requirement]（省略时使用当前会话最近一条任务）
# 退出重开后：/sessions 打开 scope 内 picker；Enter 恢复，f fork，a archive/restore

# Rule matching (scale × page_type × risk) + two-sided LLM check
pi-batch rules "生成一个登录页 demo"
pi-batch rules --check                     # registry schema integrity (three domains)
pi-batch rules "..." --summary | --llm-prompt p.txt | --llm-json '{...}'

# Context routing + evolution + self-check
pi-batch context "任务" --paths src/order/**   # project documents for a task
pi-batch learn "订单并发创建重复记录"            # RCA -> rule draft (--from-memory TERM)
pi-batch eval                                  # rule-system regression suite (42 cases)
pi-batch check                                 # one-command self-check
pi-batch tasks.yaml --validate 'jsonschema=schemas/result.schema.json'

# Frontend task: --classify routes it to the frontend implementation
# pipeline (real code in the project; plan -> implement with
# uiquality/uicheck/completion gates -> adversarial review -> verdict)
# (classifier.frontend_pipeline), which applies ui-specs + UI validators
# (uispacing/uicolor/uistyle/uifont) + meta UI review + verdict gate
pi-batch -p "生成一个登录页面 tsx" --classify --dry-run

# Batch from a YAML task file
pi-batch tasks.yaml --mode parallel --workers 4

# Multi-stage pipeline (chained roles, e.g. architect -> tech-lead -> implementer -> qa -> security)
pi-batch --pipeline pipelines/pipeline-code-impl.yaml

# 24x7 loop: rerun failed tasks until everything passes
pi-batch tasks.yaml --max-rounds 0 --round-delay 60

# Repository campaign: inspect the plan, then analyze and implement Top N directions
pi-batch campaign --dry-run
pi-batch campaign --max-directions 2 --reuse --retry-failed

# Source-first project comparison / migration / upgrade rehearsal (no code mutation)
pi-batch migrate compare --source ../reference --target . --manifest migration.yaml --json
pi-batch migrate plan --source ../reference --target . --manifest migration.yaml --json
pi-batch migrate rehearse --source ../reference --target . --manifest migration.yaml --budget-minutes 180 --json
pi-batch migrate upgrade --baseline ../old --target . --manifest migration.yaml --strict

# Project Studio：业务→领域→产品→UX→架构→数据/API→实施 DAG
pi-batch studio plan --project . --spec studio.yaml --json
pi-batch studio validate --project . --spec studio.yaml --strict
pi-batch studio rehearse --project . --spec studio.yaml --budget-minutes 240 --json

# Focused adaptive module plan: message/content relevance + observed telemetry.
# No fixed token budget is invented; provider/daily invocation caps and the
# campaign YAML batch template are used, while real input/output/total usage is
# recorded for the next decision.
pi-batch campaign --rounds 0 --reuse --retry-failed \
  --focus "journal recovery" --analysis-batch-size 4
# 24x7 incremental campaign: concurrent analysis/meta roles, bounded provider policy,
# and max reasoning (implementation remains worktree/commit governed)
pi-batch campaign --rounds 0 --round-delay 120 --jobs 4 --max-directions 1 \
  --model gpt-5.6-luna --provider openai-codex --thinking max --reuse --retry-failed
# Optional artifact-progress watchdog and bounded SIGTERM recovery:
pi-batch campaign --rounds 0 --reuse --retry-failed \
  --no-progress-timeout 7200 --auto-resume-on-signal --resume-delay 30 \
  --max-signal-resumes 3

# Bounded campaign repair: preserve failed edits without stash/reset, narrow
# one clean retry, preflight repo gates, and stop on repeated identical failures.
# See docs/CAMPAIGN_SELF_REPAIR.md and examples/repository-campaign.yaml.
pi-batch campaign --config examples/repository-campaign.yaml \
  --rounds 0 --max-directions 1 --max-total-directions 1 \
  --reuse --retry-failed

# Parallel module collaboration from a dirty baseline (one external worktree per agent)
pi-batch collab --root /path/to/project --plan /path/to/collab.yaml --dry-run
```

The canonical Pi contract is
[`earendil-works/pi`](https://github.com/earendil-works/pi). `pi-batch pi`
does not reinterpret Pi options, so its interactive, print, JSON, RPC, auth,
package, export, resource, and session calls remain owned by upstream Pi.
The invocation boundaries are:

| Need | Entry | Owner and behavior |
|---|---|---|
| Pi's own terminal, one-shot text, JSON events, raw RPC, auth or package commands | `pi-batch pi [PI_ARGS...]` | Upstream Pi CLI; argv and process streams pass through, with no pi-batch gate, retry, journal, or result rewriting |
| Governed batch or pipeline | `pi-batch -p PROMPT` / `pi-batch tasks.yaml` | pi-batch runner; validators, retries, gates and `--mode serial\|parallel` apply |
| Product workbench backed by Pi | `pi-batch tui --runtime pi-rpc` | pi-batch TUI/journal/inspectors over a managed local `pi --mode rpc` subprocess |
| Python embedding | `pbatch.create_agent_session(...)` | Synchronous, provider-neutral pi-batch facade; Pi selection uses RPC and is not a Python port of Pi's SDK |
| Node/TypeScript embedding | `@earendil-works/pi-coding-agent` | Official in-process SDK; use upstream `createAgentSession()` or, for session replacement, `createAgentSessionRuntime()` directly |

The namespaces are intentional. In the governed CLI, `-p` consumes the prompt
value and `--mode` means `serial|parallel`. After `pi`, `-p/--print` is Pi's
boolean one-shot flag, messages remain positional, and Pi owns its parser-level
`--mode text|json|rpc` values (`json` is an event stream; `rpc` consumes protocol
commands on stdin). The Pi executable is selected from non-empty
`PBATCH_PI_BIN`, then a configured agent whose basename is exactly `pi`, then
`pi` on `PATH`; a configured Codex/Claude binary never receives Pi arguments.
The Python facade also exposes normalized `runtime_state()`, `stats()`, model
and thinking catalogs, and long-running `compact()` controls. It probes the
exact Pi executable once: stable Pi 0.84.3+ automatically enables session-only
model/thinking mutation; older, prerelease, or unverified versions remain
fail-closed. Python callers may explicitly set `allow_persistent_settings=True`
only when they knowingly accept an older Pi's global-settings side effect.
See
[`T29 · Pi canonical 调用面与 RPC Runtime`](docs/tui-phase0/t29-pi-rpc.md).

### Interactive terminal UI

Running `pi-batch` without arguments opens the Textual workbench when both
stdin and stdout are interactive terminals. Use **Ctrl+P** for the command
palette, **Ctrl+N** for a new task, **Ctrl+L** to clear the transcript,
**Ctrl+G** for the Changes inspector, **Ctrl+T** for Timeline,
**Ctrl+C** to cancel the active run, and **Ctrl+Q** to quit.

Slash commands are available from the prompt: `/help`, `/status`, `/session`,
`/compact`, `/new`,
`/sessions`, `/resume`, `/fork`, `/archive`, `/unarchive`, `/clear`, `/model`,
`/thinking`, `/provider`, `/agent`, `/runtime`, `/memory`, `/queue`, `/steer`, `/approvals`,
`/agents`, `/runs`, `/files`,
`/diff`, `/plan`, `/completion`, `/artifacts`, `/changes`, `/product`, `/review`, `/doctor`,
and `/quit`.
`/help` shows the current syntax and state-dependent behavior.
Slash help, dispatch, the command palette, and busy-state policy share one
immutable command registry. Type a command-name prefix and press **Tab** or
**Right** to complete it; Enter completes a partial name before submission,
while an exact name submits normally. This static completion path is local: it
does not call RPC, rebind a runtime, or execute a command. Typing into an exact
`/model QUERY` or `/thinking QUERY` argument context loads at most one bounded,
read-only active-session snapshot and opens the local fuzzy picker; **Tab** can
explicitly start completion for a pasted context. Edits within that same
adapter/session/kind context filter the frozen snapshot without per-keystroke
RPC. Model ranking uses Pi's model search fields and fuzzy score; thinking
completion searches only the provider-returned level strings. **Tab** or
**Enter** in the open picker only fills the canonical command text. It does not
submit or call a setter; press **Enter** again to enter the normal slash handler,
which revalidates the current catalog, Pi version, settings scope, and busy
state before any mutation. An older Pi or `settings_scope=global` runtime may
therefore still provide read-only completion choices, but cannot reach a
setter. The palette also exposes typed session, model, and thinking controls
under the same gates.

Pi's interactive completion prefers `session.scopedModels` when configured,
whereas RPC `get_available_models` returns all configured models
([upstream implementation](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/modes/interactive/interactive-mode.ts#L646-L683),
[RPC contract](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/rpc.md#L255-L327)).
The latter is suitable for suggestions, but is not evidence of scoped-model
membership or cycle order. Pi's `/scoped-models` remains unexposed because
JSONL RPC has no authoritative scoped-list read/write contract.
With a capable managed runtime, `/session` reads typed active-session state and
statistics without loading the message history or exposing the provider session
file. `/compact [instructions]` performs Pi's lossy older-context compaction in
the background. It is allowed only while the session is idle and holds a
separate logical session lock; Pi RPC has no `abort_compaction`, so Ctrl+C does
not pretend to cancel it.
While an agent turn is running, plain text enters a bounded process-local
follow-up queue; use `/queue` to inspect or edit it. A successful turn starts
the next item automatically, while failure or cancellation preserves and
pauses the queue. `/steer TEXT` is available only when the active runtime
declares `turn_steer` and exposes the matching live turn handle.
Use `pi-batch tui --runtime app-server` (or idle `/runtime app-server`) for the
bidirectional Codex App Server path. Trusted server approval requests open a
typed modal; `a` allows once, `s` allows for the session when offered, `d` or
Enter denies, `i` interrupts, and `/approvals` reopens a pending card. Decisions
are scoped to the exact live turn and request id; transcript text cannot create
an approval.
`/sessions` opens only locally scoped journals. It reads a byte-bound local
summary cache and, in App Server mode, enriches exact provider-handle matches
with one bounded `thread/list` page off the UI thread. Provider names, previews,
and statuses are advisory: remote-only threads are not added, pagination is
shown, and Enter still replays the journal and performs `thread/read` before
any resume.
`/agents [agent]` opens a bounded, read-only Agents inspector (with `/runs` as
an alias): it combines the current coordinator operation, the trusted active-turn
Actor Tree, retained typed actor-history groups, and recent terminal records from
the scoped shared ledger. Press `h` to cycle retained historical trees. It supports
agent/status filters and manual refresh, and never copies inspection output into
the conversation transcript. Child lineage is accepted only from typed events;
without an explicit actor-control capability and matching actor identity, no
single-actor stop/steer action is offered.
`/findings` opens structured review findings and Enter safely jumps to the matching
workspace-relative file/line in the read-only Changes inspector. Press `f` on a finding
to start a normal, bounded agent turn that treats review text as untrusted data and
follows the ordinary permission/validation path; it is not an automatic fixer and
never marks the finding resolved. After the turn, a bounded user-level runtime `reviews/review-fix-<id>.md`
report records only exact terminal RuntimeEvent associations and typed validation/gate/workspace
facts; missing identity stays unavailable. `/completion` opens the current-turn evidence report
and updates it in place:
outcome, commands, authoritative Git changes, unfinished structured plan steps,
validation/gate facts, tools, artifacts, risks, and usage remain separate from
Assistant prose. Missing validator or gate events are shown as unavailable,
never inferred as passing.
`/artifacts [N]` (or `/artifact [N]`) opens only explicit current-turn
`artifact.created` references. UTF-8 regular files inside the current workspace,
or inside the locally resolved data directory when the typed reference explicitly
uses `scope=data`, receive a 64 KiB no-follow preview. Remote URIs, outside paths,
symlinks, directories, and binary files remain metadata-only or blocked. The
inspector does not scan Assistant prose for paths, fetch URLs, launch external
viewers, or add its content to the transcript. From Completion, press `a` to open it.
Codex Structured Exec and App Server automatically promote command output beyond
the event preview budget to a private data-dir ArtifactRef. Defaults cap one file
at 2 MiB and each workspace/session ring at 64 MiB or 256 files.
The Changes inspector supports `all`, `unstaged`, `staged`, `untracked`, and
`last-turn` scopes plus validated `base REF` / `commit REF` comparisons. Its
file list and `n`/`p` hunk navigation remain bounded and read-only.
The native pipeline observer can emit bounded Pipeline/Stage/Task/validation/gate
facts through `run_pipeline(..., event_sink=...)`. In the TUI, `/pipeline FILE [--reuse]`
or `pi-batch tui --pipeline FILE` starts an explicit workspace Pipeline and projects
its typed lifecycle read-only; approval decisions use an opt-in, run-scoped
native IPC channel; Ctrl+C cancels the active local task and stops later
stages cooperatively, without sending provider stop/cancel. The
full startup, trust-boundary, and release contract is documented in
[`docs/TUI_PRODUCT.md`](docs/TUI_PRODUCT.md).

The automatic TUI never activates when either stdin or stdout is redirected,
so existing scripts and CI remain headless-compatible. Piped prompts and all
explicit batch/subcommands keep their original behavior:

```bash
printf 'analyze this repository\n' | pi-batch
pi-batch -p "analyze this repository" --stream-output none
pi-batch tui --help
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

- **Task type classifier**: `--classify` + `pi-batch.py classify` subcommand run a
  deterministic bilingual keyword gate BEFORE execution (zero LLM cost); a
  frontend-dominated batch routes to the configured UI generation pipeline
  (`classifier.frontend_pipeline`, default
  `examples/frontend-implementation-pipeline.yaml`).
  Keywords are additive-extensible via `pbatch/task_keywords.yaml`; platform
  mention (tsx/dart/flutter/vue/react-native) boosts the frontend decision;
  explicit `--pipeline` always wins; missing pipeline file fails loudly.
- **Rule matcher** (`pi-batch rules`): the spec system is large, so a small demo
  must not load all of it. `pbatch/rule_matcher.py` + `ui-specs/rules.yaml`
  algorithmically select the applicable rules along three axes — scale
  (demo < standard < production, default standard), page type (form/table/
  detail/workbench/immersive/auth) and risk (high-risk pay/approve/delete
  flows force error-recovery + defect-patterns). Output is a bounded manifest
  (paths + why + skipped reasons); prompts inject ONLY the manifest and agents
  load files on demand (progressive disclosure).
- **Backend architecture governance** (`backend-specs/` + `backendquality`): the
  same layered machinery now covers backend work — the classifier already
  separates backend tasks, and the rule matcher/assessor pick the backend
  registry automatically (`backend-specs/rules.yaml`, signals: use
  cases/entities/integrations/distribution). Specs: architecture (module
  organization by business capability, Transport → Application → Domain ←
  Infrastructure dependency direction, data ownership, composition over
  inheritance), design-patterns (pattern selection DECISION TABLE keyed on
  problems, over-engineering red lines — no interface per class, no factory
  per object, no BaseService/Manager soup, rule of three), domain-modeling
  (state machines centralized, domain objects protect invariants, UoW at the
  use-case boundary), evolution (refactor triggers, ADR, tech debt, migration
  windows). Roles `backend_engineer`/`design_pattern_reviewer` (default
  stance: every pattern is guilty until it proves it solves a real problem).
  `backendquality` validator mechanically enforces over-engineering signals
  (single-implementation interfaces, Base classes, fuzzy names), god-service
  signals, scattered status assignment, domain→infrastructure imports, and
  complexity signals (N+1 loops, full-sort Top K, O(n) shift queues);
  `examples/backend-engineering-review.yaml` runs the audit loop.
- **Computer science base** (`backend-specs/oop-and-di.md`, `ddd.md`,
  `algorithms-data-structures.md`, `complexity-and-scale.md`): the backend
  agent must judge object-vs-function, encapsulation (business-semantic
  methods, never setter soup), IoC/DI lifecycle (constructor injection, no
  Service Locator), AOP boundaries (never hide inventory/state/payment rules
  in aspects), DDD with usage LEVELS (L0 transaction script → L3 full DDD,
  never default full DDD), the data structure decision table (Map for key
  lookups, Heap for Top K, Bloom Filter only for false-positive-tolerant
  prefiltering), BFS/DFS choice, five cost classes (CPU/memory/database/
  network/distributed coordination), and ×10/×100 scale reasoning. The
  `algorithms` rule triggers only on search/sort/graph/schedule/cache/range
  tasks; `ddd`/`complexity-scale` are production-tier. Roles include
  `algorithm_reviewer` (default stance: every hot path is guilty of O(n²)/
  N+1/scale blindness until proven otherwise).
- **Persistence modeling gate** (`backend-specs/persistence-modeling.md`):
  the data objects being persisted decide five to ten years of maintenance
  cost, so persistence design is a MANDATORY pre-code gate: four models stay
  separate (API DTO / Domain Model / Persistence Model / Read Model — never
  return ORM entities), identity decision table (surrogate PK vs UUIDv7/ULID/
  Snowflake vs business key vs idempotency key), money as minor-unit BIGINT
  or DECIMAL (never float), time semantics, status codes with a central
  state machine, snapshot fields for historical integrity, NULL semantics,
  index design driven by access paths, database unique constraints as the
  final idempotency line, audit/status-history tables, optimistic locking,
  soft-delete trade-offs, and Expand–Migrate–Contract migrations. The
  `backend_engineer` role must output a Persistence Design report (aggregate/
  tables/identity/consistency/snapshots/concurrency/queries+indexes/history/
  deletion/migration) before any table or ORM entity.
- **Production readiness + guardrails** (`backend-specs/production-readiness.md`,
  `agent-guardrails.md`): an 11-section readiness gate (requirement
  correctness, contracts/events, stable error model, reliability per
  dependency, capacity budgets with bounded pools, rate limiting/cache
  consistency/message semantics/background jobs, multi-tenant scoping,
  security & privacy, observability, five-layer testing with honest
  verification reporting, graceful shutdown & migration windows) plus AI-
  specific guardrails (no hallucinated APIs/results, change-radius control,
  don't-DRY-away-boundaries, no perf claims without benchmarks). The
  `backendquality` validator adds float-money, SELECT *, tenant-less WHERE
  id, LIKE-% full scans, VARCHAR(255) sprawl, swallowed exceptions, per-request
  HTTP clients and busy-polling loops to its strict warnings.
- **Senior architecture constitution** (`backend-specs/architecture-constitution.md`):
  architecture as change management — Change Amplification as the key metric,
  decision reversibility register (PKs/public APIs/event formats/service
  boundaries need evidence before commitment), organization fit & cognitive
  load, modular monolith as the default (distributed-monolith is the anti-
  pattern), unique data ownership with derived data marked, the coupling
  octet (code/data/temporal/order/deployment/semantic/organizational/runtime),
  blast radius & bulkheads, tail latency & queueing theory, retry budgets &
  end-to-end timeout allocation, explicit consistency models (PACELC),
  contract evolution with deprecation, delete-ability, designed manual
  intervention, STRIDE threat modeling, TCO, architecture fitness functions
  (automated gates), and the senior-architect 15 questions.
  `architecture_constitution_reviewer` role audits the design against it
  (production tier rule only).
- **Testing & system engineering** (`backend-specs/testing.md`,
  `system-engineering.md`, `network-engineering.md`): five-layer testing
  (unit/integration/contract/E2E/non-functional) with reproduce-first bug
  fixing, risk coverage instead of line coverage, property/fuzz testing,
  and honest verification (no assertion-less tests — the `backendquality`
  gate flags them); message semantics (at-least-once consumers must be
  idempotent, DLQ for poison messages), cache consistency patterns, long
  task state machines (never is_running booleans), rate-limit algorithm
  decision table, distributed-lock alternatives before locks (unique
  constraints/atomic updates/queues; fencing tokens if used), graceful
  shutdown; layered timeouts (DNS/connect/TLS/request/response/deadline),
  client reuse, the retry matrix (bounded budget, never blind retries of
  non-idempotent writes), streaming with backpressure. `testing_reviewer`
  role audits risk coverage and verification honesty.
- **Backend implementation pipeline + full classifier routing**: `--classify`
  now routes frontend tasks to `examples/frontend-implementation-pipeline.yaml` AND
  backend tasks to `examples/backend-implementation-pipeline.yaml`
  (`classifier.backend_pipeline`) — plan stage runs `pi-batch assess` and
  mandates the Persistence Design report before any code, the implement
  stage is double-gated (`backendquality` + `completion`), followed by
  meta adversarial review and a VERDICT gate. `check-frontend-quality`
  gained useEffect listener/timer cleanup detection.
- **Context routing & rule-system evals** (`pbatch/context.py`,
  `pbatch/eval.py`): `pi-batch context "<task>" --paths src/order/**` loads
  only the project documents a task needs (ADRs/contracts/security
  baselines from `docs/agent-context/context-map.yaml`, path glob + task
  keywords, missing files warned); `pi-batch eval` runs the evals/*.yaml
  regression suite against the live classifier/matcher/assessor (42
  cases encoding every regression we have actually hit — saas → L3,
  login → high risk, flutter+backend → frontend, restrained
  prescriptions) and is wired into make ci, so a keyword or registry
  change that breaks behavior fails the gate. Dangerous DDL
  (DROP TABLE/COLUMN/TRUNCATE outside migrations) added to the
  backendquality errors.
- **Product thinking** (`pbatch/product.py` + `pbatch/product_direction.py` + `product-specs/`): the agent
  must think as a product owner, not a code generator — but depth is decided
  by the productization level (L0 local_feature → L3 product_feature),
  never by keyword greed. `pi-batch assess` reports the level with
  evidence; `pi-batch product` turns L1-L3 requirements into a bounded,
  reviewable product-direction brief (direction/user story/journey/state
  machine/Epic→Feature→Story→Task/business-rule questions); unknown decisions stay
  `待确认`; L0 gets an explicit "no product structure" restraint, while L2/L3
  trigger implicit-requirement scenario chains (login/upload/approval/
  chat/ERP/search) that produce QUESTIONS to confirm (never features to
  implement silently), low-cost reservations (tenant_id in queries/indexes/
  events, audit fields, API versioning — mandatory) vs high-cost
  implementations (Billing/Subscription/marketplace — only with an explicit
  commercialization signal), open-source readiness (README/LICENSE/
  CHANGELOG/CONTRIBUTING/CI/SemVer — only with open-source intent), and
  the completion-evidence report (commands executed vs not_executed,
  no fabricated verification results). `product_thinker` role audits the
  level, the chains and the restraint lines.
- **Requirement assessment** (`pi-batch assess`): rules are prescribed by an
  expert evaluation of the REQUIREMENT, not by keyword hits. Eight
  completeness dimensions are checked (goal/user role/main flow/data source/
  permission/error path/acceptance/tech stack) with per-missing suggestions;
  a scale signal (S/M/L) counts real forms/tables/flows/modules and caps the
  effective prescription tier below the keyword-matched tier — "企业登录页"
  stays demo-tier with 2 rules even though the prompt says 企业. The report
  lists the prescription, an explicit "deliberately not selected" list with
  reasons (restraint), and requirement gaps to fill. `--llm-json` runs the
  two-sided check on top (REQUIRED rules veto-protected).
- **Two-sided rule check** (`rules --summary/--llm-json/--llm-prompt`): the
  algorithm compresses the requirement text (salient classification axes +
  business terms + bounded original tail) into the LLM's input; the LLM
  independently picks apply/skip; `reconcile` merges both sides — agreed rules
  stay with `[both]` provenance, LLM additions are admitted as `[llm-added]`,
  optional skips are dropped and recorded, and skips of REQUIRED or
  risk-mandated rules are **vetoed by the algorithm** (fail closed, marked
  `[algorithm-vetoed-llm-skip]`); malformed LLM JSON exits 2. The generation
  pipeline runs this check before writing code.
- **UI generation gates**: `uispacing`/`uicolor`/`uistyle`/`uifont` validators
  (`scripts/check-ui-spec.py`, pure stdlib) mechanically reject magic spacing
  values outside the 8pt token set, undefined numeric Iris spacing variables
  such as `var(--iris-space-4)`, typography values outside the token family,
  hardcoded colors and inline styles —
  spacing quality becomes a hard gate (fail closed, validator stderr feeds the
  retry prompt) instead of a prompt wish. The same script is a **project-wide
  gate** (gofmt -l style): `--dir src --all` scans an existing frontend tree
  (`uicheck` validator, JSON report, vendor/build dirs skipped, CSS Iris
  spacing refs included in spacing/all mode, no frontend files → pass), and
  `examples/ui-refactor-pipeline.yaml` drives an automatic
  refactor loop — agent fixes violations, the scan re-verifies, stderr feeds
  the retry prompt until 0 violations, then meta review + verdict gate.
  Spec assets live in `ui-specs/`
  (tokens/spacing/component/layout/anti-patterns/business-profiles); roles
  `ui_designer`/`ui_reviewer`/`visual_reviewer` are registered for meta stages.
- **Frontend engineering governance** (the "10 years of experience" codified):
  `ui-specs/engineering/` holds decision tables instead of essays — async-data
  (debounce vs throttle vs submit-lock by event source, race prevention,
  idempotency, retry matrix), form-table-state, error-recovery (401–5xx →
  user action, 409 version conflicts, uncertain timeouts), architecture-budgets
  (dependency direction, god-file criteria, complexity budgets), and a defect
  pattern library (12 incident archives: async-race, duplicate-submit,
  stale-closure, effect-loop, listener-leak, ...). `uiquality` validator
  (`scripts/check-frontend-quality.py`) mechanically enforces the checkable
  half (console.log/any/unsafe html always fail; god-file/complexity budgets
  under --strict). Roles `frontend_engineer`/`async_reviewer`/
  `code_architecture_reviewer` attack the judgment half, and
  `examples/frontend-engineering-review.yaml` runs the full audit loop
  (inventory → meta adversarial review → uiquality gate → verdict).
- **Task loading**: YAML / JSON / plain-text task files, `--from-dir` (one task per `.md`),
  stdin, inline `-p`. `@file.md` references in prompts are inlined automatically. Structured
  files and pipelines are schema-checked fail-closed (types, sources, upstream stage names,
  positive limits, task validators, and prompt-template paths) before an agent starts.
- **Execution**: serial or parallel (`--workers`), TTY-aware live output
  (`--stream-output auto|full|none`), per-task `cwd`/`timeout`/`env`, summary report,
  `--dry-run`. Parallel execution performs a fail-closed preflight for duplicate or
  nested outputs and output paths contained by another task's distinct `cwd`; callers
  that provide `run_parallel(waves=...)` get non-conflicting wave barriers. `auto` keeps
  model bodies out of non-interactive operational streams. Plain-text adapters write their
  response body to the artifact. Adapters declaring `output_protocol: codex-jsonl` parse
  every event fail-closed and write only the final completed assistant message; the raw
  transport remains in the agent-native session rather than the artifact or terminal stream.
- **Failure detection**: provider/CLI error signatures (quota, rate limit, auth, billing,
  network/DNS/TLS) are matched against operational output — a non-zero exit, empty output,
  or such a reply marks the task failed and **never saves** the artifact. Structured Codex
  stdout contributes only typed `turn.failed`/`error` payloads to provider-health matching;
  command output and assistant prose are never interpreted as provider failures.
- **Retries**: `--retries N` with exponential backoff (`--retry-delay`, `--retry-backoff`);
  rate-limit/network failures wait at least 30s so quota windows clear.
- **Rounds (24x7)**: `--max-rounds N` reruns the batch each round (0 = loop forever until all
  pass), `--round-delay` rest between rounds, `--log-file` appends run logs for supervision.
  Invalid negative/zero limits and non-finite delays are rejected with exit 2 before locks,
  logs, or subprocesses are created.
  `--reuse` skips only non-empty, non-symlink outputs that still pass their current
  validators, so rounds regenerate stale artifacts and otherwise rerun only failures.
- **Sessions**: `--session-mode new|shared|per-stage` with `--session-name`; pi-style
  `agent.session_flags` in `pi-batch.yaml` (start/continue), overridable per agent CLI.
- **Progressive message memory**: pi calls receive stable, named session ids and keep their
  original JSONL messages unchanged. A redacted append-only metadata index records task,
  validator, Gate, Campaign, artifact, and archive outcomes. The prompt receives only a
  bounded Memory Manifest; the current LLM decides whether to use `pi-batch.py memory
  recent|find|read`. Preliminary questions do not load history. `--memory-mode off` disables
  the feature; `auto` (default) limits it to configured compatible agents. `memory ingest`
  incrementally indexes existing pi sessions without copying or changing their messages;
  it records bounded provider/CLI failure metadata as `FAILED_OBSERVED` for future planning;
  manifest/recent/read lookup reads backward from the index tail, while `find` streams the
  index and retains only Top-K matches. Metadata lines are bounded independently from raw
  Pi messages. Session usage metering caches each raw JSONL inode/offset and folds only
  appended bounded records, so task N does not rescan the full history from task 1.
  Execute prompts select domain-matching metadata from only a fixed recent window
  (`memory.manifest_scan`); preliminary prompts remain history-free. Prompt/task/template
  files are bounded by `input.max_bytes` (2 MiB by default) and oversized or invalid-UTF-8
  sources fail before an agent starts. Structured Campaign analysis opts into the same
  bounded execute hint, allowing future direction planning to learn from prior metadata;
  recent failure-class metadata also invalidates Campaign analysis reuse so new Gate or
  validator failures can trigger a fresh plan.
  Every metadata event is additionally scoped by tenant, a one-way project identity digest,
  and sensitivity (`public|internal|confidential|secret|regulated`); mismatched or legacy
  unscoped records are not returned by memory queries. Configure `memory.tenant` /
  `memory.sensitivity` or use `PBATCH_MEMORY_TENANT` /
  `PBATCH_MEMORY_SENSITIVITY` for isolated workers.
- **Validation gates**: `--validate NAME[,NAME...]` (registry in `pi-batch.yaml` `validators:`)
  or raw `--validate-cmd`; `{output}`/`{cwd}` placeholders; AND semantics; a failing gate
  deletes the temp artifact and marks the task failed (retried/regenerated); per-task
  `validate` and per-stage `validate_cmd` override/disable the CLI gate. Validator stdout
  and stderr are bounded by `validation.output_max_bytes` and fail closed on overflow.
- **Pipelines**: chained stages via `from_prompt` (one-sentence starting point, no input file
  needed), `from_dir`, or `from_outputs` (one stage name or a list for a DAG evidence join);
  template placeholders `{input_content}` /
  `{input_stem}` / `{input_path}`; `aggregate: true` merges all upstream outputs into one
  combined prompt per template (`input_stem` = `combined`). Combined/meta evidence shares a
  configurable total content budget (`evidence.max_bytes`, default 64KiB) and source-count
  budget (`evidence.max_sources`, default 64); full artifacts remain on disk and truncation
  markers identify their paths. Post-stage shell commands
  (`commands_parallel`) act as a **stage failure gate**; they run in isolated process groups
  with configurable deadlines and bounded diagnostics (`commands.*` globally or
  `command_timeout` / `command_output_max_bytes` per stage). Per-stage/per-pipeline
  `git_commit` is supported; failed stages are reported and stop downstream stages.
- **Prompt process boundary**: the final `-p` value is capped by `prompt.max_bytes`
  (120KiB default), below Linux's usual 128KiB single-argument ceiling. Oversized prompts
  fail predictably before process spawn; move large context to artifacts for on-demand reads.
- **Meta stages** (`meta: true`): dynamic role orchestration — the agent picks which roles
  still add value (either templates from `role_dir` or **ad-hoc roles** defined inline as
  `{"role": ..., "task": ...}` objects), the chosen roles run **concurrently** (each in its
  own agent session) against the aggregated evidence, their deliverables fold back in, and
  the loop repeats until the orchestrator says done or `max_iterations` is reached. Role
  names are sanitized and non-template names (incl. path traversal) are refused. Optional
  `role_keywords`, `relevance_min_score`, and `max_roles_per_iteration` bound fan-out with
  keyword evidence and a runtime-enforced cap. On `--reuse`, the orchestrator still
  replans from current evidence, while unchanged selected role artifacts are freshness-
  validated (and optionally fingerprint-matched) instead of invoking the role again.
- **Repository campaigns** (`pi-batch.py campaign` / `scripts/full-auto.sh`): module
  discovery, parallel JSON analysis, direction scoring and evidence validation, full-SDLC
  implementation, JSONL state, fingerprint resume, derived summaries, history-based cost
  estimates, and Git-worktree isolation when pipelines run concurrently. Each isolated
  direction has a configurable wall-clock deadline (`implementation.timeout`, 14400s default)
  that kills its process group on expiry. State lookup caches only the latest event per
  direction and streams bounded JSONL records for long-running use.
- **Dirty-baseline collaboration** (`pi-batch.py collab`): an explicit module plan runs
  protected `isolate` children concurrently. `--seed-dirty` copies only selected current
  changes into an external worktree, `--write-scope` rejects out-of-scope writes, and
  `--patch-file` leaves review artifacts outside the source tree. The source worktree is
  checked for drift; commits/merges are forbidden and failed worktrees are retained.
- **Gate stages** (`gate: true`): prefer the structured contract in
  `docs/GATE_CONTRACT.md` (`schema_version`, verdict, reason, evidence and
  validator results). Structured PASS requires evidence and validators; malformed
  structured JSON fails closed. Existing `VERDICT: PASS - <reason>` artifacts
  remain compatible, while bare or missing verdicts still halt the pipeline.
- **Judge validators** (H line): a validator entry may be a mapping
  `{cmd, judge: true, scope: "file|repo"}`. `judge: true` runs the LLM-as-judge protocol —
  the gate must reply `VERDICT: PASS|FAIL - <reason>` (bare or missing verdicts fail
  closed, FAIL/REJECT rejects the artifact); `scripts/judge.py` wraps an agent call for
  this. `scope: repo` defers full-tree gates (`go build ./...`, `quality.py`, `uicheck`)
  to the end of the stage/batch so they never race concurrent role agents mid-write.
- **Per-agent failure signatures** (G line): `agent.error_patterns` overrides the built-in
  provider/network/CLI signature table for all agents, `agent.agents.<bin>.error_patterns`
  for one binary (claude/codex/... have different failure replies); `agent.agents.<bin>.session_flags`
  likewise overrides session continuation flags per binary. Set
  `agent.agents.<bin>.output_protocol: codex-jsonl` for Codex `--json` adapters: malformed or
  incomplete transcripts fail closed, and raw JSONL never becomes a task artifact.
- **Orphan detection** (`--kill-orphans`): start markers now carry the agent pid; a residue
  scan after a SIGKILLed runner warns when the orphaned agent is still running (double-
  billing risk) and can kill its process group.
- **Provider rate limiting** (F line): `--rate-limit N` / `--rate-burst N` (or `limits:` in
  pi-batch.yaml, with per-provider overrides) run a process-wide token bucket that queues
  agent spawns across parallel workers and meta role fan-out — `--min-interval` only
  throttled successful serial tasks. Retry waits are jittered (±50% normal, 30–60s spread
  for rate/network failures) so parallel failures do not re-sync into a thundering herd.
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
pbatch/                  engine + bounded evidence/memory + campaign package
pi-batch.yaml            agent.bin, session_flags, validators registry, commit prefix
AGENTS.md                agent-facing guide to the whole toolkit (Chinese; local adapt note)
quality.py               upstream-synced org scanner (func lines, complexity, dup bodies)
prompts/                 19 reference role-prompt templates (architect, tech-lead, qa-lead, ...)
pipelines/               example pipeline configs (code-impl, full-sdlc, generic example)
ai/                      AI-SDLC review framework: run-review.py + sdlc.yaml + 10 stage
                         prompts + shared prompt fragments (prompts-shared/)
examples/                pipelines incl. repository-campaign.yaml and its full-SDLC template
scripts/                 upstream one-command flows: rolling-analysis.sh, full-flow.sh,
                         full-auto.sh (module-by-module SDLC automation), paths adapted
docs/                    usage guides incl. RUNNING_247.md (24x7 operation)
tests/                   1200+ regression tests: engine, rules/assess, memory, logging, campaign, run-review, CLI E2E
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
python -m pytest tests/ -q    # 1200+ tests: config resolution, failure detection,
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
- `make self-test` verifies the harness itself (50 unittest cases); `make test`
  runs the 1200+ case regression suite + `make eval` (42 rule-system cases); `make coverage` reports check coverage
- Git hooks in `.githooks/` (enable: `git config core.hooksPath .githooks`):
  pre-commit gates staged files, pre-push runs the full harness
- CI: `.github/workflows/ci.yml` runs G1-G4 + self-test + regression + eval +
  quality + org + coverage

See `docs/CHECKS_REGISTRY.md` for the full registry.

## Known open issues

- LICENSE added (MIT) — but the upstream snaplink/ai-dev license was not publicly
  reachable when it was chosen; if that license is later published and is more
  restrictive than MIT, re-check compatibility before further distribution (see
  the note in `LICENSE`).

Resolved by self-iteration round 1 (see Local fixes above): `output_path()` ignoring
`task.cwd`, git-commit exit codes unchecked, PyYAML silent degradation, the stale
"timeout can double" README item (already fixed upstream via `_stream_proc`'s shared
deadline), missing `pyproject.toml`, and the unguarded `git add -A`.

## UI 几何构图与业务 UI 契约（内化功能）

Agent 生成前端 UI 时遵循"先构图、后写码"：

- **`ui-specs/geometry.md`**：隐形对齐轴、线条五语义（留白>分隔>边框）、
  圆角 {4,8,12}（tokens.json radius 派生）/ 线宽 {1,2,3} / 控件 {32,40,48}
  家族、光学修正显式化（`--optical-*` 变量）
- **`ui-specs/business-ui-contract.md`**：角色×任务×数据×交互×视觉五层映射、
  状态矩阵（加载/空/部分/陈旧/并发/无权限/恢复）、错误四阶、数据可信度
- **`pi-batch ui-geometry <file|--dir>`**：机械门禁子命令——裸数字小偏移、
  随机圆角、随机线宽 fail closed（`uigeometry` 验证器；确定性回归见
  `tests/test_ui_geometry.py`）
- 流水线挂载：frontend-implementation / ui-generation 的 implement/生成阶段
  自动带 uigeometry 门禁（ui-refactor 走 uicheck 项目级扫描）
