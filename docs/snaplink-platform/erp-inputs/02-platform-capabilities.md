# ERP 场景证据：平台六件套能力目录（Verified，来源各仓库 README / platform-map.yaml）

组合纪律（examples/snaplink-platform/platform-map.yaml）：各产品是**独立产品**，
通过标准协议组合；数据权威源唯一；同步依赖有向、最长 3 跳；禁止直连数据库、
禁止双向同步、禁止审计回调业务服务、禁止每请求调 aero-id 校验 token、
禁止浏览器绕过服务 API。审计分级 L0 本地日志 / L1 标准事件协议 / L2 governance 适配
（L2 是部署规则不是产品硬编码）。所有流水线产物是提案，须人工核对后提升。

## 1. snaplink（认证授权核心，Go）

- OAuth2 + OIDC SSO：SDK（interfaces/sso 嵌入）/ sso-server 二进制 / ssoclient
  （local/remote/dev 三态）；HTTP `/.well-known/openid-configuration`、/token、
  /userinfo、/api/v1/admin/*；gRPC；sso-ctl（audit-verify/import/migrate/snapshot）。
- 拥有：session、access/refresh token、api-key、role、permission、policy、
  client-application、tenant-context。
- 集成端口：AuthorizationProvider、AuditSink（认证中心的策略/RBAC/token 撤销
  是否进统一审计账本——此前证据扫描列为缺口，需验证）。

## 2. snaplink-console（平台控制面，Flutter Web）

- 入口：/admin/（管理员）、/login/（托管登录）、/portal/（自助门户）、
  /developer/（RFC 7591/7592 注册）、/setup/、/device/verify（RFC 8628）。
- 原则：以后端契约为准——加载运行时 `/api/v1/admin/endpoints` 能力清单 +
  docs/openapi.yaml 合并；不拥有业务数据，只聚合各服务管理 API；
  已有审计日志服务页（lib/services/audit_log_service.dart）。
- 集成端口：ManagementGateway（聚合各服务管理 API）。

## 3. snaplink-audit-governance（审计事实源，Go，架构设计期+参考实现）

- 能力：event-ingest（`/api/v1/audit/events`，批次去重语义与 sverpweb 的
  HttpAuditApplicationClient 载荷格式吻合）、不可变账本/哈希链、查询分析、
  合规导出、留存策略、Legal Hold、完整性校验；5 个 ADR（消息传输、事件编码
  Schema 兼容、哈希链分段检查点、查询投影与租户路由、合规归档与密钥）。
- 边界：ERP 等业务系统保留领域状态，通过 Outbox 或 SDK 发送审计事件；
  OpenTelemetry 管技术链路，不替代业务审计事实。
- 多租户、多业务系统；操作时间线；`source` 字段区分来源系统。

## 4. aero-id（账户业务域，Go）

- 管理账户拥有的文件/消息/评论；身份在外部 SSO（snaplink gRPC 校验 token +
  批量用户查询缓存）；不存账户记录，每行引用 SSO uid。
- HTTP (Gin :8080) + gRPC (:9090) 双传输共享 service 层；repository GORM/Postgres；
  storage 本地 HMAC 签名预签名 URL 或 S3（MinIO 兼容）。
- 拥有：user-profile、organization、department、position、membership、
  account-lifecycle、external-identity-binding。
- 已有 internal/connector/auditgovernance/（publisher/oauth）雏形。

## 5. aero-vault（文件平台，Go）

- REST /v1 + S3 兼容（可选）+ WebDAV + MCP 四协议共享 FileService；
  auth·tenant·rate-limit·OTel·CORS 中间件链；存储 local/s3/oss/cos；多租户；
  RAG 管道（上传即分块可检索）；事件总线 + webhooks + reconciliation。
- 拥有：file-metadata、folder、file-version、share-link、retention、
  object-storage、recycle-bin。
- 已有 Snaplink 浏览器流认证 + internal/auditgovernance/token.go。
- aero-im 附件经其 BlobStore（Snaplink client_credentials）——跨产品组合先例。

## 6. aero-im（消息/通知/直播，Rust）

- IM 文本/实时 WS、AI-Ready 消息块（text/mention/code/file/voice/card/tool_call/
  thought）、协作（编辑/删除/反应/已读回执/typing）、RAG 搜索（FTS/向量/hybrid）、
  Agent in channel、内容审核（关键词+AI+AutoMod）、1:1 通话/群通话/SFU/直播
  RTMP→HLS/弹幕礼物/治理申诉、WHIP/WHEP、SRT 摄入、MLS E2E scaffold。
- 通知渠道：in-app、WebSocket、email、sms、push、webhook、template、
  notification-preference。
- 附件走 aero-vault BlobStore（Snaplink client_credentials 鉴权）——组合先例。
- 跨主机/公网/供应商凭据项仍标注"待验证"（NAT/TURN、FCM/APNs、SMTP/OIDC/OTLP）。

## 7. iris-ui（ERP 前端基础设施，跨框架）

- AI-native、token 驱动、跨 React/Vue/Solid/Svelte；@iris-ui-kit/core 共享行为，
  每框架薄适配层；154 组件 × 4 框架，616 adapter contracts；
  12 个插件（editor/pro-table/charts/form-builder/notifications/admin/calendar/
  dashboard/kanban/markdown/query-builder/locale-zh）；skins 主题、icons、
  registry/marketplace、manifest.json + llms.txt（AI 原生消费）、MCP server
  （11 个组件发现/生成/校验工具）、eslint-plugin 架构规则。
- Alpha 阶段：npm 首次发布未授权（sverpweb-stable 用的是已发布 0.2.x）；
  CI：测试/覆盖率/类型检查/lint/构建/27 包外部安装/打包预算/RSC/四框架
  E2E/视觉回归/基准/token 审计/架构棘轮。
