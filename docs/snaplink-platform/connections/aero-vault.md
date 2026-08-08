文档已产出：`docs/snaplink-platform/connections/aero-vault.md`（208 行 ≥ 60 行要求；15 处 Verified、1 处 [PROPOSED] 标注）。

**A. 对外接口清单**（代码级 Verified，路由权威表 internal/api/rest/router.go）：
- **文件面**：presign put/get（sverpweb 浏览器直传/直链下载，BFF 不缓冲）、`/v1/files` CRUD/metadata/tags/versions/restore/lock、multipart/batch/folders——门禁 = `requireRESTScope`（GET=read/其余=write）
- **ACL 面**：`/v1/access/acl` + `/access/check`（sverpweb 部门维度授权，principal 支持 user/department/group/role/everyone）
- **部门目录面**：`/v1/admin/departments*`——sverpweb `syncDepartment()` **单向幂等同步**（按 name 匹配创建 + 全量 PUT members；vault `ListSubjectDepartments` 实时生效）
- **共享/保留面**（vault 已实现、sverpweb **未消费**）：`/v1/shares`、`/v1/assets`、lock/legal-hold/lifecycle
- **管理面**：tenant/quota/budget/keys/jwt/jobs/audit——门禁 = `requireAdmin`（`ScopeAdmin`），**注册表关闭时隐式放行**（no-auth MVP 行为）

**B. 依赖**：
- snaplink JWKS 验签（SDK rs.JWKSCache，刷新默认 3600s）+ tenant claim 映射（`AUTH_JWKS_CLIENT_TENANTS=sverp-vault:sverp`）+ scope 解析（无识别 scope 整体拒绝）
- 审计上报**已完整实现、默认关闭**（`AUDIT_GOVERNANCE_ENABLED=false`）：事务内写 fact（`WrapRepository` 拦截 RecordAudit/InsertEvent）+ 租约投递（batch 32/退避/缺口回扫）+ 禁用保护 fail-closed；scope 现状名 `audit:event:write` ≠ 提案名 `audit:event:ingest`

**C. 五项改动**（各含理由/影响面/验收标准）：
1. **C-1 审计启用定案**：scope-matrix 的授予条件（relay）**vault 已满足**；定名 + 启用 + 幂等测试
2. **C-2 部门目录**：维持单向（双向违反禁止项）；真实缺口是**成员只增不减**——调岗/离职后残留权限（`syncDepartment` 补差集 DELETE）
3. **C-3 删除审计**：`EventDeleted` 已同事务发 `file.deleted` fact；缺口 = 部门管理四个写端点**无审计**（与 admin.go 不对称）+ 批量删除路径待核查
4. **C-4 share-link/retention**：本期**不接**分享（无需求，deferred）；仅让 sverpweb 删除路径感知 lock/legal-hold 的拒绝语义；lock/legal-hold 入口维持 admin 门禁
5. **C-5 admin 门禁**：核心问题是 `sverp-vault` 持**整段 admin scope** 只为部门同步——拆独立 scope（如 `vault:dept:manage`）或迁 console BFF；生产禁隐式 admin 放行

另附克制清单（不接分享/不双向同步/不开 lock 给 BFF/deny 清单保持）。
