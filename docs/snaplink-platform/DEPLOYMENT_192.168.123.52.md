# 平台部署实录 — 192.168.123.52 (svmisdev, Debian 12)

> 部署日期：2026-08-06。目标：六件套 + ERP 全部 systemctl、单独二进制、最小化模块。
> 服务器：Debian 12 bookworm, x86_64, 15Gi RAM（已用 ~10Gi），sudo 免密（ywb）。

## 1. 部署前现状（2026-08-06 扫描）

| 项目 | 状态 | 说明 |
|---|---|---|
| snaplink (sso-server) | ✅ 已部署 | `snaplink-sverp.service`，minimal（sqlite+JWT），:13015 HTTP / :13016 gRPC |
| snaplink-console | ✅ 已部署 | `snaplink-console.service`（nginx），:13017 |
| sverpweb-stable | ✅ 已部署 | `sverpweb-stable.service`（bun），:13014 |
| aero-id / aero-vault / aero-im / audit-governance | ❌ 缺失 | 本轮补齐 |
| 基础设施 | ✅ | PostgreSQL 15 / Redis / MariaDB / MongoDB / etcd / Kafka / MinIO / Docker / nginx+openresty |

## 2. 本轮部署（4 服务 + 1 基础设施，全部 systemctl + 加固）

部署结构沿用 capistrano 风格：`/home/ywb/apps/<project>/{current,shared}`，
systemd 单元统一加固（NoNewPrivileges / PrivateTmp / ProtectSystem=strict /
ProtectHome=read-only / RestrictSUIDSGID / LockPersonality / ReadWritePaths 仅 data）。

| 服务 | 端口 | 二进制 | 最小化形态 | 依赖 |
|---|---|---|---|---|
| `audit-governance.service` | :13018 | audit-api 22M | 文件账本单节点（state.json），**AUDIT_ALLOW_DEV_AUTH=false** + snaplink Ed25519 公钥验证（JWKS 需 HTTPS，故用公钥 PEM，jwx 不支持 JWK JSON，经 run.sh 注入多行 PEM） | 无外部依赖 |
| `aero-vault.service` | :13019 | aero-vault-server 36M | sqlite + local 存储 + **Snaplink JWKS 认证**（SDK JWKSCache） | 无 |
| `aero-id.service` | :13020/:13021 | aero-id-server 45M | PG + local 存储 + snaplink gRPC 校验（:13016）；auto_migrate 首次开启建表 | PG 库 aero_id |
| `aero-im.service` | :13022 | aero-server 52M (Rust release) | IM/通知单体 + 自有 JWT（RSA-2048）；OTLP 关闭（otlp_endpoint 必须缺省，空串会启动 exporter） | PG 库 aero_im + Redis + NATS |
| `nats.service` | :13023 | nats-server v2.14.4 | JetStream（aero-im 总线） | — |

配套操作：
- **pgvector 编译安装**（Debian 仓库无包）：apt server-dev-15 + 源码 make install → `CREATE EXTENSION vector`（aero-im 迁移 1 强制）
- PG 建库：`aero_id` / `aero_im`（owner aero，密码 aero_local_dev_only）
- NATS 二进制经 ghproxy 镜像下载（github release 直连超时）
- aero-im 配置读取：WorkingDirectory=shared/（`config.toml` 相对路径，无 --config 参数）

## 3. 验证结果

- 8 个 systemd 全部 `active`：snaplink-sverp / snaplink-console / sverpweb-stable / sverpweb /
  audit-governance / aero-vault / aero-id / aero-im / nats
- 端口 13013–13023 全监听；`audit-governance /api/v1/events` 无 token → **401**（dev-auth 已关）
- aero-vault /healthz `{"ok":true}`；aero-id /healthz+readyz 200
- aero-id 迁移建表（account_identities/profiles/memberships…）；aero-im 迁移全跑（含 vector）

## 4. 密钥与安全

- audit-governance：`AUDIT_SIGNING_SECRET`/`AUDIT_ENCRYPTION_KEY` 随机生成（.env 0600）；snaplink 公钥 PEM 存 shared/data（0600），run.sh 注入
- aero-im：JWT RSA-2048 keypair（secrets/，config.toml 内嵌，shared/ 0600 建议）
- aero-id：audit_ledger signing_key 随机（顶层 `signing_key` 必填，validate.go）
- 数据库口令均为本机内网 dev 级，生产需轮换

## 5. 遗留（M2 接线）

1. snaplink 侧创建 client_credentials 服务账号（scope `audit:event:write` 等）→
   sverpweb `AUDIT_APP_URL=http://192.168.123.52:13018` + `AUDIT_INGEST_PATH=/api/v1/events:batch`
   + `AUDIT_APP_TOKEN=<snaplink token>`（审计 L2 试运行）
2. aero-vault 目前仅 Snaplink verifier——需 snaplink token（含 ten claim）方可使用
3. aero-im 通知渠道（SMTP/push）未配置（minimal 不含）
4. 内存预算：新增 4 服务 GOMEMLIMIT 256–384MiB 有界，可用 ~4.5Gi，可容纳

## 6. M2 接线完成（2026-08-06 更新，全链路 E2E 验证）

**审计 L2 试运行已打通**：ERP 业务操作（质检单创建）→ emitAuditEvent → outbox
→ 契约适配器（governance-transport.ts）→ snaplink token 认证 → governance 账本
（actor=admin 真实用户，账本恰好 1 条无重复）。

### 完成的接线

- snaplink 侧：`sso-admin` 客户端启用 client_credentials + 重置 secret + sso-admin
  角色分配（admin API 403 排查：授权查 user 权限而非 token scope，需
  permissions_assignments + permissions_roles 双表按 client_id 精确匹配）；
  新建 `sverp-audit` 客户端（client_credentials + audit:event:write +
  audit:policy:write）
- audit-governance：`AUDIT_ALLOW_DEV_AUTH=false` + snaplink Ed25519 公钥（密钥轮换
  后重新导出——kid 9WQoSnawsm4 → gwvKK02OO-g）；注册 tenant=sverp、schema
  sverp.purchase_request.created.v1 / sverp.quality_inspection.created.v1、
  source sverp-web ↔ sverp-audit 绑定
- sverpweb-stable：AUDIT_APP_URL/INGEST_PATH/SOURCE/APP_TOKEN 配置；部署新 dist
  （含 §2.2 终态机制 + 适配器）；E2E 验证三分支语义 + 幂等 + 噪音过滤
  （http.request.* 跳过投递）

### 关键修复（E2E 中发现）

- classifyByReceipts 交叉校验曾用映射后对象取 id（undefined）→ 误判 event_id
  mismatch → 改为原始 AuditEvent.id 列表（commit 0e9ac9d）
- sverpweb 服务器 dist 曾为 8/4 旧构建（无审计机制）→ 重新部署

### 遗留

- sverpweb AUDIT_APP_TOKEN 为静态 token（1h 过期）→ 需 client_credentials
  自动换 token（长期改进）；当前过期会 401 → retryable（幂等安全网兜底）
- console 审计页接入（M3）；aero-im 通知（M5）未做
- 生产注意：sso-admin/sverp-audit secret、admin 密码已重置并记录于部署者本地
  （/tmp/deploy-build/），应轮换并移入密钥管理
