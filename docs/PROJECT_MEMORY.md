# 工程知识循环（Engineering Knowledge Loop）

> **定位**：从"一次性流水线"升级为**基于项目状态的增量式 Agent 工作流**
> ——像维护同一代码库 5 年的资深团队，而不是每次重新培训的外包程序员。
> 核心不是增加 Agent 数量，而是：**系统状态记忆 + 增量推理 + 影响分析
> + 决策缓存 + 知识反向更新**。

## 1. 增量式工作流（不是每次全流程）

```
              User Prompt
                   │
        ┌──────────▼──────────┐
        │   Change Impact Agent │  ← pi-batch impact
        │   （影响分析先行）      │
        └──────────┬──────────┘
                   │
        ┌──────────▼──────────┐
        │   Project Memory     │  ← pi-batch project（facts/decisions）
        │   ADR 决策缓存        │  ← pi-batch adr find（不重新讨论已定项）
        └──────────┬──────────┘
                   │
      ┌────────────┼────────────┐
      │            │            │
 Simple Change  Feature      Architecture
      │            │            │
 Coding→Test  Selected     Full Review
              Agents       （L2/L3 触发）
                   │
              Validation
                   │
          Update Memory（代码→知识反向更新）
```

| 工作等级 | 条件 | 流程 | 仓库对应 |
|---|---|---|---|
| L0 | 文件 <3 / 无接口 / 无数据库 | Coding → Test | profile mode + pipeline |
| L1 | 局部功能 | Impact → Backend/Frontend → Test → Review | `impact` 输出 |
| L2 | 模块变化 | Domain/Architecture → DB → Coding → Review | +architect/domain_expert |
| L3 | 系统变化 | 完整流程（全角色审查） | +security/qa/arch review |

## 2. 组件

| 命令 | 职责 | 输入 → 输出 |
|---|---|---|
| `pi-batch impact --task "..." [--code DIR]` | **变更影响分析**：受影响面（数据库/API/前端/权限/安全/性能）→ 风险 → 需要角色 → 工作等级 → 变化成本 → 相关 ADR | 需求 → Change Impact Report |
| `pi-batch adr new|list|get|find|supersede` | **决策记忆库**：结构化 ADR（decision/context/alternatives/rejected reasons/status 状态机） | 决策 → docs/adr/ADR-### |
| `pi-batch project init|facts|index|show|verify` | **项目状态记忆**：facts（已确认事实带来源）/architecture/decisions 索引/constraints | 记忆文件 → 健康报告 |
| `pi-batch reflect`（已有） | 反思纠查（12 维度 + 假设审计） | 决策链 → findings → learn/truth 闭环 |
| `pi-batch profile`（已有） | L0-L3 路由（增量调度的基础） | 需求 → 模式/风险/自主性 |

## 3. 关键原则

1. **影响分析先行**：执行前回答"改这个会影响什么"（不是"改哪里"）
2. **事实与推理分层**：facts.yaml（Confirmed，带来源）vs assumptions
   （profile/truth 管理）——不把猜测当事实
3. **决策缓存**：ADR 库——已定事项（MinIO/PostgreSQL/服务边界）不再
   重新讨论；`impact` 自动引用相关 ADR
4. **增量触发**：L0/L1 只触发 Coding+Test；L2/L3 才触发完整评审——
   成本随影响面增长，而不是每次全流程
5. **知识反向更新**：代码变化 → graph extract / advance → `project
   index`（decisions 同步）/ facts 更新——防记忆过期
6. **变化成本入决策**：`impact` 输出 change_cost（low/medium/high/
   critical）——控制变化成本，不追求完美

## 4. 与已有体系的关系

- **增量调度**：profile（L0-L3）已有 → impact 复用并输出所需角色
- **动态角色**：meta.py 相关性编排已有 → impact 提供另一路角色输入
- **纠查**：reflect（确定性）+ prompts/critic.md（LLM 化身）→ 发现进
  learn/truth 闭环
- **技术债**：advance（P0/P1/P2 批次）→ 健康分（health）
- **权限边界**：capabilities trust（Agent×能力×设备信任区）

## 5. 20 年视角补充（仍需考虑的）

1. **决策缓存过期**：ADR superseded 必须显式标记（adr supersede），
   `project verify` 检查索引一致性
2. **知识粒度**：facts 必须可验证（带 source）——否则退化为聊天记录
3. **变化成本要进 gate**：impact 输出 cost=critical 时建议人工门禁
4. **反事实推理**：`profile` 的 guardrails + `impact` 的受影响面——
   单公司需求问"未来多公司？"（company_id 预留）
5. **Review Loop 的通过标准**：gate VERDICT + completion 报告——
   不是轮数，是标准
