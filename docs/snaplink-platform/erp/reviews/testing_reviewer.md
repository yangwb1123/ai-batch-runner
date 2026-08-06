All verification complete. Here is my QA lead review.

---

# QA Lead Review — sverpweb-stable 审计子系统与接入提案（P0 审计 L2 路径）

**审查对象**：`~/workspace/job/sverpweb-stable`（apps/api/src/audit/* 实现 + 测试）与 kickoff/gate 交付链。默认立场：激活路径在它最可能失败的方式上（契约 400、actor 丢失、flush 竞态、部分成功、超时）**未证明可用之前视为未测试**。

## 0. 本次修订实际运行的命令（非转述）

| 命令（cwd） | 结果 |
|---|---|
| `bunx tsc --noEmit`（apps/api） | ❌ **31 个 TS 错误**（含 audit/ 4 个：request-middleware.ts:116 TS2345、:131 TS2353、:139 TS2345、sqlite-outbox.test.ts:122 TS18048）|
| `bun test`（apps/api 全量） | ✅ 133 pass / **1 skip** / 0 fail / 976 expects |
| `SNAPLINK_E2E=1 bun test src/auth/snaplink.e2e.test.ts` | ✅ 1 pass / 31 expects（真实 sso-server）|
| `bun test src/audit/` | ✅ 7 pass / 41 expects |
| `npx vitest run`（apps/web） | ✅ 115 pass / 20 文件全绿 |
| `npx vue-tsc --noEmit`（apps/web） | ✅ 0 错误 |
| `sqlite3 data/app.sqlite ".tables"` | ❌ **无 audit_outbox 表**（B4 实证）|
| 源码核验 | app.ts:178 挂载 auditRequestMiddleware（**fallback 路径生产在用**）；app.ts:139-144 createAuditIntegration；`.env` 无 AUDIT_APP_URL；`.git` 空目录 |

**自我披露（诚实审计的一部分）**：我最初误用 `bun test` 跑 apps/web 得到 11 fail/2 errors（`vi.stubGlobal is not a function`）——apps/web 的 test script 是 `vitest run`。改用正确 runner 后 115 pass。这正说明"命名命令 + 结果"是唯一合法证据形态：**api 用 bun、web 用 vitest，两个 runner 不能互换**。

## 1. VERDICT

```
VERDICT: FAIL - 激活路径（B1/B2）缺陷在源码中仍然存活且测试门禁全绿，
现有 7 个 audit 测试全部只覆盖正常路径；P0 激活层（forwarding.ts）零测试；
4xx 永久分类、409 幂等语义、批量部分成功、flush 竞态、lease 恢复五个高风险
路径无任何测试可执行；修订阶段要求（先红后绿 B2）方向正确但缺少可执行测试
规格，无法证明"缺陷不会再回来"。全部阻塞项证据等级 Verified（本次复核）。
```

## 2. Findings（按严重度）

| # | Sev | 缺陷模式 | 缺失测试 | 能抓住它的精确测试用例 |
|---|---|---|---|---|
| H1 | High | **fallback 事件 actor 静默丢失 + 载荷污染**（B2）。`withActor(newAuditEvent(...))` 运行时把 actorUserId/actorDepartmentId/actorDepartmentSnapshot 摊到 AuditEvent **顶层**（契约外字段，contracts.ts 无这些顶层键），`operation.actorUserId` 恒 null；app.ts:178 挂载 → 生产在用；bun 不做类型检查 + 现有测试不断言 actor → 套件全绿 | 现有 `request-middleware.test.ts` 第 3 个测试**已走到缺陷行**但只断言 requestId/traceId/idempotencyKey/status | 认证会话 + 未埋点 POST：断言 `event.operation.actorUserId === "user-1"` **且** `Object.keys(event)` 精确等于契约键集合（无顶层 actorUserId/actorDepartmentId/actorDepartmentSnapshot）。当前必红；修复后绿。这正是 gate 采纳的"先红后绿"，但**必须含载荷键集严格断言**——否则治理端 ValidateBasic 即使字段映射后仍会因未知顶层键拒绝 |
| H2 | High | **P0 激活路径零测试**（B3 子集）。forwarding.ts:86 行无 forwarding.test.ts；`trigger()` 在 flush in-flight 时丢弃重触发，新事件依赖下次 append/外部 worker 才投递，无补偿语义 | 4 个用例，用两个 Deferred Promise 控制顺序（spec §7） | (a) append 期间 flush 进行中 → 解析 deferred 后再次 append → 事件在**下一次** trigger 投递（断言最终送达，非丢失）；(b) 两个并发 append → 仅一次 flush（publisher.flush 调用计数 === 1，防重复发布）；(c) publisher 抛错 → running 清除、事件保持 retryable；(d) publisher=null（无 AUDIT_APP_URL）→ append 只落 outbox 不崩溃 |
| H3 | High | **4xx 永久失败无分类**（B1 后果）。client.ts:66-80 任何 `!response.ok` 一律 throw → publisher 指数退避重试 → 契约 400 永久积压 failed，与 gate 采纳的"4xx permanent 分类（outbox 不永久积压）"矛盾——**采纳项尚未实现也尚无测试** | 假 transport 状态机测试 | (a) 返回 400 → 条目进入**终态 failed（不重试）**或检疫区；(b) 503 → retryable 且 backoff 递增；(c) 408/429 → retryable；(d) fetcher 永不 resolve + timeoutMs=1 → abort 路径 markFailed 且无 unhandled rejection（超时 uncertain 可控制时钟，不用 sleep） |
| H4 | High | **批量部分成功未处理**。治理端逐事件 ValidateBasic（人工证据），发送端 all-or-nothing：整批 400 → 整批重试 → 重试成功后再投递会产生跨批重复（幂等键映射不存在时） | 集成层假 transport 返回 200+逐事件错误 / 207 | 50 事件批，其中 3 个无效：断言当前实现整批 throw（**先钉死现状**），随后修订实现为逐事件检疫或整批重试 + 幂等键保证无重复；验收断言：合法事件最终 published，非法事件进 quarantine |
| H5 | High | **409 幂等语义无测试**（gate 采纳 idempotency_key=event.id 但无规格）。409 应视为"去重成功"而非失败，否则重试风暴 | 契约测试 | 同一 event.id 投递两次 → 第二次 409 → 断言发送端**不 markFailed**（去重=成功）；瞬断重试场景：首投 503 → 重试 2xx → 断言治理端恰好收到一个事件（或第二次 409），无重复落账。验收：`countPending === 0` |
| M1 | Medium | **lease/崩溃恢复无测试**。claim 后崩溃（publish 成功但未 ack）→ stale lease 被接管 → 重复发布 → 靠幂等键兜底；lock-token 不匹配不得误标 published | 隔离 :memory: 库 + 可控时钟 | (a) 手动置 `locked_at` 过期 → 第二次 claim 接管（断言锁 token 变更）；(b) 用错误 lockToken 调 markPublished → 不生效（断言仍 publishing）；(c) 接管后重新发布 → 断言同事件 idempotency_key 稳定（去重前提） |
| M2 | Medium | **B1 发送端契约测试缺失**。client.test.ts 只断言信封（URL/method/Authorization/schemaVersion/source/events[0].id），**零事件字段映射断言**；适配器（字段映射）应 reproduce-first | 契约测试（先红后绿） | 断言载荷中每个事件含治理端 9 必填键（event_id/source_system/event_type/schema_id/action/outcome/data_classification/retention_class/idempotency_key + schema_version>0 + actor.id + payload）——当前必红（无映射），适配器落地后绿 |
| M3 | Medium | **批边界/分页边界未测**。batchSize=50 的边界（49/50/51 事件）、audit-admin 查询分页 | 单元 + 集成 | (a) 51 事件 → 两次 flush 各 ≤50；(b) batchSize=1 单事件批；(c) filtered query 的 limit/offset 边界（现仅测过滤语义）|
| L1 | Low | **惰性建表无初始化测试**（B4 nuance）。audit_outbox 由 CREATE IF NOT EXISTS 惰性创建——首个 append 前 audit-admin 查询的路径未测 | 单元 | 全新 :memory: 库：先 listQuery 再 append → 断言不抛错且表存在 |
| L2 | Low | **skip 纪律**。snaplink E2E 的 `skipIf(SNAPLINK_E2E !== "1")` 是环境条件跳（可接受），但 M2 门禁要求 E2E 进 CI——届时必须**移除 skip** 而非保留"CI 里永远跳过" | CI job | CI 中 `SNAPLINK_E2E=1` 显式运行，skip 删除；断言 31 expects 全绿 |

## 3. 风险覆盖矩阵

| 风险路径 | 覆盖 | 证据 |
|---|---|---|
| 正常路径（append→claim→publish→ack） | ✅ 单元 | sqlite-outbox.test.ts 3 例 + client.test.ts 1 例 |
| fallback 事件 actor 上下文 | ❌ | 测试走到缺陷行但不断言 actor（H1）|
| 契约兼容（治理端 9 必填字段） | ❌ | client.test.ts 仅断言信封（H2/M2）|
| 异步乱序（flush 期间 append） | ❌ | 无 forwarding.test.ts（H2a）|
| 重复提交 / 409 幂等 | ❌ | 无（H5）|
| 超时 uncertain（abort） | ❌ | 无（H3d）|
| 4xx permanent vs 5xx retryable | ❌ | 无（H3）|
| 批量部分成功 | ❌ | 无（H4）|
| lease 过期接管 / 崩溃恢复 | ❌ | 无（M1）|
| 分页/批边界 | ⚠️ 部分 | filtered query 有，limit/batchSize 边界无（M3）|
| 时区/精度边界 | ⚠️ 部分 | 时间戳 ms 整数、lease 边界等式未测（M1 时钟可控后补）|
| 权限变化 | ⚠️ 不适用审计核心 | audit-admin 路由鉴权在 auth 测试域，未见专项 |
| 跨仓库 E2E（ERP→outbox→governance→console） | ⚠️ 手动 | SNAPLINK_E2E=1 单库 E2E 绿；**governance 实例不存在**，M2 链路未执行 |
| 重启恢复 | ⚠️ 隐式 | sqlite 持久化本身，无重启测试（L1）|
| 测试隔离 | ✅ | 全部 `:memory:` 独立库；无共享可变状态；无 sleep（已验证源码）|

## 4. 诚实审计（executed vs claimed）

| 声称 | 判定 | 备注 |
|---|---|---|
| kickoff "设置 env 即激活、零代码" | ❌ **被证伪**（B1 Verified） | gate 正确 FAIL；kickoff 文档未自降级——修订阶段须回写（gate 已要求）|
| gate B1–B5 全部 Verified | ✅ **本次独立复核一致** | env.ts:76 / client.ts:46 默认路径、contracts.ts:171 签名、:116/:131/:139 三错、31 个 tsc 错误精确吻合、.tables 无 audit_outbox、.git 空目录 |
| 前轮 QA "133 pass / 1 skip / web 115 pass / E2E 31 expects" | ✅ **可复现** | 我全部重跑，数字一致 |
| 证据 01 "用 git log 扫描"、"无 campaign 配置" | ⚠️ **provenance 不实** | `.git` 是空目录（无 log 可查）；campaign yaml 08-06 09:07 已存在（未运行）。前轮 F9 已提，**修订清单未含证据 01 归一**——应补入 |
| governance 侧（server.go:77、models.go:360 ValidateBasic 9 字段） | ⚠️ **not_executed** | governance 仓库本机不存在（Verified 缺失），依赖人工复核文档 + gatekeeper 复核；M2 E2E 需要真实实例，当前无 |
| schemaVersion 兼容值 | ⚠️ **not_executed** | Unknown，契约测试前不可宣布（M2 测试即为回答手段）|
| skip 的 E2E 测试 | ✅ 合规 | 条件跳 + 手动可跑全绿；非"为过流水线而 skip"，但 M2 后须移除 skip |
| 我自己的 runner 误用 | ⚠️ 已纠正 | `bun test`（web）11 fail 为 runner 错配产物；`npx vitest run` → 115 pass。**建议 CI 配置显式 pin 每个 package 的 runner**，防止 `pnpm test`（turbo 顶层）跑错 |

## 5. 给修订阶段的执行建议（超出裁决项的部分）

1. H1 红测试是**一行断言 + 一行键集断言**，必须先落地（当前套件绿 = 假绿）。
2. 修订清单补入：证据 01 provenance 归一（前轮 F9）、H3/H4/H5 的可执行测试规格（gate 只写了"4xx permanent 分类"与"idempotency_key=event.id"两个标题，未写测试形态）。
3. M2 验收的 E2E 前置依赖（governance 实例 + schema 注册）应列为显式风险项，not_executed 原因写明，不能以"理论上会通过"替代。
4. `pnpm check` 转绿 = api tsc 31 错清零 + api bun test 绿 + web vitest 绿 + web vue-tsc 绿；命令集要 pin 进 CI（B5 的 git/CI 前置项）。

本评审为咨询性意见；结论全部基于本次实际运行的命令与源码走查，不构成发布批准。
