已通读 gate 报告、五份对抗审查（security/distributed/database/sre/qa/protocol）与三份草案（registry/scope-matrix/ingestion-design）。以下是契约修订规划。

---

# 判断框架（先于逐项结论）

**批准模型：目标态批准（approve-as-target-state）**。同意"契约可以先批准目标态、实现前置作为各仓库实施任务"，但有两个硬条件，否则批准动作本身失真：

1. **事实性声明必须随 v2 修正**——凡是与已验证代码现实矛盾的文字（console "consumer only (Verified)"、aero-id "account rows written in the account transaction"、"fastest rollout, pattern proven"、scope `audit:event:ingest`、`tenant_id (token)` 标 `[PROPOSED]`）都属于**契约修订项**，必须在本轮 v2 内落地；它们不是"实现前置"，推迟即批准错误事实。
2. **批准决议必须附带 rollout gate 清单**——S1/S2 为 P0 且是"任何真实流量"的前置；F3/F4/F5/F-01/F-06/F13/F-3 为各仓 P1/P2 前置并挂迁移骨架的 P1–P3 cutover 门禁。契约批准 ≠ 可部署，决议文本必须显式声明这一点。

---

## S1：实现前置（sink 仓库）+ 契约新增"生产信任姿态"条款

**契约修订内容**（ingestion-design 新增 §"生产信任姿态"，或并入 §0）：
- 规范化条款："平台唯一生产信任路径 = snaplink 签发的 JWT（JWKS 验证）；dev-token 认证是测试夹具——`AUDIT_ALLOW_DEV_AUTH` 默认 false、不得作为唯一信任源（dev-only 配置拒绝启动）、部署清单必须显式置 false 并接受 CI manifest 扫描。"
- Conformance 门禁条款：T-1.1（`AllowDev=false` 时 `dev:platform:platform-admin` → 401）、T-1.2（跨仓 e2e 仅用 IdP 签发 token）成为契约验收断言。
- registry 不需要改行（事件类型与 dev-auth 无关）；scope-matrix 不受影响。

**实现前置要求**（sink，P0）：
- `cmd/audit-api/main.go:45` flag 默认改 false；`ValidateConfiguration` 拒绝 `AllowDev` 唯一信任源；`deploy/docker-compose.verify.yml` 置 false；三个回归测试（config 拒绝 / 401 / manifest 扫描）。
- 验收断言：默认配置启动即无信任源时报错；`dev:*` token 全路由 401；扫描脚本在任一生产 manifest 出现 `AUDIT_ALLOW_DEV_AUTH=true` 时失败。

**是否阻塞本轮契约批准**：**不阻塞**（契约目标态与门禁条款随 v2 落地即完成契约层），但作为 P0 实现前置记录，且批准决议必须注明"生产部署/任何真实流量以 S1+S2 落地为前置"。

---

## S2/F-2：契约修订（token claim 契约 pin 住）+ 实现前置（snaplink 铸币）

**契约修订内容**：
- scope-matrix Flow 2 现有"introspection claim 契约"（subject/tenant/expires/scopes/active，禁 PII）**扩展为 Access-token claim 契约**：`kid`（header，必发）、`iss`（配置化 issuer，非 Host 派生）、`aud`（RFC 8707 resource/audience）、`scope`（注册 scope 集合，含 `audit:event:write`）、`client_id`/`azp`、**`tenant_id`**、**`roles`**（映射 sink 权限）。注明三个 issuer（ed25519/ecdsa/rsa）共用 `buildAccessPayload`，契约不因算法分叉。
- registry §1.4：`tenant_id (token)` 从 `[PROPOSED]` 升级为 pinned——"由 snaplink 在 access token `tenant_id` claim 中铸造，aero-* 服务从 token 读取"（关闭 open decision #5 的传播机制问题）。
- ingestion-design sink ingest contract auth 行改为引用该 claim 契约（而非一句 `client_credentials JWT`）。
- scope-matrix Flow 3 的 "publisher auth" 行同步：`snaplink:token:validate` 仅作为备选，主路径 = claim 契约验证。

**实现前置要求**（snaplink，P0）：
- `infrastructure/defaultimpl/issue_payload.go` `buildAccessPayload` 增加 `tenant_id` + `roles`（client_credentials 的 tenant 由 client 绑定解析）；`token/jwt/generator.go:52-55` header 补 `kid`；issuer 配置化。
- 验收断言（T-1.2/T-8）：`POST /token`（cc，`resource=audit-governance`）→ 断言 header 含 kid、claims 含 iss/scope/tenant_id/roles/client_id；用该 token（`AUDIT_ALLOW_DEV_AUTH=false`）→ sink `POST /api/v1/events` 202 `ledgered`、`GET /events` 200 带租户上下文、`POST /exports` 202。sink `parseJWT` 无需改动（已消费这些 claims）。

**是否阻塞本轮契约批准**：**不阻塞**——v2 正是用 claim 契约把两端钉死；实现前置为 P0，与 S1 同挂"真实流量"门禁。

---

## F2：纯契约修订（一字修复，三文档 + seed 校正）

**契约修订内容**（所有 `audit:event:ingest` → `audit:event:write`）：
- scope-matrix-draft：§0.1 seed 表 `audit:event:ingest`（来自 SCENARIO_ANALYSIS P0-3 的引用）、§0.2 约定示例、Flow 3 两行 scope 名、Flow 4 "Must NOT grant to console" 中的 `audit:event:ingest` 行（语义不变，改名）、cross-cutting rule 5 示例。
- ingestion-design：§1.2 delivery 行、sink ingest contract 的 Auth 行、aero-im "scope grant conditional" 句。
- registry：`audit.event.ingest` 是**事件类型名**（`audit.event.*` 命名空间），与 scope 名（`audit:event:*`）不同——**保留**该事件类型，但在 §1.1 或 provenance 加一行说明两个命名空间的区别，防止未来再次漂移。
- 采纳"改契约"而非"实现 alias"方案：所有实现已 enforce `audit:event:write`（sink `server.go` `require()`、aero-id `oauth.go` `validOAuthBinding`、vault `model.go` `RequiredScope`），契约侧一字修复成本最低，alias 方案会在 IdP scope registry（尚未存在）引入双名维护。

**实现前置要求**：无（实现侧已全部 enforce write）。只需重验证：按新矩阵配 token → 无 403；加一个 grep 型注册表一致性检查（`rules --check` 风格）防回归。

**是否阻塞本轮契约批准**：不阻塞——但这是 v2 的**必修项**（若 v2 不修正，批准即背书一个不可供应的 scope 契约，此时才构成阻塞）。

---

## F3/H1：实现前置（aero-vault）+ 契约将 §1.4/§1.5 升级为规范性条款

**契约修订内容**：
- ingestion-design §1.4 从 `[PROPOSED]` 升级为 normative："永久失败（422 schema/validation、409 内容冲突、tenant mismatch）→ 终态（dead）+ alert，批次继续；5xx/429/网络 → 有界重试（cap 300s）。"
- 新增 readiness 解耦条款（H1 的契约表达）："audit pipeline 健康不得门禁任何核心服务 readiness；backlog/poison 状态报告 `degraded` + maxLag×0.5 告警，绝不翻转 `/readyz` 503。"——这是"audit never blocks business"保证的可执行化。
- 验收断言：T-3（422 → 一个投递周期内进入终态，`OldestPendingAuditGovernance` 排除该行，`Ready()==true`）；D1 drill（sink 停 60min → vault `/readyz` 保持 200 + 450s 处告警）。

**实现前置要求**（aero-vault，P1）：
- `audit_governance_outbox`（migration 0039 扩展）加 status/dead 列 + 部分索引；`relay.go` `retryFact` 移植 sink outbox 的 `DeliveryError.Permanent` 分类（409/422/tenant mismatch → 终态）；`runtime.go` `Ready()` 与 backlog 解耦（`OldestPendingAuditGovernance` 排除终态行，maxLag 检查降级为告警）；补 relay metrics（H6，当前为 0）。
- 验收断言：T-3 测试（今天失败）转绿；`Ready()` 含一条 dead 行时为 true。

**是否阻塞本轮契约批准**：**不阻塞**（契约即目标态描述 + 断言）；实现前置为 vault P1，挂"vault relay 生产启用"门禁。

---

## F4：契约修订（§1.3 重写为稳定性契约）+ 实现前置（vault/aero-id event_id 稳定）

**契约修订内容**：
- §1.3 删除不可实现的 SHA-256 公式（没有任何实现采用），重写为**属性契约**（对齐 F-07）："event_id 是每个业务发生点首次写入时分配并固定、重试复用的确定性键；sink 以 `(source, event_id)` + 内容 digest 去重、`idempotency_key` 租户内唯一不可复用。**regeneration 规则**：gap-reconcile/重放/行清除后重建的 fact 必须复用原 event_id（实现锚点：vault `UNIQUE(origin_kind, origin_id)`、aero-id `UNIQUE(tenant_id, source, event_id)`），任何再生成路径产生新 ID 即违约。"
- 新增防 squatting 条款（S3/S12）："若采用可预测的确定性推导，trigger key 必须含不可猜测成分（服务端生成），或 sink 采用 claim-then-verify-digest 策略（同 event_id 不同 digest → 409，已实现）——二选一，契约不允许裸可预测 ID。"
- 附带修订：scope-matrix Flow 3 Ingest contract 的 idempotency 行引用新 §1.3；registry 不需要改行。

**实现前置要求**（aero-vault P1 / aero-id P1）：
- vault `facts.go` 三个构造点（`factFromEvent/factFromAudit/factFromGap`）由 `uuid.NewString()` 改为派生自 origin anchor；aero-id 无 EventID 的调用点（`service/account.go:155,184`、`audit_hold.go:59`、`legacy_cutover.go:88`）补稳定键。
- 验收断言：T-4（清除 outbox 行 → reconcile 再生成 → sink `Duplicate` 回执、`QueryEvents` 恰好 1 行）。

**是否阻塞本轮契约批准**：**不阻塞**——v2 直接修正 §1.3 即完成契约层（顺带消除了原设计引入的 squatting 面）；实现前置记录为两仓任务。

---

## F5：实现前置（aero-id）+ 契约路径清单修正（删除不实断言）

**契约修订内容**：
- ingestion-design aero-id 部分与 §1.1：删除"account rows written in the account transaction"的不实陈述（已验证 8 个 post-commit `_ = s.audit.Record(...)` 调用点），改为**路径清单表**：in-tx 路径（repository `AppendInTx`：account_profile/operation/sync_job/event_receipt/operation_audit）vs at-least-once-with-alert 路径（`service/account.go:155,184`、`callback.go:160,221,247`、`audit_hold.go:59,74`、`legacy_cutover.go:88`）。
- "never drop" 保证加可观测性子句："任何 post-commit 记录路径必须使失败可见（`ObserveAuditIngestFailure` metric + 告警），契约禁止静默丢弃错误。"
- 顺带修正 F6 的双投递问题（inline publish + durable worker 双路径）：契约写明 outbox worker 是唯一投递路径，inline publish 删除或降级为 best-effort。

**实现前置要求**（aero-id，P1）：
- 8 个调用点移入业务事务（`AppendInTx`）或改为失败可观测（counter + alert，绝不 `_ =`）；删 inline publish 或加独立短超时；补 401 → token 失效（F8，关联项）。
- 验收断言：T-5（mock `Record` 失败 → counter 递增或操作失败，无静默调用点）。

**是否阻塞本轮契约批准**：**不阻塞**（契约如实描述 + 可观测性条款）；实现前置为 aero-id P1，挂"aero-id relay 生产启用（F-03 激活门）"。

---

## F-01：实现前置（sink P0a）+ 契约容量/拓扑/持久化条款

**契约修订内容**：
- ingestion-design sink 部分新增条款：
  - **拓扑**：exactly-one-active-writer；多副本 = 冷备（共享 snapshot 行、乐观锁冲突不得以裸 5xx 逃逸——有界 jitter 重试或显式单写者声明）。
  - **持久化**：file backend fsync（或文档化丢失窗口）；`/readyz` 必须探测 store（当前只探 archive，PG 死了仍报 ready）。
  - **容量**：relational ledger（migration-001 的 `ledger_events` 等表）为生产目标态，**或**显式测量容量预算（`BenchmarkIngest` 记录 envelope）；高容量类（‡/L1）的 cutover 以容量决策为前置——registry open decision #2/#3（L1 阈值）与 #7（schema registry）的解决顺序依赖它。
- 验收断言：T-6（PG 并发 ingest 在 CI 跑、不 skip；等 digest → 一行 + 一 202 一 Duplicate；异 digest → 一 202 一 409）；停 PG → `/readyz` 503（今天 200）。

**实现前置要求**（sink，P0a）：
- relational backend（或 fsync + `ErrSnapshotConflict` 有界重试 + ingest 上限）；`statusForError` 映射冲突类错误；`/readyz` store ping；CI 加 PG job（`AUDIT_TEST_POSTGRES_DSN`）。

**是否阻塞本轮契约批准**：**不阻塞**（目标态明确：relational 或显式预算二选一）；实现前置为 sink P0a，挂"任何高容量类（auth.token.issue ‡ 等）cutover"门禁。

---

## F-06：契约修订（console 重标注 + `audit.event.read` 入注册表）+ 实现前置（sink read self-audit + console 接线）

**契约修订内容**：
- registry console 部分：删除/修正 "consumer only (Verified)" 声明 → "**local-only today (Verified)**：`audit_log_service.dart` 是浏览器 localStorage 1000-ring、不读任何 audit API；consumer 角色是目标态"。`console.audit.view`/`console.export.view` 两行**移出 console 事件集**：按设计（ingestion-design console 节），读自审计由 ledger 侧生成——在 ledger/governance 部分新增 **`audit.event.read`**（actor=调用者 admin/service、payload: query_filter/result_count/request_id、C1/High）与 **`audit.event.export`**（export 读取路径），console 不再声称生成它们；`console.capabilities.refresh`/`console.config.update` 等原生事件保留。
- ingestion-design console 节："cannot be bypassed by direct API calls" 保证改为 **sink P0 交付物声明** + 验收断言 T-12（query → 同一次 store update 内追加 `audit.event.read`；console 只显示服务端记录）。
- scope-matrix Flow 4：`audit:event:read` scope 的 permission 集补 `audit.event.read` 生成端说明（由 sink 在 serve 时写，非 console 写）。

**实现前置要求**（sink P0 / console 后置）：
- sink `QueryEvents`/`getEvent`/`downloadExport` 写 read self-audit（复用现有 `adminAction` 原子模式，仅 mutations 有，需扩展）；console `audit_log_service.dart` 改为调 sink 读 API（或先只做 sink 侧，console 接线列入 P3）。
- 验收断言：T-12（查询后出现调用者的 `audit.event.read` 行）；S9 场景（devtools 伪造/清空 localStorage 不再构成证据）。

**是否阻塞本轮契约批准**：**不阻塞**——v2 修订即修正错误声明并定义目标态事件；实现前置为 sink P0（与 F13 同仓同批）。

---

## F13：实现前置（sink self-audit writer）+ 契约支持拓扑声明

**契约修订内容**：
- ingestion-design sink 部分：self-audit writer 从 `[PROPOSED]` 升级为 **normative P0 条款**："治理操作（legal hold/export/retention/checkpoint/integrity verify）以完整信封的注册表事件（`audit.legalhold.apply`、`audit.export.*`、`audit.retention.policy.update`、`audit.ledger.checkpoint` 等）自审计，与操作同一 store update 写入；append 失败 → 操作中止（fail-closed）。"注明现状：mutation 有内部 `adminAction` 原子自审计（已验证），但未以注册表事件形态入账、read path 完全缺失——契约要求两者对齐。
- 新增**支持拓扑声明**："受支持入站路径 = HTTP ingest API + self-audit writer；sink 自带 outbox relay / Kafka consumer 未接线，v1 契约中标记 not-a-supported-ingest-path（防未来按错误拓扑集成）。"
- registry ledger/governance 各事件行注明"由 sink 自身写入（self-audit）"。

**实现前置要求**（sink，P0）：
- 治理操作 same-tx self-audit 事件写入；接线或移除孤儿 outbox/Kafka 组件。
- 验收断言：治理 append 失败 → 操作回滚（无部分 legal hold）；`audit.legalhold.apply` 等事件出现在 ledger 中。

**是否阻塞本轮契约批准**：**不阻塞**；实现前置为 sink P0，与 F-06 同批，挂"任何 producer cutover 之前"门禁（设计自身 P0）。

---

## F-3：实现前置（snaplink discovery）+ 契约 conformance 门禁

**契约修订内容**：
- scope-matrix Flow 2（或 ingestion-design 参考节）新增 **IdP endpoint contract**：`token_endpoint=/token`（`client_secret_post` 为唯一受支持方法，实现 Basic 前 metadata 不得广告 `client_secret_basic`/`private_key_jwt`）；`authorization_endpoint`/`response_types`/`authorization_code` 在 `/authorize` 实现前不得广告；`revocation_endpoint` 实现或从 metadata 删除；device grant 广告则必须含 `device_authorization_endpoint`；issuer 来自配置化 allowlist，禁止 Host header 派生。
- Conformance 门禁条款：discovery sweep 测试（每个广告端点实际请求 → 2xx/405，绝不 404）替代现有 `TestOIDCDiscovery`（当前是缺陷的回归锁）。

**实现前置要求**（snaplink，P1）：
- `routes.go:63-77,663-684` 修复 metadata；`routes_test.go:41` 重写；相关 F-4/F-5/F-6/F-8（token 端点绑定、introspection 认证、query-string 拒绝、device 认证）按协议审查一并处理。
- 验收断言：T-2 sweep 全绿；`metadata.token_endpoint == "/token"`。

**是否阻塞本轮契约批准**：**不阻塞**（契约加 conformance 门禁条款）；实现前置为 snaplink P1——注意它与审计契约是间接关联（vault browser flow / console OIDC 依赖 discovery），但 gate 列为阻断，故纳入 v2 门禁条款。

---

# 六步解锁路径完成矩阵

图例：**已覆盖** = 本规划已完整覆盖该步（含契约侧与无需代码改动的部分）；**部分** = 契约侧已覆盖、实现侧为各仓待办任务；**未覆盖** = 本规划未覆盖。

| # | 解锁步骤（gate 原文） | 契约侧（v2 修订） | 实现侧（仓库任务） | 状态 |
|---|---|---|---|---|
| 1 | **Trust path fix**（S1+S2+T-1） | 已覆盖：生产信任姿态条款 + Access-token claim 契约（kid/iss/aud/scope/client_id/tenant_id/roles）+ T-1.1/T-1.2 验收断言 | 未覆盖：sink flag 默认翻转 + config 拒绝（S1）；snaplink `buildAccessPayload`/`generator.go` 补 claims/kid/iss（S2）；跨仓 e2e（T-1.2） | **部分** |
| 2 | **Scope-name alignment**（F2） | **已覆盖**：三文档全部 `audit:event:ingest`→`audit:event:write`（scope-matrix §0.1/§0.2/Flow 3/Flow 4/rule 5、ingestion §1.2/§sink/§aero-im）+ registry 命名空间注释 + seed 校正 | 无需代码改动（实现已全部 enforce write）；仅需按新矩阵重验证 + grep 型注册表一致性检查 | **已覆盖** |
| 3 | **Vault dead state + readiness 解耦 + fact-ID determinism**（F3/H1/F4） | 已覆盖：§1.4 normative 终态分类 + readiness 解耦条款（degraded + maxLag×0.5 告警，绝不 503）+ §1.3 稳定性契约与防 squatting 条款 + T-3/T-4 断言 | 未覆盖：vault migration 0039 扩展 + `relay.go` Permanent 分类 + `Ready()` 解耦 + relay metrics（F3）；`facts.go` 确定性 ID（F4）；aero-id 3 个无 EventID 调用点 | **部分** |
| 4 | **Aero-id in-tx recording + 连接器显式激活**（F5/F-03） | 已覆盖：路径清单表（in-tx vs at-least-once-with-alert）+ "禁止静默丢弃"可观测性子句 + F-03 激活门（bindings 前置、boot fail-closed 即安全门） | 未覆盖：aero-id 8 个 post-commit 调用点（in-tx 或 alert）+ inline publish 收敛（F6）+ 401 失效（F8）+ 各仓连接器按迁移骨架显式启用（aero-id/vault flag、snaplink/aero-im 先建 relay） | **部分** |
| 5 | **Sink durable ledger + self-audit**（F-01/F-06/F13） | 已覆盖：容量/拓扑/持久化条款（relational 或显式预算、单写者、fsync、readyz store probe）+ `audit.event.read`/`audit.event.export` 入注册表 + self-audit writer 升为 normative P0 + 支持拓扑声明（outbox/Kafka 标记非受支持路径） | 未覆盖：sink relational backend（或 fsync+冲突重试+有界 ingest）+ read self-audit + 治理操作同事务自审计事件 + console 改读 sink API + PG CI job（T-6） | **部分** |
| 6 | **Discovery fix**（F-3/T-2） | 已覆盖：IdP endpoint contract（token_endpoint=/token、不广告未实现端点、issuer allowlist）+ discovery sweep conformance 门禁条款 | 未覆盖：snaplink `routes.go` metadata 修复 + `TestOIDCDiscovery` 重写 + F-4/F-5/F-6/F-8 端点绑定配套 | **部分** |

**结论**：契约侧 6/6 全部在本规划中覆盖（含 2 号步的完整落地）；实现侧 6/6 均为各仓待办任务（0 完成）。v2 契约可按目标态批准，批准决议附带：① S1/S2 = "任何真实流量" P0 门禁；② F3/F4/F5/F-01/F-06/F13/F-3 = 对应服务 cutover（迁移骨架 P1–P3）门禁；③ 生产部署文档以步骤 2（scope 对齐）为最先落地项（零代码成本）。
