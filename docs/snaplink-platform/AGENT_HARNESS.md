# Agent 产品设计 Harness（组合需求强制决策程序）

> 目的：把"多项目产品设计规范"变成**机械强制流程**，而不是超长
> AGENTS.md / Prompt 劝说。Agent 的默认链路从
> `Prompt → 找代码 → 改代码` 提升为：
>
> `Prompt → 目标理解 → 场景建模 → 能力发现 → 组合设计 → 变更设计 →
> 决策摘要 → 决策门（机械校验）→ 人工批准 → 分仓实施 → 组合验证`

## 1. 强制流程总览

```
原始需求
  │
  ▼
requirement-pipeline.yaml（本仓库运行）
  ├─ intake                需求解释（真实目标/角色/范围分级）       [L1-L2]
  ├─ scenario              完整场景建模（主流程/异常/后续/体验）      [L2]
  ├─ capability_discovery  能力发现（先能力后项目，复用优先）         [L3-L4]
  ├─ composition_design    组合设计（主责任/不修改/数据权威/失败策略） [L5]
  ├─ change_design         变更设计（API/事件/权限/兼容/部署/回滚）    [L6]
  ├─ decision_summary      结构化决策摘要 JSON（机器可读）            [L7]
  ├─ decision_gate         决策门：check-decision-summary.py 机械校验
  │                        + 独立 gatekeeper VERDICT（fail closed）
  └─ approval_note         人工批准点（PROPOSAL 声明 + change_id）
        │
        ▼ 人工批准后
Change Manifest（check-change-manifest.py 校验）
        │
        ▼
Scoped Execution：各产品仓库 campaign 按摘要实施（只改受影响项目）
        │
        ▼
组合验证：suite 仓库 E2E + 契约测试 + 兼容矩阵
```

## 2. 机械强制点（不是 Prompt 劝说）

| 强制点 | 机制 | 失败后果 |
|---|---|---|
| 决策摘要必填维度 | `scripts/check-decision-summary.py` 作为 `decision_gate` 阶段 `validate_cmd`（产物级验证器） | 校验失败 → 摘要不落盘 → gate 无 VERDICT → 流水线阻断 |
| 主责任项目合法性 | 校验器对照 `platform-map.yaml` 项目名（fail closed，未知项目即失败） | 同上 |
| 组合类型必须声明协作项目；协作与不修改不得重叠 | 校验器交叉检查 | 同上 |
| 失败策略必须显式（每个依赖 fail_closed/fail_open/local_queue/best_effort_async/durable_async） | 校验器 `integration.failure_policies` 非空 | 同上 |
| 跨仓库变更边界 | `scripts/check-change-manifest.py`：change_id/受影响/不受影响/失败策略/兼容性 | 边界声明无效 → 实施前即失败 |
| 范围膨胀 | gatekeeper 校验 scope.priority 未静默越界 + 对抗审查角色 | VERDICT FAIL |
| 实施边界 | 各仓库 campaign 只跑自己的仓库；suite E2E 只跑组合层 | 流程纪律 |

## 3. 角色与职责（meta 编排可动态挑选）

| 角色 | prompt | 职责 | 写权限 |
|---|---|---|---|
| Product Discovery | `prompts/requirement_interpreter.md` | 目标/角色/场景/范围分级 | 无 |
| Capability Architect | `prompts/capability_architect.md` | 能力发现、复用优先 | 无 |
| System Composition | （流水线内嵌） | 主责任/协作/数据权威/失败策略 | 无 |
| Contract Agent | （流水线内嵌） | API/事件/权限/兼容性 | 无 |
| Composition Reviewer | `prompts/composition_reviewer.md` | 反方寻找错误：责任/耦合/失败语义/越权/兼容 | 无 |
| Repository Agent | 各仓库 campaign | 单仓库实施 | 单仓库 |
| Gatekeeper | gate 阶段 | 独立裁决 VERDICT | 无 |

原则：提出方案、实现、证明正确由**不同 Agent** 承担；反方审查必须存在。

## 4. Agent 上下文（分层加载，不堆长 Prompt）

- 全局：`docs/snaplink-platform/COMBINATION_MODEL.md`（原则）
  + `platform-map.yaml`（机器可读事实：owns/capabilities/数据权威/依赖规则）
- 项目：各仓库 `AGENTS.md` + `docs/agent-context/`（平台契约引用）
- 任务：流水线阶段注入 `{input_content}` + 决策摘要 JSON
- Agent 先读地图再按需检索代码——不全文搜索、不全部塞入上下文

## 5. 目标式 Prompt 模板（给用户的输入建议）

> 不要把我的描述直接视为最终技术方案。首先识别：真实用户目标；角色与
> 完整业务流程；必要条件/风险条件/可选增强；已有项目能力；主责任项目、
> 协作项目与**不应修改**项目；数据权威源与依赖方向；同步/异步/工作流
> 选择；失败/重试/幂等/补偿策略；权限/租户隔离/审计/敏感数据要求；
> API/事件/版本兼容/部署/回滚影响；可验证的验收标准。优先复用和组合
> 现有能力；不得因多项目场景默认修改全部项目。完成上述分析并生成
> 决策摘要且通过决策门之前，不得进入编码阶段。

## 6. 落地清单

- [x] `platform-map.yaml` — 机器可读项目地图（能力目录/数据所有权/依赖规则/审计分级/端口）
- [x] `scripts/check-decision-summary.py` — 决策门机械校验（9 组必填维度 + 项目引用 + 失败策略 + 命名规范）
- [x] `scripts/check-change-manifest.py` — 变更清单门禁
- [x] `examples/decision-summary.example.json` / `change-manifest.example.yaml` — 合法示例
- [x] `requirement-pipeline.yaml` — 8 阶段强制决策流水线（含决策门；真实需求端到端跑通，4 次校准已固化）
- [x] `prompts/requirement_interpreter.md` / `capability_architect.md` / `composition_reviewer.md` + role_keywords
- [x] 真实运行校准：@file 需求注入、拒绝检测、命名规范、VERDICT 正则（全部机械强制）
- [x] 工具同步 6 仓库（`sync-tool.sh`，每仓库薄壳 + pbatch/ + quality.py + 按语言验证器）
- [x] snaplink-suite 组合仓库实例化（~/workspace/demo/snaplink-suite，suite-check PASS）
- [x] COMPOSE-2026-017 分仓实施配置（campaign-aero-vault.yaml + tasks-compose-2026-017.yaml，dry-run 验证）
- [ ] 各仓库 campaign 实际运行（需人工批准提案后执行）
- [ ] suite 仓库 E2E 按摘要验收（20 项边界场景）
