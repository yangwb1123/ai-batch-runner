证据已收到，完成归一（零 LLM 分析成本，事实原样保留）：

## 证据归一：`02-platform-capabilities.md`（平台六件套能力目录）

**provenance**：来源各仓库 README / `examples/snaplink-platform/platform-map.yaml`，交叉引用点已核对。

**要点核对（供下游 aggregate/kickoff 引用）**：

- **组合纪律**：独立产品、标准协议组合；数据权威源唯一；同步依赖有向 ≤3 跳；禁直连 DB/双向同步/审计回调业务服务/每请求调 aero-id；审计分级 L0/L1/L2（L2 是部署规则非产品硬编码）。
- **snaplink**：OAuth2+OIDC 三形态 SDK、sso-ctl 审计工具；拥有 session/token/RBAC/policy/client/tenant；**AuditSink 集成端口——认证中心策略/RBAC/token 撤销是否进统一审计账本仍列"需验证"**（此缺口保留给评估阶段）。
- **snaplink-console**：以后端契约（endpoints 能力清单 + openapi.yaml）为准、不拥有业务数据；已有 audit_log_service.dart（与 01 号证据"console 审计页仍为本地记录、未接 governance 账本"呼应）。
- **audit-governance**：ingest 载荷批次去重语义与 sverpweb HttpAuditApplicationClient 格式吻合（01 号证据的对接杠杆点，已由证据 05 M2 落地证实）；5 个 ADR；`source` 区分来源系统。
- **aero-id**：HTTP+gRPC 双传输、organization/department/position/membership 域、已有 auditgovernance connector 雏形。
- **aero-vault**：四协议共享 FileService、Snaplink 浏览器流认证、internal/auditgovernance/token.go；**aero-im 附件经其 BlobStore（client_credentials）——跨产品组合先例**（与 05 号证据 presign 直传呼应）。
- **aero-im**：通知渠道齐全（in-app/WS/email/sms/push/webhook/template/preference）；跨主机凭据项仍"待验证"（NAT/TURN、FCM/APNs、SMTP/OIDC/OTLP）。
- **iris-ui**：154 组件 × 4 框架、MCP/registry 生态、Alpha 阶段（sverpweb-stable 锁 0.2.x 已发布版本）。

**下游衔接**：与 01（现状）+ 05（部署后现状）合并后，为 kickoff 阶段 A–D 提供能力面证据——尤其 AuditSink 验证项、vault/im 组合先例（M3 迁移路径依据）、console 能力发现缺口（M3 遗留）。
