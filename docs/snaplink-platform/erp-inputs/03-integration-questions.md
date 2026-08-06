# ERP 场景待回答的问题（场景分析流水线的输入目标）

背景：sverpweb-stable（Vue3 + iris-ui + Hono BFF）是 ERP 业务应用，已接 snaplink
认证；平台六件套（snaplink / snaplink-console / snaplink-audit-governance /
aero-id / aero-im / aero-vault）是其能力底座。请基于证据文件 01/02 回答：

## A. 能力接入的优先级与顺序

1. 六件套中哪些能力是 ERP **必须接入**的（高价值/低成本），哪些应克制
   （当前本地实现已够用，先不接）？给出按价值/成本排序的接入路线图
   （P0/P1/P2），每项说明证据（现状文件、组合纪律、平台成熟度）。
2. 审计是当前最大杠杆：sverpweb 已有完整 Outbox（合同格式与 governance
   ingest 吻合），把 `AUDIT_APP_URL` 指向 audit-governance 的接入成本/收益/风险？
   本地 SQLite outbox 与 governance 账本并存时的职责边界（谁是真源）？
3. 媒体/附件从本地 SQLite+文件迁移到 aero-vault 的取舍：哪些先接（上传下载
   直连）、哪些后接（版本/分享/保留/RAG）；BFF 代理 vs 前端直连两种模式；
   迁移期双写/只读切换策略。

## B. 架构与治理

4. BFF（Hono）在组合中的定位：认证代理、能力聚合、还是仅路由？
   与 snaplink-console 管理端如何分工（ERP 自己的管理页 vs 平台统一控制面）？
5. aero-id 的 profile/组织/部门对 ERP 的价值（人员选择器、组织树），接入
   成本（gRPC vs HTTP）与克制红线（不得每请求调 aero-id）。
6. aero-im 通知的接入面：ERP 的哪些事件值得发通知（审批、异常、任务分派），
   通知模板/偏好放哪；与审计的 failure_policy 对齐（异步不阻塞业务）。
7. iris-ui 的角色：sverpweb-stable 是否应锁定 npm 发布包版本、参与 iris-ui
   MCP/registry 生态；iris-ui Alpha 状态对 ERP 的版本策略（锁版本 + 升级门禁）。
8. 多租户/tenant 上下文：ERP 数据模型与平台 tenant 的关系（低成本预留：
   tenant_id 进主查询/唯一索引、事件带组织上下文）。

## C. 工程与运营

9. 各仓库维护节奏：sverpweb-stable 尚未嵌入 pi-batch/pbatch——是否值得
   同步（campaign 方向发现、门禁、7×24 轮巡）；平台六件套已有的
   platform-status.sh / platform-247.sh 是否扩展覆盖 ERP 仓库。
10. 验证与验收：跨仓库组合验证（compose + E2E + suite-check）、
    Change Manifest 门禁；审计链路 E2E（ERP 操作 → outbox → governance 账本 →
    console 时间线）的验收标准。
11. 明确**不接**的能力清单（克制原则），防止过度设计。

## 输出要求

- 全部结论标注证据等级：Verified（源码/文档实证）、Proposed（推理建议）。
- 路线图可执行：每项含仓库、文件级改动面、门禁、验收标准。
- 遵守组合纪律：默认只改当前仓库；跨仓库修改必须改变双方契约且经套件验证；
  审计 L2 是部署规则；AI 产出是提案，人工批准后提升。
