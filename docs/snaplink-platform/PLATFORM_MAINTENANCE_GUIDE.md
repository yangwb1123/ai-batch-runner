# Snaplink 平台维护手册（最终整合版）

> 场景：6 个独立仓库构成组合式平台——snaplink（认证授权）、aero-id（账户）、
> aero-vault（文件）、aero-im（消息通知）、snaplink-audit-governance（审计
> 治理，所有审计操作汇入）、snaplink-console（管理端）。全部由 ai-batch-runner
> 维护，使用全部特性。
>
> 本手册整合整个实战会话的成果：组合架构模型、决策 Harness、审计契约
> 三轮迭代、批次实施、工具自我迭代。所有状态为真实运行结果（2026-08-06）。

## 1. 架构定位（一句话）

**6 个独立可部署产品 + 标准协议组合 + snaplink-suite 组合仓库**——
不是统一平台整体开发（防发布耦合），也不是六套互不理解的标准
（防协议漂移）。核心：内部低耦合、协议可组合、部署可裁剪、版本可兼容。
详见 `COMBINATION_MODEL.md`。

职责边界：
```
aero-id 管"你是谁" → snaplink 管"你能做什么" → aero-vault 管"文件在哪里"
→ aero-im 管"消息怎么送达" → audit-governance 管"发生过什么"
→ snaplink-console 管"管理员如何操作整个平台"
```

## 2. 全特性映射（所有 ai-batch-runner 特性 → 维护场景 + 状态）

| 特性 | 用法 | 实战状态 |
|---|---|---|
| `assess` | 需求先过 8 维完整性 + 规模处方 + 产品化分级（L0-L3）+ 工作流风险分级 | ✅ 平台审计需求 → L3 平台 + 克制处方 |
| `classify` | Go/Rust 后端任务自动路由 backend 流水线；Flutter 路由前端流水线 | ✅ 已用于批次任务路由 |
| `rules` | 前端任务三轴规则筛选（demo/standard/production × page_type × risk）+ 双向校验 | ✅ 生产/table 命中实测 |
| `requirement-pipeline` | **强制决策程序**：目标→场景→能力→组合→变更→决策摘要→决策门（机械校验）→人工批准 | ✅ 真实需求端到端跑通，4 次校准固化 |
| `check-decision-summary` | 决策门机械校验器（9 组必填维度 + 项目引用 + 失败策略 + 命名规范） | ✅ 6 次真实拦截（结构/命名/归属漂移） |
| `check-change-manifest` | 跨仓库变更边界门禁（affected/unaffected 零交集） | ✅ COMPOSE-2026-017 manifest 验证 |
| `check-no-refusal` | 拒绝工作检测（LLM 诚实拒绝 ≠ PASS） | ✅ 虚假 PASS 全链修复 |
| meta 编排 + gate | 对抗审查（security/QA/架构/协议角色并发）+ VERDICT 裁决门 fail-closed | ✅ 审计契约三轮迭代：10 阻断 → 5 修正 → PASS |
| `campaign` | 每仓库方向发现 → worktree 隔离实现 → 完整 SDLC 流水线 | ✅ COMPOSE-2026-017（21 方向）+ B2/B1 实施中 |
| `advance` | 自推进维度迭代（P0 恢复性 → P1 视觉 → P2 架构） | ✅ 代码就绪（advance.py + state.jsonl） |
| `learn` | 事故/RCA → 规则草案 → 人工批准入注册表 | ✅ GATE-FORMAT-001 从真实数据沉淀 |
| `retro` | **运行日志回顾**：事件计数/耗时分布/失败证据 → 自我迭代信号 | ✅ 44 日志 53.5h 分析 → 6 项修正落地 |
| `ps` | 全局运行注册表（谁在哪个仓库跑什么） | ✅ 6 任务并发全景实测 |
| `--wait-lock` | 锁排队（30s 轮询，预算耗尽才退出） | ✅ 4 任务自动排队实测 |
| `context` | 每仓库 docs/agent-context/ 契约路由（关键词 → 平台契约注入） | ✅ 6 仓库部署，路由验证 |
| `memory` | 渐进式会话记忆（manifest/recent/find/read，有界注入） | ✅ 82 会话索引 |
| `eval` / `make ci` | 规则回归 27 用例 + 441 测试 | ✅ 首次全绿（外部漂移测试修复后） |
| 验证器注册表 | pyquality/gobuild/gotest/completion/拒绝检测（按语言 + 任务类型） | ✅ 每仓库 pi-batch.yaml 适配 |
| `--reuse` / fingerprint | 断点续跑（validator 复验 + 输入指纹） | ✅ 多轮续跑稳定 |
| 7×24 | max-rounds 0 + retries + round-delay + circuit 熔断 | ✅ 协作者 36h campaign 实测（150 次熔断） |
| `decision_log` + archive | 决策历史追加式 + 成功后归档 | ✅ 全部流水线启用 |
| suite-check | 组合仓库门禁（compatibility BOM + compose 一致性） | ✅ snaplink-suite 实例化 + PASS |

## 3. 标准工作流（端到端）

```
自然语言需求
  │
  ▼ ① 决策程序（requirement-pipeline，主仓库）
  │    intake → scenario → 能力发现 → 组合设计 → 变更设计
  │    → 决策摘要 JSON → 决策门（机械校验 + gatekeeper）→ 人工批准
  ▼ ② Change Manifest（check-change-manifest 验证）
  │
  ▼ ③ 分仓实施（各仓库 campaign，按批次门禁顺序）
  │    COMPOSE-2026-017 → 审计契约批次 B1（sink）→ B2（aero-id）
  │    → B3（vault）→ B4（IdP）→ B5（im）→ B6（console）
  │    每仓库：分析 → 方向 → requirements → design → 对抗审查
  │    → design gate → implement（真实改代码+工程门禁）→ acceptance gate
  ▼ ④ 组合验证（snaplink-suite）
  │    compose 拓扑冒烟 + 20 项边界场景断言（租户隔离/审计可用性/幂等...）
  ▼ ⑤ 契约反向反馈（contract-feedback）
  │    实施提案核对清单 → 发现缺陷 → v2.1 修订流水线 → gate 重裁
  ▼ ⑥ 自我迭代（retro → learn → 工具修正）
       运行日志 → 失败模式 → 规则草案/工具改进 → 回归固化
```

## 4. 当前资产与状态（2026-08-06）

| 资产 | 状态 |
|---|---|
| 决策程序 + 机械门禁（3 校验器 + 3 prompts） | ✅ 运行中 |
| 审计契约 v2 包（121 事件/scope 矩阵/接入设计/实施批次） | ✅ GATE PASSED，待人工批准 |
| 契约反馈 FB-001..006（实施反向校验） | ✅ 待 v2.1 修订 |
| 批次提案 B1-B6（6 仓库） | ✅ 5/6 完成（B3 排队中） |
| COMPOSE-2026-017 实施（aero-vault campaign） | ⏳ 21 方向推进中 |
| B2 实施（aero-id campaign） | ⏳ 分析 20+/48 模块 |
| B1 实施（sink campaign） | ⏳ 排队等锁（wait-lock 240min） |
| snaplink-suite（compose/E2E/BOM） | ✅ 实例化 + 冒烟框架 |
| 工具增强（ps/wait-lock/retro/meta_timeout/stderr 摘要） | ✅ 全部落地 + 测试 |

## 5. 关键纪律（实战验证）

1. **机械强制 > Prompt 劝说**：能校验的字段绝不依赖 LLM 自觉
2. **默认只改当前仓库**：单项目需求不扩散；跨仓库修改必须改变双方契约 + Manifest
3. **审计 L0/L1/L2 分级**："所有审计进 governance"是 full 组合部署规则，产品核心只依赖 AuditSink 端口
4. **gate fail-closed 是迭代引擎**：REJECTED 带证据 → 收敛 → PASS（三轮迭代实证）
5. **AI 产出是提案**：Verified/Partial/Missing/Proposed 标注纪律；契约须人工批准
6. **共享工作区禁止 git add -A**；单实例锁不抢（--wait-lock 排队）
7. **诚实 SKIP**：E2E/服务不可达标注 SKIP，绝不冒充通过

## 6. 下一步（按门禁顺序）

1. **G1 信任路径**（sink B1 + IdP B4-1 联合）：dev-token 关闭、IdP token 带 tenant_id/roles
2. **G2-G7**：sink → aero-id → vault → IdP → im → console 批次门禁逐项验收
3. **G8 cutover**：全部批次 + 迁移骨架 + 首个事件验证 + DLQ/告警上线
4. **契约 v2.1 修订**：合并 FB-001..006 → 修订流水线 → gate 重裁
5. **suite E2E 20 项**：服务部署后逐项从 SKIP 转 PASS
6. **持续自我迭代**：每轮 retro 验证修正效果（meta 超时归零？gate 无 VERDICT 减少？）
