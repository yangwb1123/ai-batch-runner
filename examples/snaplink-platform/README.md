# Snaplink 平台维护包（ai-batch-runner 全特性使用）

6 个仓库构成一个平台：snaplink（认证授权 IdP）、snaplink-console（管理控制面）、
snaplink-audit-governance（审计事实源）、aero-id（账户域）、aero-vault（文件平台）、
aero-im（消息/通知）。

- **场景分析与建议**：`../../docs/snaplink-platform/SCENARIO_ANALYSIS.md`
- **全特性运营方案**：`../../docs/snaplink-platform/OPERATIONS_PLAN.md`

## 配置清单

| 文件 | 用途 | 在哪里运行 |
|---|---|---|
| `audit-onboarding-pipeline.yaml` | 跨仓库审计接入契约设计（事件注册表 → scope 矩阵 → 接入设计 → 对抗审查 → VERDICT 门），只产出提案文档 | 主仓库 |
| `campaign-audit-governance.yaml` | audit-governance 生产化 campaign（模块发现 → 方向评分 → worktree 隔离实现） | 复制到 audit-governance 仓库 |
| `platform-247.sh` | 6 仓库 7×24 轮巡：滚动分析 + 失败补跑 + 决策日志 + 归档 | 主仓库 |

## 快速开始

```bash
# 1. 环境自检（本仓库门禁必须全绿）
python pi-batch.py check

# 2. 需求评估（零 LLM 成本，先评后做）
python pi-batch.py assess "平台统一审计：6 服务管理操作经 Outbox 进 audit-governance，控制台统一查询，生产"

# 3. 跨仓库审计契约设计（LLM 流水线，先 dry-run）
python pi-batch.py examples/snaplink-platform/audit-onboarding-pipeline.yaml --dry-run
python pi-batch.py examples/snaplink-platform/audit-onboarding-pipeline.yaml --retries 3

# 4. 每仓库同步 ai-batch-runner 后跑 campaign（先 dry-run）
cp -r pbatch pi-batch.py pi-batch.yaml <目标仓库>/
cd <目标仓库> && python pi-batch.py campaign --dry-run --modules "internal,cmd,migrations"
cd <目标仓库> && python pi-batch.py campaign --modules "internal,cmd" --jobs 4 --max-directions 2

# 5. 7×24 轮巡
bash examples/snaplink-platform/platform-247.sh 3 300
```

## 平台契约证据纪律

- 所有流水线产物是**提案**：事件类型清单、scope 矩阵须人工按
  Verified/Partial/Missing/Proposed 核对后提升为仓库契约文档
- 每个仓库自己的 `make ci` / `cli.py check` / AGENTS.md 硬门禁不被 AI 建议豁免
- 并行实现（worktree）不自动合并回主分支，按正常评审/合并流程处理
