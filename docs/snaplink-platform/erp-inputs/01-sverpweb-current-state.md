# ERP 场景证据：sverpweb-stable 现状（Verified，2026 人工证据扫描）

来源：`~/workspace/job/sverpweb-stable`（git log / README / apps/api/src 源码扫描）。

## 技术栈（Verified）

- Monorepo：pnpm workspaces + turbo；`apps/api`（Hono BFF，Bun 运行）、
  `apps/web`（Vue 3 + TSX + Pinia + vue-router + vue-i18n）、`packages/types`（前后端共享契约）。
- Web 依赖 `@iris-ui-kit/vue@0.2.18`、`@iris-ui-kit/core@0.2.0`（iris-ui 仓库 npm 发布包）。
- 数据持久化：SQLite（`APP_DATABASE_PATH`）；会话：AES-256-GCM 加密 SQLite，
  浏览器只持有 HttpOnly Cookie。

## 已接入的平台能力（Verified）

1. **snaplink 认证**：`apps/api/src/auth/snaplink.ts` 使用官方 `@snaplink/sso-client`
   SDK（SSOClient / buildHostedLoginURL / PKCE S256 / 授权码换令牌 / 刷新令牌轮换）。
   userinfo、权限、角色、菜单、系统用户、系统角色从 Snaplink 加载。
   托管登录/Consent 由 Snaplink Console 承担（README 明确：无凭据的 Iris UI 登录跳板）。
2. **安全边界**：来源校验、CSRF、权限通配符、服务端 API 权限边界
   （`apps/api/src/auth/permissions.ts`、`http/middleware`）；Snaplink 菜单到本地
   路由白名单映射（`menu-normalizer.ts`，远端数据不能注入任意组件/URL）。
3. **审计 Outbox 体系**（`apps/api/src/audit/`，完整实现）：
   - `contracts.ts`：AuditEvent / AuditOutboxStore / AuditApplicationTransport；
   - `sqlite-outbox.ts`：本地 SQLite 队列，pending/publishing/published/failed 状态机，
     lease 机制（`AUDIT_PUBLISH_LEASE_MS`）、批量发布（`AUDIT_PUBLISH_BATCH_SIZE` 默认 50）；
   - `client.ts`：HttpAuditApplicationClient，POST `{AUDIT_APP_URL}/api/v1/audit/events`，
     载荷 `{schemaVersion, source, sentAt, events}`，事件 ID 稳定、接收方可去重；
   - `request-middleware.ts`：`emitAuditEvent` 请求级埋点；
   - `routes/audit-admin.ts`：管理端查看 outbox 状态（pending/publishing/published/failed）。
   - 配置：`AUDIT_SOURCE`（默认 sverp-web）、`AUDIT_APP_URL`、`AUDIT_INGEST_PATH`（默认
     /api/v1/audit/events）、`AUDIT_APP_TOKEN`。
4. **业务路由**：quality（一品一单）、APS、bs-standard-pricing（精准报价）三条业务
   路由 + 系统管理（system/audit-admin/media 等），内容/媒体/质检/APS/精准报价聚合数据
   存 SQLite。

## 尚未接入（Verified 缺失）

- 媒体/附件走**本地 SQLite + 本地文件**（`apps/api/src/media/sqlite-store.ts`），未接
  aero-vault（无 REST/S3/WebDAV 客户端、无分片上传、无版本/分享/保留策略）。
- 通知：未见 aero-im 客户端（无 webhook/WS/邮件推送调用）。
- 账户资料/组织：用户信息只来自 Snaplink userinfo，未见 aero-id 的
  profile/组织/部门/职位查询。
- 审计目标未指向 snaplink-audit-governance：`AUDIT_APP_URL` 可配置但未验证接
  governance 的 ingest；audit-admin 是本地 outbox 状态页，不是 governance 查询。
- 管理端：无 snaplink-console 能力发现/统一审计时间线嵌入。

## 工程门禁

- `pnpm check` = turbo run typecheck test build；有 vitest 单测
  （auth/snaplink.test.ts、snaplink.e2e.test.ts、audit outbox 全套测试等）。
- 无 pi-batch/pbatch 嵌入（无 pi-batch.py、pi-batch.yaml、campaign 配置）。
