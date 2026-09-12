# 可用示例：给 snaplink 提功能需求提案

一个完整跑通的小场景：让 agent 分析项目的 OIDC logout 子系统、提出 3 个
新增功能点，再把最优提案细化成 feature spec——覆盖 ai-dev 的核心机制：
任务批次、共享会话、错误重试、断点续跑、工程质量门禁与评审闭环。

## 0. 前置

```bash
uv sync                      # 安装 PyYAML（pyproject.toml 已管理）
python pi-batch.py --help   # 确认 runner 可用
```

## 1. 基本运行：两个任务 + 共享会话

```bash
python pi-batch.py examples/snaplink-proposals.yaml \
  --mode serial \
  --session-mode shared --session-name logout-proposals \
  --log-file logs/proposals.log
```

- 任务 1 分析项目并提 3 个功能点 → `docs/proposals/oidc-logout-3-features.md`
- 任务 2 细化 feature spec → `docs/proposals/oidc-logout-feature-spec.md`
- `--session-mode shared`：两个任务在**同一个会话**中执行，任务 2 能看到
  任务 1 的分析上下文（会话 id 可重现：`--session-name logout-proposals`）
- 两个任务 `validate: ""`：纯分析产出，显式跳过工程质量门禁
- `--log-file` 落盘日志，`tail -f logs/proposals.log` 可监控

## 2. 额度 / 速率 / 断网：自动重试

agent 返回 `rate_limit_error`、`insufficient_quota`、`connection refused`
等错误回复时（即使退出码为 0），输出**不会落盘**，并自动重试：

```bash
python pi-batch.py examples/snaplink-proposals.yaml \
  --mode serial \
  --retries 3 --retry-delay 30 --retry-backoff 2
```

速率/网络类错误每次重试至少等 30 秒（给限流窗口恢复），重试仍在同一会话内。
日志示例：

```
[WARNING] agent output REJECTED: agent reported provider failure (rate_?limit_?error)
[WARNING] RETRY 1/3 for task [1/2] in 30s (reason: ...)
[INFO] WROTE docs/proposals/oidc-logout-3-features.md
```

## 3. 中断 / 崩溃：断点续跑

任何时刻中断（Ctrl-C、断网、机器重启），已完成的任务输出都在磁盘上；
重新运行加 `--reuse` 只重跑失败的任务：

```bash
python pi-batch.py examples/snaplink-proposals.yaml \
  --mode serial --reuse --session-mode shared --session-name logout-proposals
```

```
[INFO] Reuse: 1 task(s) already have outputs, skipped
[INFO] WROTE docs/proposals/oidc-logout-feature-spec.md
```

`--max-rounds 0 --round-delay 300` 可让 runner 循环重试直到全部通过
（7×24 无人值守，见 `docs/RUNNING_247.md`）。

## 4. 工程质量验证（按需）

示例任务不生成代码，所以跳过了门禁。若任务是生成 Go 代码，用注册表
验证器（`pi-batch.yaml` 的 `validators`，与 `cli.py` 门禁体系一致）：

```bash
python pi-batch.py code-tasks.yaml --validate quick,gofmt
```

生成结果先写临时文件，`python cli.py check`（filesize+vet）与 gofmt 全部
通过才原子落盘；失败则删除临时文件并触发重试。

## 5. 评审闭环：用 run-review 评审提案

```yaml
# docs/proposals/review-ctx.yaml
project: Snaplink SSO
subsystem: OIDC RP-Initiated Logout proposals
files:
  - docs/proposals/oidc-logout-3-features.md
  - docs/proposals/oidc-logout-feature-spec.md
```

```bash
python ai/run-review.py --all \
  --context docs/proposals/review-ctx.yaml \
  --session-mode shared --session-name logout-review
```

10 个评审阶段共享一个会话依次执行；`--resume` 可在中断后续跑，失败的
阶段自动跳过接力（评审输出同样先过校验：额度/断网错误回复不落盘）。

## 预期输出

```
docs/proposals/
├── oidc-logout-3-features.md      # 3 个功能点（问题/证据/行为/验收）
└── oidc-logout-feature-spec.md    # 最优提案的 bounded spec
logs/proposals.log                 # 运行日志
```

把 AI 产出当**提案**而非结论：按 AGENTS.md 的证据标准核对后，将验证过的
结论提升到 `docs/feature-matrix.md` / `docs/deferred-backlog.md`，或按
`docs/templates/feature-spec.md` 走正式特性流程。

## 6. 项目级全自动 Campaign

`repository-campaign.yaml` 声明模块发现、分析并发、方向评分、状态路径和
实现流水线；`repository-campaign-pipeline.yaml` 是通用的需求→设计→相关性
角色对抗→设计 gate→实现→验收 gate 模板：

```bash
python pi-batch.py campaign --dry-run
python pi-batch.py campaign --max-directions 2 --reuse --retry-failed
```

分析必须返回 JSON，并为每个方向给出现有文件/符号证据和可测试验收标准。
恢复依据是用户级 runtime 中的 `state.jsonl` 与 fingerprint（默认
`~/.codex/pbatch/projects/<workspace-id>/`），不是 Markdown 文件是否非空；显式
`state_file` 时仍按配置路径执行。
需要并行实现时使用 `--parallel-pipelines N`；runner 会先要求主工作区干净，
再为每个方向建立独立 Git worktree 和分支。

## 7. 流水线目录（examples/*.yaml）

| 流水线 | 用途 | 入口 |
|---|---|---|
| `frontend-implementation-pipeline.yaml` | 前端真实实现：assess → plan（布局/组件树/状态/交互链）→ implement 改项目代码（uiquality+uicheck+completion 三门禁）→ 对抗审查 → VERDICT | `--classify`（前端任务自动路由） |
| `backend-implementation-pipeline.yaml` | 后端实现：assess → plan（含**强制 Persistence Design 报告**）→ implement（backendquality+completion 双门禁）→ 对抗审查 → VERDICT | `--classify`（后端任务自动路由） |
| `ui-generation-pipeline.yaml` | 产物型页面生成模板（输出到 docs/ui/generated/） | 显式 `--pipeline` |
| `ui-refactor-pipeline.yaml` | 已有项目前端代码按 ui-specs 重构到 0 违规（uicheck 门禁反馈循环） | 显式 |
| `frontend-engineering-review.yaml` | 前端工程审计：异步/竞态/防抖/上帝文件/复杂度 | 显式 |
| `backend-engineering-review.yaml` | 后端架构/模式/领域/算法对抗审计 + backendquality | 显式 |
| `full-sdlc-implement.yaml` | 全流程真实实现（需求→设计→对抗→gate→实现→验收） | L2/L3 工作流 |
| `meta-review-pipeline.yaml` | 通用分析流水线（一句话起点 + meta 动态角色） | 显式 |
| `quickstart-full-sdlc.yaml` | 完整闭环示例（一句话→提案→归档） | 显式 |
| `repository-campaign*.yaml` | 项目级自动发现→分析→Top N 实现（implement 阶段自动加载域规则处方） | `campaign` |
| `structured-output-pipeline.yaml` | 纯 JSON 产物 + 声明式 JSON Schema 原子落盘门禁 | 显式 |

配套评估/上下文/规则命令：`assess`（需求评估：处方+产品化+工作流分级；
`--product-brief` 输出产品方向提案）、
`rules`（规则匹配+双向校验）、`context`（项目文档路由）、`eval`（规则
系统回归套件）、`learn`（RCA→规则草案）、`classify`（任务类型判断）。
