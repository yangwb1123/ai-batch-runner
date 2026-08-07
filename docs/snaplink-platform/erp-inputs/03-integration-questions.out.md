证据已收到，完成归一（零 LLM 分析成本，事实原样保留）：

## 证据归一：`03-integration-questions.md`（场景分析输入目标——11 问）

**provenance**：场景分析流水线输入目标定义（基于 01/02 证据），非新事实，是下游 kickoff 必须逐条回答的问题集。

**要点核对（供下游 aggregate/kickoff 引用）**：

- **A 优先级与顺序（Q1–Q3）**：六件套必须接入 vs 克制的价值/成本排序（P0/P1/P2，含证据）；审计接入成本/收益/风险（Outbox 格式吻合是杠杆点）；媒体迁移取舍（先接直传、后接版本/分享/保留/RAG）、BFF 代理 vs 前端直连、迁移期双写/只读切换。
- **B 架构与治理（Q4–Q8）**：BFF 定位（认证代理/聚合/仅路由）与 console 分工；aero-id profile/组织价值与克制红线（不得每请求调）；aero-im 事件面（审批/异常/任务分派）与 failure_policy 对齐（异步不阻塞）；iris-ui 版本策略（锁版本 + 升级门禁）与 MCP/registry 生态；tenant 上下文低成本预留（tenant_id 进主查询/唯一索引、事件带组织上下文）。
- **C 工程与运营（Q9–Q11）**：pi-batch 嵌入价值（campaign/门禁/7×24）与 platform-status/247 脚本扩展；跨仓库组合验证（compose+E2E+suite-check、Change Manifest）、审计链路 E2E 验收标准（ERP 操作 → outbox → governance → console 时间线）；**明确不接清单（克制原则，防过度设计）**。

**输出要求**（下游约束）：结论标注证据等级（Verified/Proposed）；路线图可执行（仓库/文件级改动面/门禁/验收标准）；组合纪律（默认只改当前仓库、跨仓库改契约须套件验证、审计 L2 是部署规则、产出是提案）。

**下游衔接**：本文件是 kickoff 阶段的回答提纲；其中 Q2（审计真源）、Q10（审计链路 E2E 验收）已由证据 04/05 提供部分事实（契约对拍 B1–B5 复核、M0/M1/M2 落地实测），kickoff 评估时应以 05 的"当前实际状态"覆盖 03 的提问前提。
