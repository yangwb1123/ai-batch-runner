文档已产出：`docs/snaplink-platform/connections/snaplink.md`（241 行 ≥ 60 行要求；19 处 Verified、7 处 [PROPOSED] 标注）。

**A. 对外接口清单**（全部代码级 Verified，`/home/dwp/snaplink` 核查）：
- **OIDC/OAuth2 面**：`/auth/login`（PKCE code flow）、`/token`（client 认证五种方法：secret_basic/post/private_key_jwt/tls_client_auth/self_signed_tls/none+PKCE）、`/token/introspect`（client 认证必需，批量 opt-in）、`/token/revoke`、`/userinfo`、`/.well-known/jwks.json`（single-flight+ETag）——scope 门禁 = `GrantedScopes` 统一共享门（per-client `AllowedScopes` allowlist，越界整体拒绝）
- **Admin REST 面 `/api/v1/admin/*`**：统一 `AdminMiddleware`（GET=admin:read、其余=admin:write、`admin:*` 通配符、feature gate 热开关）；用户状态管理/治理只读/操作三类代表性端点表；消费方 = console 直连（命中 `console-browser-bypasses-service-api` 禁令）
- **Audit events API**：`/api/v1/audit/events|:id|facets`，仅 `WithAuditAPI()`+auditor 挂载，admin:read 全量门禁
- **gRPC 面**：authz.Authorizer（开放数据面）、admin.v1.*（门禁+自动审计）、audit.v1.AuditWriter（gated）——**无 token validate 服务**

**B. 调用/依赖**：确认**不向 audit-governance 上报**（无 connector；`auth.token.issue` 保持 L1；`tenant.delete` 同步确认 [PROPOSED]）；出站全为 opt-in 可选导出（SCIM push/SSF/SIEM sink）；6 项目组合中出站强制依赖为零。

**C. 五项改动**（每条含理由/影响面/验收标准）：
1. **C-1 scope 注册表落地**：签发引擎 allowlist 门已存在，缺注册表——`AllowedScopes` 为空的 client 完全不设限（GrantedScopes 规则②）
2. **C-2 admin 审计全覆盖**：gRPC 面 interceptor 自动全覆盖；HTTP 面 `recordAdminUserCRUDAction` 等已覆盖，**疑似缺口**：branding PUT/DELETE、backup、admin token revoke、dr/mode、access-policy converge、netpolicy（代码中未见 Record 调用）
3. **C-3 JWKS 轮换**：grace 契约（≥2×最慢下游刷新间隔，vault 默认 3600s）、compromise 无 overlap 路径通知流程、联动 C-4
4. **C-4 gRPC 校验定案**：建议主推本地 JWKS 验证（已实现、零新服务），仅撤回 ≤1s 需求时新增 gated `snaplink.token.v1.Validate`（claim 最小化+限流）；mTLS 拒绝双轨
5. **C-5 审计读侧粒度**：两档模型（admin:read 全量运维 vs tenant-scoped 服务账号 token tenant claim 校验）、sverp-audit 读侧剥离、随连接 8 BFF 化收敛

另附前置依赖链（C-1→C-4→连接 8）、开放决策联动与克制清单（不新增 governance 上报、不引入 OPA）。
