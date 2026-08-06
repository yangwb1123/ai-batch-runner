修订完成。完整修订版注册表已写入 **`docs/snaplink-platform/v2/audit-event-registry-v2.md`**（42KB，可人工核对），未修改任何代码。事件类型集合经机械化 diff 校验：**v1 121 = v2 121**（−2 行 +2 行，其余 119 行原样保留）。

## 五项强制修订的落实情况

**1. S2 — schema 必填字段**（§1.2/§1.3/§1.4，标记 `[v2]`）
- 信封规范新增"Required fields"：`tenant_id` 必填（唯一豁免 = `— (platform)` 系统级事件），无租户事件 ingest 422 fail-closed；`actor.roles` 必填（来自 access-token `roles` claim，`system:` 角色恒为 `["system"]`，非系统 actor 空 roles 拒绝）。
- `tenant_id (token)` 从 `[PROPOSED]` 升级为 pinned（access-token `tenant_id` claim），open question #5 标记 **RESOLVED**。
- §0.2 新增溯源锚：Access-token claim 契约（kid/iss/aud/scope/client_id/azp/tenant_id/roles）。

**2. F4 — 确定性 event_id + 去重键显式化**（新增 §1.9）
- 公式按指定方案落实：`event_id = SHA-256(source_service | tenant_id | event_type | resource_id | time_bucket)[:32]`，五个输入逐一定义（resource_id 给出各域的稳定锚点表）。
- Distinct-occurrence 规则：同桶同资源多次发生（如同一文件两次下载）必须扩展 per-occurrence 分量。
- 去重键四层显式化：producer outbox `(service, event_id)` / aero-id `(tenant_id, source, event_id)` / vault `(origin_kind, origin_id)`（再生锚）/ sink `(source_service, tenant_id, event_id)` + `idempotency_key`（同 digest→202，异 digest→409）。
- 再生规则（gap-reconcile/重放必须复用原 event_id，新 ID 即违约）+ 防 squatting（time_bucket 不可预测 + sink claim-then-verify 后盾）+ 过渡条款（随机 UUID 仅覆盖同行重试，确定性 ID 为 P2 cutover 前置）。

**3. F-06/F13 — 读路径审计与自审计事件**（console § + sink §）
- console 部分重标注："local-only today (Verified)"，删除 `console.audit.view`/`console.export.view` 两行。
- sink ledger 新增 **`audit.event.read`**（QueryEvents/getEvent 同 store update 追加，actor=调用者，C1/High）与 **`audit.event.export`**（downloadExport 读审计）。
- F13：governance 表整体加注"由 sink self-audit writer 同事务写入，append 失败 → 操作中止（fail-closed）"；新增支持拓扑声明（outbox relay/Kafka consumer = 非受支持路径）。

**4. F-3 — OIDC discovery 只列已验证事实**（新增 §1.10）
- 九行事实表：Verified（`/token` cc 铸币、JWKS 良构但缺 kid）×2，**False** ×7（token_endpoint=/authenticate、无 /authorize、/revoke 404、/validate 非标准、两种广告认证方法未实现、无 device_authorization_endpoint、Host 派生 issuer）。
- 后果条款：`user:<uid>` OIDC auth-code 路径与 `auth_flow=oidc` 事件标记 `[PROPOSED]`（auth.login.success 行已加注）；`tenant_id (session)` 仅对 password/session 流保持 Verified。

**5. 溯源格式与 v2 标记**
- 保留 Verified/[PROPOSED] 格式；§0.1 新增 v2 修订日志（阻断项 → 变更 → 位置映射表，含 F2 命名空间说明与 v1 计数纠错）；41 处 `[v2]` 内联标记；§2 覆盖清单更新（console 5/5、sink 15、admin-path 总数 25→23，总数 121 不变）。

## 机械校验结果

```
v1 独有（预期 2）: console.audit.view, console.export.view   ← 已移除
v2 新增（预期 2）: audit.event.read, audit.event.export      ← 已加入
每服务计数: snaplink 45 | console 5 | sink 15 | aero-id 14 | aero-vault 20 | aero-im 22 = 121
```

修订中另将 v1 §2 的每服务计数笔误（snaplink 46 / sink 12 对调，实际 45/13）在修订日志与清单中显式纠正。剩余待办：open question #8（time_bucket 粒度与 L1 阈值联动）与 #9（roles claim 内容，依赖 IdP scope registry P0-3）保留为待决策项。
