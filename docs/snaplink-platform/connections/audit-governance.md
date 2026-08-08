文档已产出：`docs/snaplink-platform/connections/audit-governance.md`（179 行 ≥ 60 行要求；12 处 Verified、1 处 [PROPOSED] 标注）。

**A. 对外接口清单**（代码级 Verified，openapi.yaml + server.go 门禁）：
- **摄入面**：`POST /api/v1/events` + `events:batch`（`audit:event:write`；batch 逐条 receipt 幂等；**body tenant_id 忽略**，tenant 由签名 claims 解析）+ receipt 查询——消费方 sverpweb 已对接、vault/id 已实现待启用
- **查询面**：`/api/v1/events`（16 过滤参数 + cursor）、operations/aggregates 时间线、`integrity/verify`——门禁 `audit:event:read`，**非写权限必须带 tenant 上下文**（platform 经 `?tenant_id=` 跨租户）
- **Schema 面**：`POST/GET /api/v1/schemas`——`audit:policy:write`，**立即生效无审批链**（摄入校验 fail-closed 直接引用 Active schema）
- **治理面**：exports（`audit:export:create`，API 边界不暴露原始错误）、legal-holds（`audit:legal_hold:manage`）、retention policies、restores（approve/reject 双人）、tenants/sources/admin/actions（自审计 AdminAction 同事务 append，fail-closed）
- **权限模型**：roles→permissions 映射表；token `scope` 字符串**直接并入权限、无通配符展开**（`audit:*` 不会命中 `Allows()`）

**B. 依赖**：governance **不直接调 snaplink**——只远程 JWKS 验签其签发的 JWT（kid 唯一匹配/alg 严格校验/私钥材料禁止/HTTPS 强制）；client 身份 = client_id/azp 一致性校验；现状凭据 `sverp-audit`/`sso-admin` 的 scope 名与 governance 权限名**恰好一致**。

**C. 四项改动**（各含理由/影响面/验收标准）：
1. **C-1 读侧专用账号**：写账号（sverp-audit）与读账号分离（对偶 deny 规则）；新增 `sverp-audit-reader` 仅读权限；sso-admin 移除常规查询用途
2. **C-2 schema 审批链**：现状任何 `audit:policy:write` 者可单方注册（误注册/错注册直接放宽入账面）；加 status（draft→approved）状态机 + 第二人批准（复用 restores 双人语义），摄入只接受 approved
3. **C-3 legal hold/export 对 console**：命名冲突发现——scope-matrix 提案名 `audit:legalhold:apply` ≠ governance 现收 `audit:legal_hold:manage`（建议保留现状名零改动）；走 BFF 服务账号禁直连；释放/下载高风险操作按决策 #6 双人控制
4. **C-4 两套账本关系**：**保持分离是正确设计**（域/权威/保留均不同，非重复）；真实缺口在**统一时间线视图**——console 经 BFF 聚合双源 + request_id/trace_id 关联，不引入跨账本同步

另附克制清单（不统一账本、不给写账号加读、不绕过审批、不开放直连）。
