文档已产出：`docs/snaplink-platform/connections/aero-im.md`（169 行 ≥ 60 行要求；12 处 Verified、1 处 [PROPOSED] 标注）。

**A. 对外接口清单**（代码级 Verified，Rust crates/aero-server）：
- **认证面**：`POST /api/auth/login`（sverpweb 入口——自有账号登录换 token，60s 提前刷新，即 kickoff 所指"im app token"）
- **Rooms/Messages**：`/api/rooms`（ensureRoom 按 name 幂等创建）、`/api/rooms/:id/messages`（sendMessage 广播 blocks 文本 ≤2000 字符）——连接 4 已对接
- **DM 能力已存在**（sverpweb 未用）：`/api/dm/:participant_id` 幂等 open_dm + block guard
- **通知偏好/模板/推送已存在**：`/api/notifications/prefs` + DND + per-room notification-level + mute、`/api/templates`、push_tokens、`/api/me/unread-summary`

**B. 依赖**（关键发现）：
- **认证差异**：aero-im 是**自签发 JWT**（issuer "aero-im"，RS256 自持密钥）——连接 7 把它列为"JWKS 验签方"**需修正**；snaplink 关联仅为可选 OIDC 验签（三项全配才启用，默认关）+ SSO 登录页
- **vault BlobStore 已完整实现**：`aero_vault_blob_store.rs` client_credentials（snaplink token endpoint，client_id=aero-im-vault）+ 静态 bearer 备用——scope-matrix flow 1 的 Verified 锚点，`vault:blob:write/read` 授权模型（RFC 8693 vs identity-header）仍未定
- **审计**：内部 workspace audit trail + moderation 审计行存在；**无 governance connector**（verified gap）；但 outbox 基建可复用（event_outbox/bot_delivery_outbox）

**C. 四项改动**（各含理由/影响面/验收标准）：
1. **C-1 Outbox relay 实现**：`audit:event:ingest` 授予条件（flow 3 明确"grant conditional on relay"）——复用现有 outbox 模式 + 参照 vault 实现（租约/回扫/幂等）；moderation 事件优先分区
2. **C-2 定向通知**：DM 能力已就绪但**切换有成本**（per-participant 需 subject→participant 账号打通，当前无映射表）——建议默认维持广播，DM 切换 deferred 带触发条件
3. **C-3 通知偏好**：唯一权威源 = aero-im 偏好体系（与 aero-id 边界已定）；当前无铃铛需求不主动建设，仅防双写
4. **C-4 审计上报定案**：connector + app token 入注册表 + scope 全平台统一三件事必须一起落地，端到端验收（im 删除 → 账本 → console 时间线）

另附克制清单（不拉产品能力入组合协议、附件委托模型延续、block/mute/legal-hold 不向 sverpweb 暴露）。
