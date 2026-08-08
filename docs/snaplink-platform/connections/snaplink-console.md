文档已产出：`docs/snaplink-platform/connections/snaplink-console.md`（139 行 ≥ 60 行要求；8 处 Verified、1 处 [PROPOSED] 标注）。

**A. 现状接口清单**（代码级 Verified，nginx.conf + lib/ 核查）：
- **架构**：Flutter Web 同源 nginx；浏览器持 **admin 用户 OIDC token 原样透传**（连接 5，命中 `console-browser-bypasses-service-api` 禁止项）
- **nginx 代理清单**：→ snaplink（auth/api/me/token/roles 等全段）、→ billing、→ stripe adapter；**governance/vault/id/im 零 upstream**
- **消费面**：`/api/v1/admin/endpoints` 能力发现、admin/* 通用 JSON 传输（build-time catalog 校验）、SCIM（`application/scim+json`）、审计时间线 `GET /api/v1/audit/events`（snaplink 账本）、门户 `/me/*`（铃铛走 snaplink 自服务通知，**非 aero-im**）
- **关键澄清**：`governance_models.dart` 的"governance 模块"全部是 **snaplink 自身路径**（storage-health/config/compliance/snapshots/changes）——**不是 audit-governance 服务的 API**
- **审计现状**：console mutation 只记**本地 localStorage 日志**（可清除可伪造，非服务端账本）

**B. 四项改动**（各含理由/影响面/验收标准）：
1. **B-1 两 token 模型落地**：浏览器 token 被窃 = 全平台管理权限；BFF 服务账号持枚举 admin scope（禁通配符）+ nginx 层强制剥离浏览器 token + decision_id 契约桥保持用户归因
2. **B-2 governance 查询经 BFF**：治理账本当前**完全不可达**（无路由）；新增白名单路由（events 读/operations/integrity/legal-holds/exports）+ 专用查询账号 + 双源时间线聚合
3. **B-3 BFF 路由清单**：四元组注册表（上游/路径白名单/服务账号 scope/用户权限映射）；**按需增长**——首批 vault 部门 + governance，不预代理全部
4. **B-4 审计上报**：服务账号代理后上游 actor 将失真——必须透传 decision_id + 用户 identity（不伪造 actor）；本地日志降级为 UX 辅助；snaplink 认证审计已覆盖 console 自身操作无需重复

另附克制清单（不预代理、不升级浏览器 token、不做账本统一、本地日志不上报）。
