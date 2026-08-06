# Snaplink 组合维护包（ai-batch-runner 全特性使用）

6 个**独立产品**构成可组合套件：snaplink（认证授权 IdP）、snaplink-console
（管理端）、snaplink-audit-governance（审计事实源）、aero-id（账户域）、
aero-vault（文件平台）、aero-im（消息/通知）。组合式架构与定位模型见
`COMBINATION_MODEL.md`。

- **场景分析与建议**：`../../docs/snaplink-platform/SCENARIO_ANALYSIS.md`
- **组合式架构建议**：`../../docs/snaplink-platform/COMBINATION_MODEL.md`
- **多项目联动模型**：`../../docs/snaplink-platform/MULTI_REPO_OPERATIONS.md`
- **全特性运营方案**：`../../docs/snaplink-platform/OPERATIONS_PLAN.md`
- **组合仓库模板**：`suite-template/`（snaplink-suite 骨架，含 suite-check 门禁）

## 配置清单

| 文件 | 用途 | 在哪里运行 |
|---|---|---|
| `audit-onboarding-pipeline.yaml` | 跨仓库审计接入契约设计（事件注册表 → scope 矩阵 → 接入设计 → 对抗审查 → VERDICT 门），产出**提案**（人工批准后进 suite 契约） | 主仓库（产提案） |
| `campaign-audit-governance.yaml` | audit-governance 生产化 campaign（模块发现 → 方向评分 → worktree 隔离实现） | 复制到 audit-governance 仓库 |
| `platform-247.sh` | 6 仓库 7×24 轮巡：滚动分析 + 失败补跑 + 决策日志 + 归档 | 主仓库 |
| `platform-status.sh` | 6 仓库状态看板（commit/脏文件/campaign 产物/决策历史） | 主仓库 |
| `suite-template/` | snaplink-suite 组合仓库骨架：compose 多拓扑 + compatibility.yaml + suite-check.py 门禁 + E2E 清单 | 复制为 ~/workspace/demo/snaplink-suite |

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

# 5. 每仓库同步 ai-batch-runner 后跑 campaign（先 dry-run）
cp -r pbatch pi-batch.py pi-batch.yaml <目标仓库>/
cd <目标仓库> && python pi-batch.py campaign --dry-run --modules "internal,cmd,migrations"

# 6. 7×24 轮巡 + 看板
bash examples/snaplink-platform/platform-status.sh
bash examples/snaplink-platform/platform-247.sh 3 300
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
