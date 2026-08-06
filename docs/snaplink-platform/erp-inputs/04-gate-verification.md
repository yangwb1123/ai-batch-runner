# 裁决门阻塞项的人工复核（Verified，2026-08-06 人工证据复核）

gate 阶段 VERDICT: FAIL 后，按信任边界对 5 项阻塞逐条人工复核。结论：
**B1/B2/B3/B4/B5 全部 Verified**，kickoff 的 P0"设置 env 即激活、零代码"论断
**被证据推翻**。本文件是修订阶段的事实依据。

## B1 契约不兼容 — Verified

- 发送端：sverpweb `apps/api/src/audit/client.ts` 默认
  `ingestPath = "/api/v1/audit/events"`（env.ts 同）；载荷
  `{schemaVersion, source, sentAt, events}`，事件字段为
  eventType/action/aggregateType/aggregateId/operation/occurredAt/...
- 接收端：audit-governance `internal/httpapi/server.go:77`
  `mux.HandleFunc("POST /api/v1/events:batch", s.postBatch)`；
  `internal/service/service.go:306` 对每个事件调 `ValidateBasic()`。
- `internal/domain/models.go:360 ValidateBasic` 必填 9 字段：
  event_id / source_system / event_type / schema_id / action / outcome /
  data_classification / retention_class / idempotency_key，外加
  schema_version>0、occurred_at、actor.id、payload 或 payload_ref。
- 结论：字段命名与取值语义均不匹配；直接设 `AUDIT_APP_URL` 必然整批 400，
  outbox 永久 failed 积压。**P0 必须改为：契约对拍（字段映射/适配器）+ 先
  schema 注册，再指 URL。**

## B2 actor 丢失 — Verified

- `contracts.ts:171` `withActor(context: OperationContext, ...)` 只接受
  OperationContext。
- `request-middleware.ts:115-116`：`withActor(newAuditEvent({...}), ...)` —
  第一参传的是 AuditEvent，TS 直接报
  `error TS2345: Argument of type 'AuditEvent' is not assignable to parameter
  of type 'OperationContext'` → request 级事件 actorUserId 恒 null。
- 对照：`emitAuditEvent`（域级路径，request-middleware.ts:172）写法正确：
  先 `withActor(operation, ...)` 再 `newAuditEvent({...input, operation})`。
- 同文件还有 :131 `actorDepartmentId` 不存在于 AuditEventInput、:139
  OperationContext 不可赋给 AuditEvent 等关联错误。

## B3 测试与门禁 — Verified（部分）

- `apps/api/src/audit/` 只有 client/request-middleware/sqlite-outbox 三个
  .test.ts，**无 forwarding.test.ts** ✓
- `bunx tsc --noEmit` 实测 **31 个 TS 错误**（含 audit/ 4 个 + system-users/
  @snaplink/sso-client 声明解析失败等）→ `pnpm check`（typecheck）当前红。
- 补充证据：sso-client 的 package.json "exports" 未含 types 入口，导致
  TS7016（声明文件解析失败）——这是 sverpweb 侧工作区 file: 依赖的问题。

## B4 生产零审计事实 — Verified（附 nuance）

- `data/app.sqlite` 实表清单**无 audit_outbox 表**（.tables 仅有
  system_route_definition_audit 等）。audit_outbox 由 sqlite-outbox.ts
  `CREATE TABLE IF NOT EXISTS` 惰性创建——表不存在说明该库从未初始化过
  outbox/从未产生审计事件。
- nuance：不是"无 outbox 能力"，而是"当前运行实例无审计事实"；审计中间件
  是否挂载于 app.ts 需实现期确认。

## B5 无 git 仓库 — Verified

- `~sverpweb-stable/.git` 是**空目录**（无 HEAD/objects/refs）。
- 影响：pi-batch `git_commit`、campaign worktree 隔离、指纹复用全部前提缺失；
  M4"嵌入 pi-batch"前置必须是 `git init` + 首次提交 + CI 门禁。

## 修订要求（gate 裁决原文摘录，须逐项落实）

1. kickoff 回写 P0 修正（适配器 + schema 注册，可逆回退 L1）与证据降级
   （01/02 中"设置 env 即激活"类表述作废）。
2. 下一 gate 硬门槛：M0→M1 门禁转绿（tsc 0 错 + 测试）+ M2 E2E 全绿。
3. 8 项 owner 决策附责任人。
4. git/CI 纳入 M4 前置。
5. 采纳项保留：B1 的 4xx permanent 分类、B3 的 idempotency_key=event.id
   （409 语义陷阱）、B2 精确修复 + 先红后绿测试、H2 降级、媒体 presign
   直传禁双写。
