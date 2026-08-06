后端工程评审已完成，交付物：`docs/snaplink-platform/erp/reviews/backend_engineer.md`。全部关键论断经源码与命令实测核验（非转述）：

## 实测核验
- **ERP**：`tsc --noEmit` 复现 31 错误（含 `request-middleware.ts` 3 处，即 F2 类型证据）；`bun test apps/api/src/audit/` 7 pass 且确认 **forwarding 激活路径 0 测试**（F3）
- **governance**：`go test ./...` **全绿**——修正 QA F4："governance 仓库本机不存在"与事实不符（位于 `/home/dwp/snaplink-audit-governance`），跨仓契约测试现在即可执行，M0 从"待建基础设施"降为半天工作量

## 本评审新增的三个实现级约束（architect/qa 未覆盖）
1. **B1 [High] 永久性失败无分类**：4xx 拒收与断网同走指数退避无限重试（封顶 5min），H1 误配置场景 = 全 outbox 永久重试风暴 → 4xx 分类为 permanent + 远期暂停 + `[permanent]` 标记，不新增状态（避免表重建迁移）
2. **B3 [Medium] idempotency_key 映射陷阱**：governance 对"同 key 异 event_id"是 **Conflict 409 而非 Duplicate**（Verified `service.go:371-379`）→ `idempotency_key = event.id`，**不得**映射 Idempotency-Key 头，否则业务重试第二次必被永久拒收
3. **§3.7 hasField 注册陷阱**：`hasField` 只特判 6 个字段名，required 里写 `aggregate_type` 会恒失败；每 event_type 一条 schema（约 22 条，已 grep 枚举）

## 其余交付
- B2（F2）实现级定位与精确修复代码：`withActor` 应作用于 `operation` 而非事件对象；`emitAuditEvent` 路径正确不动；配套先红后绿测试
- 完整字段映射表（20+ 字段，含 outcome 提升、actor 显式未知标记、tenant 由 claims 盖章不发）
- drainer（默认 60s > lease 30s，lease+lockToken 论证多实例安全）、坏行容错、schema 注册顺序 runbook（tenant→source→schema→token→env 五步）
- Persistence Design 报告（§12 强制关卡）：本阶段无新表无迁移，对既有 outbox 表逐项复核
- 双侧契约测试设计（ERP golden 映射单测 + governance `test/erp_contract_test.go` 钉死 B3 语义）

**结论**：P0 收敛为一个 transport 适配器（~150 行）+ 3 处小改 + 22 条 schema 注册 + 双侧契约测试；前置条件与 QA 退出标准 1-3 一致（B2 修复 → 门禁转绿 → forwarding 测试）。本评审为咨询性意见，不批准任何仓库门禁豁免。
