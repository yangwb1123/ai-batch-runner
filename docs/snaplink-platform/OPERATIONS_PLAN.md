# ai-batch-runner 全特性运营方案（Snaplink 平台）

> 6 仓库 = 一个平台。每个仓库自带 ai-batch-runner（薄壳 + pbatch/ + pi-batch.yaml，
> 可移植性设计），平台级编排由主仓库（本仓库）的配置驱动。
> 前提：先把 aero-id / aero-vault 的内嵌旧版同步到本仓库当前版本。

## 1. 特性 → 平台用法映射（全部特性逐一对应）

| 特性 | 在此平台的具体用法 | 何时用 |
|---|---|---|
| `classify`（任务类型判定） | `pi-batch.py classify` 双语关键词判定：snaplink/aero-*/audit 的 Go/Rust 后端任务自动路由 `backend-implementation-pipeline`（plan 含强制持久化设计报告）；console 的 Flutter 任务路由前端流水线（plan→implement→对抗→gate） | 每个新需求/缺陷修复入口 |
| `assess`（需求评估） | 新需求先过 8 维完整性 + 规模信号 + 处方克制；平台需求自动 L2/L3，触发 product_thinker 推演与 commercial-readiness 检查 | 需求进入 backlog 时 |
| `rules`（规则匹配） | console 前端任务：demo/standard/production × page_type × risk 三轴选规范，必选规则不可跳过；`--llm-json` 双向校验（算法 vs LLM 对比裁决） | 任何 UI 生成/重构前 |
| `advance`（自推进） | 每仓库独立推进：P0 审计生产化（audit-governance）→ P1 控制面聚合（console）→ P2 编排/告警；状态记录 `docs/advance/state.jsonl` 逐轮可续 | 每周固定推进 |
| `campaign`（项目级发现） | 每仓库跑 `campaign --modules "domains,internal,interfaces" --jobs 4 --max-directions 2`：模块扫描 → 结构化方向评分 → worktree 隔离并行实现（`--parallel-pipelines N` 时要求工作区干净，独立 branch/commit 证据，不自动合并） | 新迭代开始的探索轮 |
| meta 动态角色编排 | `adversarial_review` 阶段：security_engineer 挑战审计模型/哈希链、protocol_expert 验 OAuth/client_credentials 流、qa_lead 找边界缺陷；并发独立会话 + 证据折叠 + 运行时角色上限 | 每个设计/实现评审 |
| `gate` 裁决门 | 设计 gate + 验收 gate：`VERDICT: PASS|FAIL - <reason>`，无理由/无 VERDICT 一律 FAIL（fail closed）阻断后续阶段 | 关键阶段目标确认 |
| 验证器注册表 | `validators`：`backendquality`（过度设计/依赖方向/浮点金额/N+1）、`pyquality`（代码组织）、`completion`（诚实完成报告）、`build`（go build+vet）、`gofmt`；audit 接入检查可注册 `auditevents` 自定义门禁 | 每阶段产物落盘前 |
| 失败签名拒绝 | 额度/速率/断网/认证失败签名 → 零产物落盘，自动重试（指数退避，网络类至少 30s） | 全天候 |
| `--reuse` / `--reuse-fingerprint` | 断点续跑：产物先复验 validator 再复用；fingerprint 变化（模块/快照/prompt/方向/pipeline/模型）则重新生成 | 7×24 续跑 |
| `--max-rounds 0` 轮循环 | 无人值守直到全通过；`--round-delay` 轮间休息、`--min-interval` 节流、circuit 熔断毒任务、env-signature 停环 | 夜间/周末批量 |
| 会话模式 + memory | `--session-mode shared` 稳定会话延续上下文（同方向多轮）；`memory ingest/recent/find/read` 跨天检索历史裁决，有界注入 + 秘密脱敏 | 长期维护 |
| `context`（上下文路由） | 每仓库 `docs/agent-context/` 放平台契约（事件类型清单、scope 矩阵、ADR 指针），按路径 glob + 关键词自动加载；缺失文档显式警告 | 所有 agent 任务 |
| `learn`（自演进） | 事故/评审 FAIL → RCA → Rule Schema 草案（docs/rules/drafts/）→ 人工批准并入注册表；例如"写操作无审计事件 = 评审 FAIL"沉淀为规则 | 每个 FAILED gate 复盘 |
| `eval` 回归 | 27 个断言用例守护 classify/rules/assess/product/workflow 行为；改关键词/注册表后 `make ci` 自动跑 | 任何规则改动后 |
| `decision_log` | 平台级 `docs/DECISIONS.md` 追加式记录每阶段决策（含被否方案与 gate 裁决），滚动分析每轮一条 | 全程 |
| `git_commit` + `archive_dir` | 每阶段提交产物；全成功后才归档（gate FAIL 保留现场） | 全程 |
| 硬超时/进程组 | 每任务默认 900s 绝对截止 SIGKILL 零孤儿；pipeline 总截止（campaign implementation.timeout 14400s） | 全程 |
| 多仓库 worktree 隔离 | `--parallel-pipelines N`：每方向独立 worktree/branch/日志，避免并行实现互相踩 | 并行实现多方向时 |
| `--dry-run` | campaign/流水线先预览计划与耗时估计，不创建目录/日志/状态 | 每次批量前 |

## 2. 平台级流水线（跨仓库契约类工作）

仓库内用各自 `pi-batch.py`；**跨仓库契约工作**（事件类型清单、scope 矩阵、统一登录）
在主仓库用 `examples/snaplink-platform/audit-onboarding-pipeline.yaml` 类流水线：
一句话起点 → 设计 → meta 对抗（多角色并发）→ 设计 gate → 产出平台契约文档
（作为提案，按证据标准提升），再由各仓库 campaign 落地。

```bash
# 跨仓库审计接入契约设计（在主仓库运行）
python pi-batch.py examples/snaplink-platform/audit-onboarding-pipeline.yaml \
  --retries 3 --log-file logs/platform-audit.log --decision-log docs/DECISIONS.md

# 审计治理平台生产化（在 audit-governance 仓库运行，先 dry-run）
python pi-batch.py campaign --dry-run --modules "internal,cmd,migrations,deploy"
python pi-batch.py campaign --modules "internal,cmd" --jobs 4 --max-directions 2 \
  --reuse --retry-failed --parallel-pipelines 2 --log-file logs/campaign.log
```

## 3. 每周运营节奏（7×24 无人值守 + 每周人工裁决）

| 频次 | 动作 | 命令（示意） |
|---|---|---|
| 每日 | 每仓库滚动分析一轮 | `python pi-batch.py -p "基于当前代码库分析扩展方向" --output docs/requirements/runs/run-$(date +%Y%m%d-%H%M%S).md --session-mode shared --session-name ext-rolling --min-interval 60 --retries 3 --log-file logs/ext.log --decision-log docs/DECISIONS.md --git-commit --archive-dir docs/archive` |
| 夜间 | 失败方向重试 + 熔断 | `python pi-batch.py tasks.yaml --mode serial --reuse --retries 3 --max-rounds 0 --round-delay 300 --min-interval 5 --log-file logs/run.log` |
| 每周 | campaign 探索轮 + advance 推进 | 见上节；`python pi-batch.py advance` 在每仓库跑 |
| 每周 | 人工裁决：gate FAIL 复盘 → learn 沉淀规则；approved 规则并入注册表 | `python pi-batch.py learn --rca docs/incidents/xxx.md` |
| 每周 | 规则系统回归 | `make ci`（含 eval 27 用例） |
| 每迭代 | 同步 ai-batch-runner 到 6 仓库 | 复制薄壳 + pbatch/ + pi-batch.yaml |

## 4. 无人值守脚本

`examples/snaplink-platform/platform-247.sh [轮数] [间隔秒]`：遍历 6 仓库，
按顺序跑（a）滚动分析 1 轮、（b）重试失败任务（--reuse --retry-failed）、
（c）清理归档；每仓库独立日志，汇总到 `logs/platform-247-summary.log`。
配合 systemd timer 或 nohup 常驻。

## 5. 安全边界（必须遵守）

- 所有 agent 产出是**提案**：按证据标准（Verified/Partial/Missing/Proposed）核对后提升；
  审计事件类型清单、scope 矩阵进仓库契约文档需人工批准
- 每个仓库的硬门禁（make ci / cli.py check / AGENTS.md）不因 AI 建议豁免
- 并行实现不自动合并回主分支：gate 通过后按仓库正常评审/合并流程
- 生产凭据（S3/KMS/SMTP/OIDC/OTLP）未验收项不得外推为已验收（aero-im 现有标注纪律）
