架构对抗审查已产出：`docs/snaplink-platform/connections/reviews/architect-review.md`

## 审查概要

**执行验证**（针对 `/home/dwp` 各仓库做了独立抽查，非仅转述阶段 3 文档）：
- ✅ `snaplink protocols/oauth/oauthvalidate/scope.go:50` 规则②——空 `AllowedScopes` = 不设限（F1 依据成立）
- ✅ snaplink `proto/` 无任何 Validate 服务，而 aero-id `api/proto/sso/v1/sso.proto` 定义了 `SSO/ValidateToken`——**死依赖确证**（F7）
- ✅ aero-vault `admin.go:456` 注册表关闭时隐式 admin（F9）
- ✅ aero-vault `presign.go` 签发时刻确有 `ActionDownload` ACL 检查——F13 修正为"撤销窗口"问题而非"签发无检查"
- ✅ snaplink `interfaces/admin/` 有 `recordAdmin*` helper 但 branding/backup/netpolicy/dr 等端点无 Record 调用（F6 方向成立）

**关键发现（17 条 + 2 条 Info）**：
- **Critical**：console 浏览器 admin token 直连（命中 `console-browser-bypasses-service-api` 禁止项，XSS = 全平台管理权）
- **High ×8**：scope 注册表缺失（fail-open 默认）、审计读账号跨租户放大、部门同步只增不减、撤销不级联 vault ACL、admin HTTP 审计缺口、aero-id 死依赖、schema 无审批链、vault 隐式 admin

**核心裁决**：gRPC 校验定案 = 本地 JWKS 首选（零新服务，≤1s 需求真实性是唯一例外触发条件）；审计启用顺序 = 定名→读写分离→启用→审批链→im relay；BFF 两 token 模型为控制面唯一正解。

**实施序列**：M0 配置止血（并行 fail-fast）→ M1 授权/审计完整性 → M2 控制面 BFF → M3 韧性契约。唯一破坏性变更（scope 注册表空 allowlist 语义）带盘点前置步骤。

**10 项待决未知项**：≤1s 需求真实性、审计 scope 命名、im token 模型、组织目录所有权、通知偏好权威、双人控制、DM 触发条件、BFF 部署归属、BFF 租户上下文、部门同步触发方式。
