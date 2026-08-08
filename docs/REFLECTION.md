# Reflection Engine（Critic Layer / 二阶观察层）

> **定位**：系统已有 check（验证产物），但缺 critique（审视决策链本身）。
> Reflection Engine 在 VERIFY 之后对"整个决策链"做二阶分析，并把发现
> 对接 truth / learn / eval 闭环——它是 `events/capsule/truth/learn/
> eval/rules` 之间"把它们串起来的人"。
>
> 生命周期：`PLAN → EXECUTE → VERIFY → **REFLECT** → LEARN → ARCHIVE`

## 1. 为什么需要（现有能力缺口）

| 能力 | 现状 | 缺口 |
|---|---|---|
| 理解需求 / 复杂度 / 方案 / 执行 / 规则 / 证据 / 学习 / 恢复 | ✅ atomize / profile / pareto / runtime / rules / events / learn / recovery | — |
| **事后复盘**：任务完成 → 重新审视整个过程 → 发现问题 → 改进建议 | ❌ | **Reflection Engine** |
| **反方攻击**：证明方案有问题而非只是通过 | ❌ | adversarial 检查 |
| **二次方案发现**：执行后的新信息 → 方案 C | ❌ | 提示机制 |
| **假设审计**：执行完成后检查当初假设是否成立 | 🟡 profile 有 assumption_records | 需要审计环节 |

## 2. 核心纪律

**Critic 只看证据，不看执行者解释**：输入 = 需求文本 / 代码 / 测试 /
运行结果 / 指标；输出 = 发现与动作。不输入 Executor 的自我解释——
否则产生确认偏差（Executor 说合理 → Critic 说听起来合理）。

本实现是**确定性启发式**（零 LLM 成本、可测试、fail closed），天然满足
该纪律；未来可叠加 LLM Critic 角色（meta 编排），但证据输入契约不变。

## 3. 强度分级（防"大炮打蚊子"）

| 级别 | 模式 | 检查维度 | 耗时 |
|---|---|---|---|
| R0 | `quick` | 目标对齐 / 需求完整性 / 假设审计 | 秒级（默认） |
| R1 | `architecture` `security` | +架构循环 / 复杂度 / 失败路径 / 安全 / 性能 | 分钟级 |
| R2 | `full` | +UX / 未来影响 / 反方验证 | 分钟级+ |

## 4. 12 维度检查（复用已有能力）

| # | 维度 | 检查 | 复用能力 |
|---|---|---|---|
| 1 | Goal Alignment | 需求核心词是否在证据中体现（防解决症状非根因） | `_business_terms` |
| 2 | Requirement Completeness | 缺失的角色/流程/异常/验收维度 | assessor 8 维 |
| 3 | Assumption Audit | 隐含假设是否验证（未验证 → 级联失效风险） | profile.assumption_records |
| 4 | Architecture | 模块级循环依赖 | hypergraph.extract |
| 5 | Complexity | 大函数/复杂度超预算 → over_design | quality.py |
| 6 | Missing Failure Mode | 无失败/重试/降级设计信号 | 关键词 |
| 7 | Security | 敏感域（权限/支付/审计）无安全设计 → critical | 关键词 |
| 8 | Performance | 导出/搜索/大数据无性能设计 | 关键词 |
| 9 | UX | 前端任务无反馈/加载/空态 | classifier |
| 10 | Future Impact | 临时/硬编码信号 = 未来债务 | 关键词+代码扫描 |
| 11 | Adversarial | 证据只有正面 → 反方验证缺口 | 证据分析 |
| 12 | Knowledge Extraction | 发现 → 候选规则（learn 流程） | findings 聚合 |

## 5. 闭环动作（发现进入系统，不是报告即结束）

```
Reflection findings
 ├─ critical  → 先修复再进入下一任务（阻断）
 ├─ wrong_assumption → truth invalidate（级联失效）或显式 accepted
 ├─ causal 错误      → causal evidence 更新因果模型
 ├─ 重复出现         → learn draft → shadow → promote（规则升级）
 └─ 新测试用例       → eval 回归用例（如：导出 10GB 用例）
```

`--save` 把反思追加到 `.pi-batch/reflections.jsonl`（不可变账本，供
metrics/health/retro 聚合）。

## 6. 用法

```bash
# R0 快速反思（默认，秒级）
pi-batch reflect --task "需求文本"
pi-batch reflect quick --task "..." --evidence "运行结果/测试输出"

# R1 工程反思（架构/复杂度）
pi-batch reflect architecture --task "..." --code pbatch
pi-batch reflect security --task "支付审批，生产"

# R2 高级反思（全 12 维）
pi-batch reflect full --task "..." --code src --evidence "测试输出"

# 沉淀
pi-batch reflect full --task "..." --code src --save --json
```

退出码：`reflection_score >= 75` → 0；否则 1（critical 发现时脚本可阻断）。

## 7. 与四文档的关系

- 理论层：Critic Layer 是 AADM §25（多 Agent 组合）中 IndependentReview
  的确定性实例化
- 运行时：REFLECT 是 AADM-R 生命周期 VERIFY 之后的显式阶段
- 治理：假设审计对接 truth（§20 真值维护）、知识提取对接 learn（§21
  规则晋升）——闭环
- 反方验证对接 nversion（§35.7 非确定性控制）与故障注入
