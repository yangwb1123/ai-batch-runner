# Snaplink 组合维护包（ai-batch-runner 全特性使用）

基础六个**独立平台产品**构成可组合套件：snaplink（认证授权 IdP）、snaplink-console
（管理端）、snaplink-audit-governance（审计事实源）、aero-id（账户域）、
aero-vault（文件平台）、aero-im（消息/通知）；`sverpweb-stable` 是本套件之外的 ERP
首个业务实现项目，`iris-ui` 是 UI 基础设施，不改变领域数据归属。组合式架构与定位模型见
`COMBINATION_MODEL.md`。

- **场景分析与建议**：`../../docs/snaplink-platform/SCENARIO_ANALYSIS.md`（六件套）
  / `../../docs/snaplink-platform/ERP_SCENARIO_ANALYSIS.md`（ERP 接入场景，五轮对抗后终裁 PASS）
- **组合式架构建议**：`../../docs/snaplink-platform/COMBINATION_MODEL.md`
- **总体架构（Workflow/资产/审计/Agent/插件/弹性）**：`../../docs/snaplink-platform/PLATFORM_ARCHITECTURE.md`
- **跨项目契约草案**：`../../docs/snaplink-platform/contracts/`
- **多项目联动模型**：`../../docs/snaplink-platform/MULTI_REPO_OPERATIONS.md`
- **全特性运营方案**：`../../docs/snaplink-platform/OPERATIONS_PLAN.md`
- **组合仓库模板**：`suite-template/`（snaplink-suite 骨架，含 suite-check 门禁）
- **Agent 产品设计 Harness**：`../../docs/snaplink-platform/AGENT_HARNESS.md`（需求→决策摘要→决策门→分仓实施的强制流程）

## 配置清单

| 文件 | 用途 | 在哪里运行 |
|---|---|---|
| `audit-onboarding-pipeline.yaml` | 跨仓库审计接入契约设计（事件注册表 → scope 矩阵 → 接入设计 → 对抗审查 → VERDICT 门），产出**提案**（人工批准后进 suite 契约） | 主仓库（产提案） |
| `campaign-audit-governance.yaml` | audit-governance 生产化 campaign（模块发现 → 方向评分 → worktree 隔离实现） | 复制到 audit-governance 仓库 |
| `platform-247.sh` | 6 仓库 7×24 轮巡：滚动分析 + 失败补跑 + 决策日志 + 归档 | 主仓库 |
| `platform-status.sh` | 6 仓库状态看板（commit/脏文件/campaign 产物/决策历史） | 主仓库 |
| `suite-template/` | snaplink-suite 组合仓库骨架：compose 多拓扑 + compatibility.yaml + suite-check.py 门禁 + E2E 清单 | 复制为 ~/workspace/demo/snaplink-suite |
| `platform-map.yaml` | 机器可读项目地图：能力目录 / 数据所有权 / 依赖规则 / 审计分级 / 集成端口（含 ERP 场景 sverpweb-stable / iris-ui 条目） | 主仓库（Agent 能力发现依据） |
| `erp-scenario-pipeline.yaml` | ERP 场景流水线：证据→分析→meta 对抗审查→修订（doccomplete 门禁防空壳）→复审→终裁门 | 主仓库（产提案） |
| `scripts/check-doc-complete.py` | 交付物完整性门禁（防"自指空壳"：行数/关键节/自引用三重检查） | 主仓库（revision 阶段 validate_cmd） |
| `requirement-pipeline.yaml` | 组合需求强制决策流水线：目标→场景→能力→组合→变更→决策摘要→决策门→批准 | 主仓库 |
| `platform-architecture-pipeline.yaml` | 总体架构推进：Workflow、审批、资产、审计、Telemetry、Agent、Profile/Plugin 契约与首切片变更设计 | 主仓库 |
| `platform-architecture-gate-pipeline.yaml` | 有界证据独立 Gate；避免 adversarial review 聚合超过 prompt 限制 | 主仓库 |
| `platform-readiness-pipeline.yaml` | 根据 Gate blocker 生成持久化、Workflow 状态、审计 Outbox、运行与验收工件 | 主仓库 |
| `platform-readiness-gate-pipeline.yaml` | 独立设计→代码交接 Gate；PASS 不代表生产验收 | 主仓库 |
| `platform-implementation-pipeline.yaml` | 在独立 sverpweb worktree 实现首个 approval-only 切片 | 主仓库调度、目标仓库执行 |
| `platform-repair-pipeline.yaml` / `platform-adversarial-review-pipeline.yaml` | 代码修复与对抗复核流水线 | 独立 sverpweb worktree |
| `scripts/check-decision-summary.py` | 决策门机械校验器（9 组必填维度，fail closed） | 主仓库（决策门 validate_cmd） |
| `scripts/check-architecture-contract.py` | Phase 3 架构契约、Manifest、项目边界和 Workflow/Agent/Audit 不变量门禁 | 主仓库（默认 fail closed；`--mechanical-only` 仅用于批准前检查） |
| `scripts/check-change-manifest.py` | Change Manifest 门禁（跨仓库变更边界校验） | 主仓库 |
| `prompts/requirement_interpreter.md` 等 | 组合角色模板（requirement_interpreter / capability_architect / composition_reviewer）+ role_keywords | 主仓库 meta 编排 |

## 快速开始

```bash
# 1. 环境自检（本仓库门禁必须全绿）
python pi-batch.py check

# 2. 需求评估（零 LLM 成本，先评后做）
python pi-batch.py assess "平台统一审计：6 服务管理操作经 Outbox 进 audit-governance，控制台统一查询，生产"

# 3. 初始化组合仓库（模板门禁自检）
cp -r examples/snaplink-platform/suite-template/. ~/workspace/demo/snaplink-suite/
cd ~/workspace/demo/snaplink-suite && python scripts/suite-check.py

# 4. 跨仓库审计契约提案（LLM 流水线，先 dry-run）
python pi-batch.py examples/snaplink-platform/audit-onboarding-pipeline.yaml --dry-run

# 4. 组合需求（强制决策程序，产出提案）
python pi-batch.py examples/snaplink-platform/requirement-pipeline.yaml \
  -p "管理员删除文件后通知所有者，并记录审计" --dry-run

# 5. 总体架构/通用 Workflow 首切片（先 dry-run，再运行）
python pi-batch.py examples/snaplink-platform/platform-architecture-pipeline.yaml \
  --agent-bin ./examples/snaplink-platform/scripts/pi-read-only.sh --dry-run
python pi-batch.py examples/snaplink-platform/platform-architecture-pipeline.yaml \
  --agent-bin ./examples/snaplink-platform/scripts/pi-read-only.sh \
  --retries 3 --log-file logs/platform-architecture.log
python examples/snaplink-platform/scripts/check-architecture-contract.py . \
  --mechanical-only --json

# 6. 设计交接 Gate 与 sverpweb 首切片（独立 worktree，不自动合并）
python pi-batch.py examples/snaplink-platform/platform-readiness-gate-pipeline.yaml \
  --agent-bin ./examples/snaplink-platform/scripts/pi-read-only.sh \
  --model gpt-5.6-luna --thinking max
python pi-batch.py examples/snaplink-platform/platform-implementation-pipeline.yaml \
  --model gpt-5.6-luna --thinking max --no-lock

# 7. 人工批准后：Change Manifest 门禁
python examples/snaplink-platform/scripts/check-change-manifest.py \
  docs/snaplink-platform/decisions/change-manifest.yaml

# 7. 每仓库同步 ai-batch-runner 后跑 campaign（先 dry-run）
cp -r pbatch pi-batch.py pi-batch.yaml <目标仓库>/
cd <目标仓库> && python pi-batch.py campaign --dry-run --modules "internal,cmd,migrations"

# 8. 7×24 轮巡 + 看板
bash examples/snaplink-platform/platform-status.sh
bash examples/snaplink-platform/platform-247.sh 3 300

# 9. ERP 场景：sverpweb-stable 接入建议（五轮对抗流水线，已终裁 PASS）
#    完整证据链 docs/snaplink-platform/erp/；决策日志 decisions/erp-scenario.md
python pi-batch.py examples/snaplink-platform/erp-scenario-pipeline.yaml --dry-run
#    sverpweb-stable 的 campaign 配置/流水线模板已在仓库内（pi-batch-campaign.yaml）
cd ~/workspace/job/sverpweb-stable && python ~/ai-batch-runner/pi-batch.py campaign --dry-run
```

## 组合纪律

- 所有流水线产物是**提案**：事件类型清单、scope 矩阵须人工按
  Verified/Partial/Missing/Proposed 核对后提升为 suite 契约
- **默认只改当前仓库**；跨仓库修改必须确实改变双方契约，且由 suite
  组合验证（compose + E2E + suite-check）把关
- 每个仓库自己的 `make ci` / `cli.py check` / AGENTS.md 硬门禁不被 AI 建议豁免
- 并行实现（worktree）不自动合并回主分支，按正常评审/合并流程处理
- 审计集成 L0/L1/L2：各产品核心只依赖 AuditPublisher 端口，L2 是
  full 组合的部署规则，不是产品硬编码
