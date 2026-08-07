证据已收到，完成归一（零 LLM 分析成本原则，事实原样保留，不新增推断）：

## 证据归一：`01-sverpweb-current-state.md`（sverpweb-stable 现状）

**provenance**：来源 `~/workspace/job/sverpweb-stable` 人工证据扫描（git log / README / apps/api/src 源码），全部条目 Verified。

**要点核对（供下游 aggregate/kickoff 引用）**：

- **技术栈**：pnpm workspaces + turbo；Hono BFF（Bun）+ Vue3/TSX/Pinia；iris-ui 0.2.x；SQLite + AES-256-GCM 加密会话（HttpOnly Cookie）。
- **已接入**：snaplink 认证（SSOClient / PKCE / 刷新轮换）、安全边界（权限通配符 / 菜单白名单映射）、**审计 Outbox 完整体系**（状态机 + lease + 批量发布，载荷 `{schemaVersion, source, sentAt, events}`）、三条业务路由（quality/APS/精准报价）。
- **Verified 缺失**：媒体本地存储未接 aero-vault、无 aero-im 客户端、无 aero-id profile/组织查询、`AUDIT_APP_URL` 未指向 governance（audit-admin 仅是本地 outbox 状态页）、无 console 能力发现嵌入。
- **工程门禁**：`pnpm check` = typecheck+test+build，有 vitest 单测；无 pi-batch 嵌入。

**下游衔接点**：证据 05（部署后现状）将补充 M0–M2 实现结果与遗留问题，两者合并后是 kickoff 阶段（第二阶段评估 A–D）的事实基础——尤其 §4 的密钥轮换、静态 token、DEMO_PASSWORD、console 审计缺口需逐条处置。

无需落盘（evidence 阶段任务无 output，本轮由流水线 runner 记录最终答复；归一产物由后续阶段按需读取原文件）。
