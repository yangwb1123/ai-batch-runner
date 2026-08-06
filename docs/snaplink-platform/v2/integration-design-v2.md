修订完成。完整修订版接入设计已写入 **`docs/snaplink-platform/v2/audit-ingestion-design-v2.md`**（37KB），v1 的 `audit-ingestion-design-draft.md` 保留不动。机械校验：五项强制修订全部覆盖，`audit:event:ingest` 仅剩修订日志 1 处（删除说明），6 个服务各有任务清单表，64 处 `[v2]` 标记。

## 五项强制修订的落实情况

**1. F3/H1 — aero-vault 毒事件/死信设计**（§1.4 升级为 normative + vault 节）
- 契约条款：永久失败（422/409/tenant mismatch/无效回执）→ **立即终态 dead**（≤1 次分类尝试）；瞬态（5xx/429/网络）→ 有界重试 cap 300s；**"retry-forever 是违约"**（vault 现状）；dead 行排除出 `OldestPendingAuditGovernance`。
- **Readiness 解耦条款**：audit backlog/poison 绝不翻转 `/readyz` 503——改为 `degraded` + **maxLag×0.5（450s）告警**，读路径降级为超时/变慢而非宕机信号。
- 任务清单：vault migration 0039 加 status/dead 列、`relay.go` 移植 `DeliveryError.Permanent` 分类、`runtime.go` Ready() 解耦、relay metrics（H6）；验收 = T-3 + D1 drill（sink 停 60min → `/readyz` 200）。

**2. F5 — aero-id 审计移入业务事务**（aero-id 节，normative）
- 新增**已验证路径清单表**：5 条 repository 路径已 in-tx（`AppendInTx`）vs **8 个 post-commit `_ = Record(...)` 静默丢失点**（account.go:155,184 / callback.go:160,221,247 / audit_hold.go:59,74 / legacy_cutover.go:88）→ v2 要求全部移入事务或可观测失败，`_ =` 明令禁止。
- 连带：单一投递路径（删 inline publish，F6）、401 → token 失效（F8）、3 个无 EventID 调用点补稳定键（F4）、死信重放工具（F9）；激活门（F-03）标注"fastest rollout 是 code-true、deployment-false"。
- 验收 = T-5（mock Record 失败 → counter 递增）。

**3. F-01 — sink 快照存储乐观锁冲突重试**（sink 节 + §1.5 新行）
- `ErrSnapshotConflict` → 有界 jitter 重试 + `statusForError` 映射域错误（禁止裸 500 逃逸）；file backend fsync（或文档化丢失窗口）；`/readyz` 必须探测 store（今天只探 archive，PG 死了仍 200）；relational ledger（migration-001）或显式容量预算二选一，高容量类 cutover 以此门禁。
- 验收 = T-6（PG 并发 ingest 无 500）+ 停 PG → readyz 503 drill。

**4. S1 — dev-token 默认关闭 + IdP 信任路径**（新增 §1.7 生产信任姿态）
- `AUDIT_ALLOW_DEV_AUTH` 默认 false；dev-only 配置拒绝启动；部署清单 CI 扫描；唯一生产信任路径 = IdP 签发 JWT（JWKS 硬化验证器），token 必须携带 claim 契约（kid/iss/aud/scope/client_id/azp/**tenant_id**/**roles**）；缺 tenant_id/roles 拒绝。
- 验收 = T-1.1（dev token → 401）+ T-1.2（跨仓 e2e：`/token` → ingest 202 ledgered → read 200 → export 202）。

**5. 每服务实现前置任务清单**（6 个 `### 实现前置任务清单` 表）
- 每表三列：任务 | 仓库/位置（含文件路径） | 验收断言（映射审查测试编号 T-1.1/T-2/T-3/T-4/T-5/T-6/T-8/T-10/T-11/T-12 与 drill D1）。
- 覆盖：snaplink（claim 契约/scope registry/discovery/连接器）、console（接 sink 读 API）、sink（7 项 P0：dev-auth/store/self-audit/read self-audit/拓扑/e2e）、aero-id（5 项）、aero-vault（5 项）、aero-im（3 项）。

**顺带一致性修订**（跨文档互引）：§1.3 幂等键重写为引用 registry v2 §1.9（确定性公式 + 去重键 + 再生规则 + 防 squatting + P2 前置过渡条款）；F2 scope 名全改 `audit:event:write`；F12 把 batch/backoff 降级为每服务运营参数（契约只定语义）；F13 自审计 writer 与受支持拓扑声明；F-06 console 重标注；F-3 discovery 修复任务入 snaplink 清单；§2 rollout 顺序挂上各任务门禁；open decision #5（tenant 传播）标记 RESOLVED，新增 #7（容量决策）、#8（连接器硬化验证）。
