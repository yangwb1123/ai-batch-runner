文档已产出：`docs/snaplink-platform/connections/aero-id.md`（200 行 ≥ 60 行要求；13 处 Verified、1 处 [PROPOSED] 标注）。

**A. 对外接口清单**（代码级 Verified，`/home/dwp/aero-id` 核查）：
- **HTTP 面**（8080，`/v1`+Auth）：自服务 `me` 系列（档案/来源/membership/通知偏好/生命周期导出抹除）、管理面（audit-log/roles/bans/accounts，`RequirePermission` 经 snaplink Authorizer.Check）、自审计账本（哈希链/verify/export/legal-hold）、事件摄入/操作/回调
- **gRPC 面**（9090，15 服务）：AccountService 等，门禁 = AuthInterceptor → 按方法权限注册表（`audit.read`/`system.config`/`source.event.ingest`）→ snaplink `Authorizer.Check`
- **消费方现状**：连接矩阵中**无 sverpweb/console → aero-id 的调用**——接口只服务自服务面，账户能力尚未被组合消费

**B. 依赖**（关键发现）：
- 认证**双路径已实现**：默认 gRPC `sso.v1.SSO/ValidateToken`（proto 在 **aero-id 仓库**，带 token cache/stale 300s/熔断器/重试全套韧性）——但 **snaplink 侧不存在该服务**；配置 `jwks_url` 即切本地验证（`RequireTenant: true`），二选一非双轨
- 审计：本地账本 append（幂等）→ **同步直报** governance `?wait_for=ledgered`，scope=`audit:event:write`（Verified 名 ≠ 提案名 `audit:event:ingest`）；`audit_governance.enabled: false` 未启用——**直报模式与 integration-design 的 outbox 目标态语义不匹配**
- 组织模型：**aero-id 无 organization/department 实体**（platform-map 归属声明与代码事实不符；sverpweb 组织目录实际在 vault）

**C. 四项改动**（各含理由/影响面/验收标准）：
1. **C-1 gRPC 校验定案**：实为三选一——默认 gRPC（需 snaplink 新增服务+契约归属定案）、本地 JWKS（推荐：零服务端改动）、mTLS（已否决）；撤回语义差异（本地验签看不到撤销）是决策核心
2. **C-2 审计直报 vs Outbox**：建议维持直报（本地账本幂等兜底+事件量小），补人工重放 runbook + DLQ 等价告警；启用前须完成 tenant bindings 覆盖校验
3. **C-3 组织模型**：建议**修正 platform-map**（organization 归 vault 现状事实），aero-id 不落地组织模型（克制、零重复）
4. **C-4 notification 端口**：aero-id 通知限定账户级事件、业务通知走 aero-im；偏好表归属二选一防双写

另附 5 项 platform-map 差异清单（含 `account:uid:resolve` 保留不开放）与克制清单。
