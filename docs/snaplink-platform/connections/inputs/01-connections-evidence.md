# 6 项目连接现状证据（2026-08-08 代码级收集，供连接分析流水线使用）

> 来源：各仓库实际代码（sverpweb apps/api/src/integrations/clients/、
> snaplink-console lib/api/、aero-vault 部署配置）——Verified 等级。
> 端口：snaplink=13015、console=13017、audit-governance=13018、
> aero-vault=13019、aero-id=13020、aero-im=13022、sverpweb=13014。

## 连接 1：sverpweb → snaplink（认证授权）
- 接口：OIDC code flow + PKCE（/api/auth/login、/token、/userinfo）、
  session（BFF cookie）、权限校验（permissions claims）
- SDK：@snaplink/sso-client（docs/sdks/typescript，exports 指向 dist）
- 服务账号：sso-admin（admin:*+audit+platform）、sverp-audit（audit:*+platform）、
  sverp-vault（read/write/admin scope）
- 现状：BFF 代理 + SDK 客户端（SnaplinkTokenClient 统一 token）

## 连接 2：sverpweb → snaplink-audit-governance（审计）
- 接口：POST /api/v1/events:batch（审计事件批量入账）、
  GET /api/v1/events（查询）、POST /api/v1/schemas（schema 注册）
- 凭据：sverp-audit（audit:event:write）、sso-admin（audit:policy:write）
- 链路：local outbox（SQLite）→ governance-transport 映射 → 批量提交；
  permanent 毒丸隔离 + 幂等重放；schema 注册每事件一条
- 现状：sverp 侧 338 事件全部 published，健康检查脚本防漏注册

## 连接 3：sverpweb → aero-vault（文件存储）
- 接口：POST /v1/files/:key/presign?op=put|get、GET/DELETE /v1/files/:key、
  PUT /v1/files/:key/metadata、PUT/DELETE/GET /v1/access/acl、
  GET/POST /v1/admin/departments、PUT /v1/admin/departments/:id/members/:subject
- 凭据：sverp-vault（read write admin scope）
- 现状：presign 直传（BFF 不缓冲）、受控引用（/api/resources/:key 双重校验）、
  部门维度 ACL（department_members 实时）、质检附件 vault 存储

## 连接 4：sverpweb → aero-im（消息通知）
- 接口：POST /api/rooms/:id/messages（房间消息）、ensureRoom/login
- 凭据：aero-im 应用内登录（app token）
- 现状：best_effort 通知（审批/质检异常/文件共享变更→频道广播）

## 连接 5：snaplink-console → snaplink（管理面）
- 接口：GET /api/v1/admin/endpoints（能力发现）、/api/v1/admin/*（CRUD）、
  GET /api/v1/audit/events（审计时间线）、/api/v1/scim/*
- 凭据：admin 用户 OIDC token（admin scope）
- 现状：SnaplinkAdminApi（Flutter，浏览器直连 snaplink）

## 连接 6：aero-vault → snaplink（认证信任）
- 接口：GET :13015/.well-known/jwks.json（JWT 验签）、tenant claim 映射
- 配置：AUTH_JWT_ISSUER=http://192.168.123.52:13015、
  AUTH_JWKS_CLIENT_TENANTS=sverp-vault:sverp
- 现状：服务账号 JWT（vault 侧验签）

## 连接 7：aero-im / aero-id / audit-governance → snaplink（认证信任）
- 接口：JWKS 验签（各服务配置 AUTH_JWT_ISSUER/JWKS_ENDPOINT）
- 现状：aero-id 有 gRPC 校验（snaplink:token:validate 备选，未定案）、
  aero-im 走 JWT 验签；governance 用 OAuth 服务账号

## 连接 8：snaplink-console → 其他服务（管理面 BFF）
- 现状：nginx BFF 代理（/tmp/snaplink-console/proxy）；console 管理面
  聚合各服务 admin API（governance 等），两 token 模型（用户 OIDC +
  服务账号）按 scope-matrix 提案推进中
