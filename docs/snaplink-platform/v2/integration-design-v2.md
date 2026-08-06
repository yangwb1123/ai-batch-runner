修订完成。完整修订版接入设计已写入 **`docs/snaplink-platform/v2/audit-ingestion-design-v2.md`**（37KB），v1 的 `audit-ingestion-design-draft.md` 保留不动。机械校验：五项强制修订全部覆盖，`audit:event:ingest` 仅剩修订日志 1 处（删除说明），6 个服务各有任务清单表，64 处 `[v2]` 标记。

## 五项强制修订的落实情况

**1. F3/H1 — aero-vault 毒事件/死信设计**（§1.4 升级为 normative + vault 节）
- 契约条款：永久失败（422/409/tenant mismatch/无效回执）→ **立即终态 dead**（≤1 次分类尝试）；瞬态（5xx/429/网络）→ 有界重试 cap 300s；**"retry-forever 是违约"**（vault 现状）；dead 行排除出 `OldestPendingAuditGovernance`。
- **Readiness 解耦条款**：audit backlog/poison 绝不翻转 `/readyz` 503——改为 `degraded` + **maxLag×0.5（450s）告警**，读路径降级为超时/变慢而非宕机信号。
- 任务清单：vault migration 0039 加 status/dead 列、`relay.go` 移植 `DeliveryError.Permanent` 分类、`runtime.go` Ready() 解耦、relay metrics（H6）；验收 = T-3 + D1 drill（sink 停 60min → `/readyz` 200）。

**2. F5 — aero-id 审计移入业务事务**（aero-id 节，normative）
- 新增**已验证路径清单表**：5 条 repository 路径已 in-tx（`AppendInTx`）vs **49 个 `_ =` 站点**（SEC-V2-7 实测：grep `internal/`+`service/`+`cmd/` @ aero-id HEAD `756b65b`，全部为 audit 触达行；其中 8 个 governance `_ = Record(...)` 静默丢失点列明：account.go:155,184 / callback.go:160,221,247 / audit_hold.go:59,74 / legacy_cutover.go:88）→ v2 要求全部移入事务或可观测失败，`_ =` 明令禁止（grep 门禁覆盖全量 49 站点，非仅 8 点）。
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
- 覆盖：snaplink（claim 契约/scope registry/discovery/连接器）、console（接 sink 读 API）、sink（8 项 P0：dev-auth/store/self-audit/read self-audit/拓扑/e2e/tenant 一致性 422）、aero-id（5 项）、aero-vault（5 项）、aero-im（3 项）。

**顺带一致性修订**（跨文档互引）：§1.3 幂等键重写为引用 registry v2 §1.9（确定性公式 + 去重键 + 再生规则 + 防 squatting + P2 前置过渡条款）；F2 scope 名全改 `audit:event:write`；F12 把 batch/backoff 降级为每服务运营参数（契约只定语义）；F13 自审计 writer 与受支持拓扑声明；F-06 console 重标注；F-3 discovery 修复任务入 snaplink 清单；§2 rollout 顺序挂上各任务门禁；open decision #5（tenant 传播）标记 RESOLVED，新增 #7（容量决策）、#8（连接器硬化验证）。

## 重审修正（gate FAIL 五项，原位修订）

**1. DS-08/SRE-07 — sink 不得覆写 envelope `tenant_id`（契约 422 对齐任务）**
- 明确任务：sink 在 `internal/service/service.go:389` 以 token/URL 解析 tenant **静默覆写** envelope `tenant_id`（`event.TenantID = tenantID`，DS-08/SRE-07 实测）——**禁止**；envelope `tenant_id` 与 token `tenant_id` claim 不一致 → **422 拒绝入账**（scope-matrix Flow 3 契约，冲突即拒绝、绝不重标）。
- 已入任务：sink 实现前置任务清单新增「tenant 一致性强制」行（验收 = T-13：envelope tenant-b + token tenant-a → 422、零入账）；implementation-gate 增列 sink B1-8。

**2. DS-09 — F13 拓扑声明覆盖活动 gRPC 入站**
- 现状缺口：契约声明 "HTTP ingest API + self-audit writer" 为唯一受支持入站，但 `cmd/audit-api/main.go` `--grpc-listen` 注册 `internal/grpcapi/server.go`（Write/WriteBatch/WriteStream 走同一 `Service.Ingest`）——活动入站未声明（DS-09）。
- 修订：gRPC ingest（`--grpc-listen`）**声明为受支持入站路径**，与 HTTP 同一认证/校验/self-audit/tenant 一致性约束；拓扑收敛任务相应改为 "gRPC 声明支持 + relay/Kafka 接线或移除（grep 门禁）"；B1-6 已同步。
- 验收：拓扑声明覆盖全部活动入站；契约外无活动入站路径（grep 无 enqueue）。

---

## v2.1 修正附注（contract-feedback FB-001/002/004/005，2026-08-06）

- **F5 计数**：上文 "49 个 `_ =` 站点" 的精确计数不可复现（SEC-V2-7 原始 grep 无存档）——v2.1 改为门禁语义 `make check-audit-swallow`（全仓禁止吞审计），实测基线 raw `_ =` = 142 站点、审计 Record = 25 站点（FB-001）；governance 调用点 8 → 9（补 `account_provision.go:168`，FB-002，属 6+1 写路径）。
- **B1-8 描述**：sink `service.go:397-399` 已判 mismatch→422（`:401` 仅空 envelope 盖章），非静默重标；剩余缺口 = OpenAPI 同步（仍写 "Body tenant_id is ignored"），并入 B1-6 契约声明（FB-004）。
- **kafka replay 孤儿**（`internal/kafka/replay.go:205`，未提交、`python3 cli.py quality` FAIL：go vet 格式串 %s 绑 int）：列为实施第一动作（修复或移除），接线决策进 B1-6（FB-005）。
