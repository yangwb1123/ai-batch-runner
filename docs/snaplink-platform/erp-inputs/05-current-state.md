# 第二阶段证据：平台当前状态（Verified，2026-08-06 部署/实现后）

来源：8 轮实现工作实录（部署 192.168.123.52、M0-M2 落地、集成页、测试体系）。

## 1. 部署现状（全部 systemctl，192.168.123.52 / svmisdev / Debian 12）

| 服务 | 端口 | 形态 | 状态 |
|---|---|---|---|
| snaplink-sverp | :13015/:13016 | sso-server minimal（sqlite+JWT） | active，密钥重启即轮换 |
| snaplink-console | :13017 | nginx（Flutter Web） | active |
| sverpweb-stable | :13014 | Hono BFF + Vue3/iris-ui | active，新 dist 已部署 |
| audit-governance | :13018 | 文件账本单节点 + snaplink Ed25519 公钥 | active，dev-auth 关闭 |
| aero-vault | :13019 | sqlite + local + Snaplink JWKS | active（含 nil-panic 修复） |
| aero-id | :13020/:13021 | PG + local + snaplink gRPC | active |
| aero-im | :13022 | PG + Redis + NATS JetStream | active |
| nats | :13023 | JetStream | active |

基础设施：PostgreSQL 15（aero_id/aero_im 库）、Redis、pgvector（源码编译）、
NATS v2.14.4（ghproxy 下载）、公钥同步 timer（sync-sso-key，2min 轮询）。

## 2. 已完成实现（Verified 实测）

- **M1 门禁**：sverpweb tsc 31→0（B2 withActor 修复 + sso-client 声明解析）、
  forwarding 测试 8 用例、permanent 列 + 毒丸隔离 + 逐事件 receipts（§2.2）
- **M0 契约对拍**：governance erp_contract_test.go 5 测试（三分支 + 字段清单）
- **M2 审计 L2 接线**：sverp-audit 服务账号（client_credentials +
  audit:event:write/read/policy/platform）、schema/source 注册、E2E 全通
  （质检单创建 → outbox → 适配器 → 账本 actor=admin，无重复）
- **token 自动轮换**：OAuthClientCredentialsTokenProvider（exp 缓存 + 提前刷新
  + 401 强制重试）——sverpweb 审计/vault 集成均用
- **集成测试页**（ERP 系统管理 → 平台集成测试）：三服务健康、vault presign
  直传/下载/删除、IM 收发、审计账本查询——全部实测通过
- **IM 业务通知**（M5 最小版）：质检单创建 → aero-im 频道自动消息（best_effort）
- **测试体系**：test-platform.sh status(25 项)/audit(8 项)/e2e(8 项) 全绿；
  keys.txt 凭据文件（0600）；公钥自动同步
- **账号体系**：admin（SSO 管理 + ERP 全权限）、tester（质检员最小权限 5 项）

## 3. 组合资产（snaplink 侧）

- 服务账号：sso-admin（admin:* + audit 读/写/policy + platform）、
  sverp-audit（audit:* + platform）、sverp-vault（read/write + client→tenant 映射）
- sverp-web client（ERP OIDC 授权码 + PKCE + admin 用户权限分配）
- 角色：sso-admin、sverp-web admin（全权限）、sverp-web quality（质检最小集）
- 菜单：permissions_menus 已加平台集成测试组（/system/integrations）

## 4. 已知问题与遗留（Proposed/Verified）

- **M3 遗留**：snaplink-console 审计页仍为本地记录，未接 governance 账本
  （统一审计时间线需 Flutter 侧改动）
- **密钥轮换**：snaplink 每次重启轮换签名密钥（rotation.enabled=false 仍轮换）
  ——静态 PEM 靠 timer 同步，长期应上 HTTPS + JWKS 动态
- **M5 扩展**：通知仅质检创建 1 个触发点；审批/异常/任务分派未接
- **vault 生产数据**：ERP 媒体仍本地 SQLite（M3 迁移未做：懒迁移+回填+双读禁双写）
- **aero-id 组织**：org/department 投影未接（H2 降级中）
- **静态 token 残留**：sverpweb .env 仍有 AUDIT_APP_TOKEN（OAuth 优先，可清理）
- **安全**：sso-admin/sverp-vault secret 在服务器 keys.txt（0600）；demo 模式
  .env 仍有 DEMO_PASSWORD 死配置（N2 待办）；snaplink-console 曾 inactive 一次
- **测试账号**：admin/tester 密码在服务器 accounts.txt；生产应轮换
