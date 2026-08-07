证据已收到，完成归一（零 LLM 分析成本，事实原样保留）：

## 证据归一：`04-gate-verification.md`（裁决门阻塞项人工复核）

**provenance**：2026-08-06 人工证据复核（gate FAIL 后按信任边界逐条核验），B1–B5 全部 Verified，是第一轮 revision 阶段的事实依据。

**要点核对（供下游 aggregate/kickoff 引用）**：

- **B1 契约不兼容（Verified）**：发送端 `/api/v1/audit/events` + `{schemaVersion, source, sentAt, events}` 载荷 vs 接收端 `POST /api/v1/events:batch` + `ValidateBasic` 必填 9 字段（event_id/source_system/event_type/schema_id/action/outcome/data_classification/retention_class/idempotency_key）——字段命名与取值语义均不匹配，直接指 URL 必然整批 400、outbox 永久 failed。**P0 改为：契约对拍（适配器）+ 先 schema 注册再指 URL。**
- **B2 actor 丢失（Verified）**：`request-middleware.ts:115` 把 AuditEvent 传给只接受 OperationContext 的 `withActor`（TS2345）→ request 级事件 actorUserId 恒 null；:131/:139 关联类型错误；对照 :172 域级 `emitAuditEvent` 写法正确。
- **B3 测试与门禁（Verified 部分）**：无 forwarding.test.ts；`bunx tsc --noEmit` 实测 31 错（含 sso-client 声明解析 TS7016，工作区 file: 依赖问题）→ `pnpm check` 红。
- **B4 生产零审计事实（Verified 带 nuance）**：`data/app.sqlite` 无 audit_outbox 表（惰性创建从未触发）——不是"无 outbox 能力"而是"当前运行实例无审计事实"；中间件是否挂载需实现期确认。
- **B5 无 git 仓库（Verified）**：`.git` 为空目录（无 HEAD/objects/refs）→ M4 嵌入 pi-batch 前置必须 `git init` + 首次提交 + CI。

**修订要求 5 条**（kickoff 回写 P0 修正与证据降级；M0→M1 门禁转绿硬门槛；8 项 owner 决策附责任人；git/CI 纳入 M4 前置；采纳项保留——4xx permanent 分类、idempotency_key=event.id 409 语义、先红后绿、H2 降级、presign 直传禁双写）。

**下游衔接**：本文件是第一轮 revision 的强制约束清单；对第二阶段评估而言，其中 B1–B5 已由 8 轮实现工作解决（证据 05：M0 契约对拍 5 测试、M1 门禁转绿 tsc 31→0、M2 E2E 全通），kickoff 引用时应以 05 为当前事实、04 为历史裁决依据。
