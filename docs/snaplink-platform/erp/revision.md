# 修订版最终路线图 — ERP（sverpweb-stable）接入平台六件套

> **平台架构顾问 · revision 阶段产出（第 2 轮，人工复核修订版）**
>
> - **§2.3 409 语义已按终裁门实测修正**：同 event.id 重投 = 202+Duplicate；409 仅两个冲突分支（检疫+告警）[Verified]
>
> - **状态：Proposed** —— 本文件可作为人工批准的基础，但本身仍是提案；不豁免任何仓库门禁，批准后按决策链提升（`decisions/` 01→05 流程现成）。
> - **本文件是新文档**：kickoff 原文不回写、保留为决策证据（其 P0"设置 env 即激活、零代码"论断已由 B1 实锤证伪，原文即被否决策略的历史记录）。
> - **依据链**：gate `VERDICT: FAIL`（`docs/snaplink-platform/erp/gate.md`）→ 04-gate-verification（B1–B5 人工复核**全部 Verified**，`docs/snaplink-platform/erp-inputs/04-gate-verification.out.md`）→ 5 份对抗评审（architect / qa_lead / backend_engineer / protocol_expert / devops_engineer / security_engineer / compliance_officer）。
> - 下游：review2（对抗审查）→ gate2（裁决）。本修订逐条落实 gate 的 5 条修订要求。

## 0. 证据等级约定

| 等级 | 含义 |
|---|---|
| [Verified] | 源码/命令/实机独立复现（≥2 个独立评审交叉确认，或 gatekeeper 亲核） |
| [Partial] | 部分成立（如"载荷格式吻合"仅去重语义吻合） |
| [Proposed] | 建议/设计决定，待人工批准 |
| [Unknown] | 契约测试/决策前不可宣布 |
| [not_executed] | 有证据缺口（未运行/未执行），不能以"理论上会通过"替代 |

## 1. 上轮 FAIL 阻塞项 → 本轮修订对照（B1–B5）

| # | 阻塞项（复核结论） | 修订处置 |
|---|---|---|
| B1 | L2 契约不兼容：ERP 默认 `AUDIT_INGEST_PATH=/api/v1/audit/events`（`env.ts:76`）vs governance `POST /api/v1/events:batch`（`server.go:76-77`）；`ValidateBasic` 必填 9 字段全部缺失；要求已注册 active schema。设 env 即全批 400 → failed 永久积压 [Verified] | **P0 全面修正**（§2）：契约对拍 → 字段映射/适配器 → schema 注册 → L1 试运行 → 宣布 L2；4xx permanent 分类（§2.2）+ `idempotency_key=event.id` 409 语义（§2.3） |
| B2 | 回退审计事件 actor 丢失：`request-middleware.ts:116` `withActor` 作用于 AuditEvent 而非 OperationContext，`operation.actorUserId` 恒 null（31 个 TS 错误之一）[Verified] | **精确修复规格**（§3）：与 `emitAuditEvent` 同款写法（先 `withActor(operation)` 再 `newAuditEvent`），先红后绿 |
| B3 | 门禁红（31 错，五方复现）+ `forwarding` 激活路径零测试（`apps/api/src/audit/` 无 `forwarding.test.ts`）[Verified] | **M1 硬门槛**（§4.1）：tsc 0 错 + 全部测试绿；`forwarding.test.ts` 6 类用例 + 4xx/409/批量部分成功/超时 uncertain 测试 |
| B4 | 生产零审计事实（初判）：`data/app.sqlite` 原无 `audit_outbox` 表；**2026-08-06 10:32 起隐式迁移已建表（0 行，无版本戳/备份）** [Verified 复核]；无 purge、governance retention 只读无执行器 → Not audit-ready 判断保留 | M1：迁移版本戳 + 建表备份 + 存量表审计；坏行检疫与保留策略（owner #5）；M2 E2E 全绿才产生审计事实 |
| B5 | 无 git（`.git` 空目录）、生产二进制脏树+dirty、无 CI、备份过期、8 项 owner 决策未决 [Verified] | **M4 前置**（§4.3）：git init + 首次提交 + CI；8 项 owner 决策附责任人（§6） |

**处置原则**：阻塞项无一被驳回（04 号证据人工复核全部 Verified），全部转化为可执行里程碑项；未解决项不再作为"路线图内待办"，而是**门禁准入条件**（不绿不前进）。

## 2. P0 修正：审计 L2 接入路径

> kickoff P0"最大杠杆、零代码"**被证伪**（B1 实锤：路径、必填 9 字段、active schema 三缺一即全批 400）。修正为：**零架构改动 + 一个契约适配器（~150 行）+ 一次 schema 注册（30 条）+ 部署规则**。本项证据 [Verified]（gate + architect H1 + compliance F-2 + devops H1 四方独立复核）。

### 2.1 五步路径（替代"设置 env 即激活"）

| 步骤 | 内容 | 证据等级 |
|---|---|---|
| ① 契约对拍（M0） | golden payload：真实 ERP 事件样本 POST governance 测试实例 → 预期 400 且错误列出缺失字段 → 断言列表 = B1 字段清单，记入契约测试。governance 仓库本机存在（`/home/dwp/snaplink-audit-governance`，`go test ./...` 全绿）→ 跨仓契约测试现在即可执行，M0 约半天 | [Verified] 仓库存在与测试绿；[Unknown] 兼容值由测试回答 |
| ② 字段映射/适配器（M1） | 新建 `apps/api/src/audit/governance-transport.ts`：字段映射（§2.5）+ `AUDIT_APP_URL` 仍留空（**不启用**） | [Proposed] |
| ③ schema 注册（M1） | governance 注册 ERP schema：**每 event_type 一条（终裁门实测 30 条，早期枚举 22 条低估 36%）**——`hasField` 只特判 6 个字段名，required 里写 `aggregate_type` 会恒失败 [Verified]；顺序 runbook：**tenant → source → schema → token → env 五步**；**发布门禁**：新 event_type 必须先注册 schema 才能发版（CI 检查，防止随发版 422 拒收）[Proposed] | [Verified] hasField 陷阱；[Proposed] runbook；[Verified per gate2] 30 条 |
| ④ L1 试运行（M2） | 设置 `AUDIT_APP_URL` + `AUDIT_INGEST_PATH=/api/v1/events:batch` + `AUDIT_APP_TOKEN`；先按 **L1 sink 验收**（账本 E2E 全绿） | [Proposed] |
| ⑤ 宣布 L2 | 部署规则核对清单（deploy/README：URL/路径/token/schema/验收 curl）全部通过后才宣布；**可逆回退**：清空 env + 重启即回退 L1，outbox 数据无损 | [Proposed] |

### 2.2 4xx permanent 分类（失败签名拒绝，outbox 不永久积压）

现状：`client.ts:66-80` 任何 `!response.ok` 一律 throw → 指数退避无限重试（封顶 5min）——H1 误配置场景 = 全 outbox 永久重试风暴 [Verified]。修正（与 pi-batch 失败签名拒绝同构：永久性失败立即判负落终态，不进入无限重试）：

| 分类 | 语义 | 处置 |
|---|---|---|
| **permanent（4xx 确定拒收）** | 400/401/403/404/405/410/422（契约、认证、配置类） | 立即落**终态**：`permanent=1` + `last_error='[permanent] …'` + 告警；**不再被 claim**；人工修复配置后经 admin 重放（远期暂停） |
| **409 + `event_id_content_conflict` / `idempotency_key_conflict`** | 发端缺陷（语义见 §2.3） | 同 permanent：`permanent=1` + 告警；**绝非去重成功**（与 §2.3 三分支严格一致） |
| **retryable** | 408/429、5xx、网络错误、超时（uncertain） | 指数退避（封顶 5min，带 jitter）；幂等键保证重试不重复落账。~~尊重 Retry-After~~：governance 全仓 0 实现（终裁门 A2 实锤），承诺已删——如需要由 governance 侧建能力（M1 议题，非 ERP 承诺） |

**终态机制（终裁门实锤后重裁定）**：`[permanent]` 标记写 `last_error` **不可行**——`sqlite-outbox.ts` claim UPDATE 显式 `last_error = NULL` 且 `WHERE status IN ('pending','failed')` 会把 failed 行重领、标记抹除 [Verified `sqlite-outbox.ts` claim]。**裁定**：新增列 `permanent INTEGER NOT NULL DEFAULT 0`（SQLite `ADD COLUMN` 零重建，无需动 CHECK 约束）[Proposed]；claim 查询与 UPDATE 增加 `AND permanent = 0`；permanent 行永不被 claim（队首不再毒丸，见下）。**批量毒丸隔离（终裁门 B 项）**：`client.publish()` 现为 `Promise<void>` 全或无、整批 markFailed、`ORDER BY created_at ASC` → 1 条坏事件使其后健康事件无限饥饿 [Verified]。**裁定**：transport 改为按事件返回结果（`publish → AuditPublishOutcome[]`，每事件 ok/permanent/retryable；governance `postBatch` 本就逐事件回 receipts）；outbox 逐事件 mark；坏事件落 permanent 后 claim 天然跳过 → 队首保护，健康事件不被饿死 [Proposed]。验收测试（先红后绿）：400 → permanent=1 且不再被 claim（并发 claim 也不重领）；同批 1 坏 9 好 → 坏 permanent、好 published（批量部分成功）；503 → retryable 且退避递增；408/429 → retryable；fetcher 永不 resolve + timeoutMs=1 → abort 路径 markFailed 且无 unhandled rejection。**迟到 202 语义（终裁门 A1 修正）**：`markPublished` 守卫 `status='publishing'`+lock_token、`markFailed` 已清 lock_token → 迟到 202 是**无害 no-op**（0 行变更），正确恢复路径 = 事件重领重投 → governance 202 Duplicate → markPublished [Verified `sqlite-outbox.ts` 守卫]。

### 2.3 idempotency_key=event.id 的 409 去重语义

governance 对"同 key 异 event_id"是 **Conflict 409 而非 Duplicate**（`service.go:371-379`，[Verified] backend_engineer 源码核验）→ 陷阱：若把业务 Idempotency-Key 头映射为幂等键，业务重试第二次必被永久拒收。**裁定**：

- `idempotency_key = event.id`（每个事件稳定唯一，跨重试不变）[Proposed]
- **不得**映射 Idempotency-Key 请求头 [Verified 陷阱 + Proposed 裁定]
- **正确三分支语义**（`service.go` Ingest 源码实测 [Verified]，终裁门复核）：
  - **202 + `receipt.Duplicate=true`**（同 event_id + 同 digest 重投）→ 去重成功：发送端 markPublished、不重试、不告警；
  - **409 + `event_id_content_conflict`**（同 event_id + 内容不同）→ **编程错误/账本污染信号**：发送端 markFailed + `[permanent]` + 告警，**不得**当作去重成功吞掉；
  - **409 + `idempotency_key_conflict`**（同 key + 异 event_id）→ 同样检疫 + 告警（幂等键碰撞 = 发端 bug，杜绝 key 复用）；
  - 两个 409 分支都是"发端缺陷"信号而非"投递成功"，与 4xx permanent 分类（§2.2）同源处理。
- 语义推理：`idempotency_key = event.id` 后，同 event.id 重投走 202 Duplicate 分支；409 两分支理论上不应出现，出现即触发检疫告警（T12 测试覆盖三分支）[Proposed]
- 契约测试双侧钉死：ERP golden 映射单测 + governance `test/erp_contract_test.go`（钉死三分支语义）[Proposed]

### 2.4 真源裁定（保留）与可逆回退

- **真源裁定保留** [Verified]：本地 outbox = 投递真源（retryable truth，源码注释实证）；governance 账本 = 审计事实真源（platform-map 所有权）。2xx 确认后 outbox 退为运维语义，业务永不读 outbox 当事实。
- **可逆回退保留** [Proposed]：`AUDIT_APP_URL` 留空即纯本地 L1；启用后回退 = 清 env + 重启，outbox 数据无损。schema 注册是只读叠加，可回退。
- **试运行门槛保留** [Verified]：governance 是设计期+参考实现（文件存储、单节点）→ 账本 E2E 全绿才宣布 L2；生产硬化验收（多节点/持久存储/性能基准）由 owner 决策 #8 定。

### 2.5 字段映射与契约差异清单（适配器输入规格）

| ERP 侧 | governance 契约侧 | 处理 |
|---|---|---|
| `event.id` | `event_id` | 直接映射 |
| 信封 `source` | `source_system` | 适配器填入 `sverp-web`（信封字段 `source`/`sentAt` 会被 batchRequest 静默丢弃 [Verified P6] → 不依赖信封，映射在事件体内完成） |
| `operation.*`（B2 修复后） | 扁平 `operation_id` / `trace_id` / `actor` | 解嵌套映射；`actor.id` 是接收端必填，修复前显式未知标记 |
| `metadata.outcome` | `outcome` | 提升到顶层 |
| — | `idempotency_key` | **派生 = event.id**（§2.3） |
| `before`/`after`/`metadata` | `payload` / `payload_ref` | 打包进 payload |
| — | `data_classification` / `retention_class` | schema 注册时定常量 |
| `occurred_at`（整数毫秒） | 契约 `format: date-time` | **类型级不兼容** [Verified P6]：Go 解码层拒 ms → **唯一可行 = 适配器转 RFC3339**（终裁门 A3 修正，删除"schema 接受 ms"分支，契约测试钉死） |
| — | `schema_id` / `schema_version` | 注册的 ERP schema v1（每 event_type 一条，30 条） |
| `tenant` | tenant 归属 | **由 governance claims 侧盖章注入**，事件不带（owner 决策 #1） |
| — | `ValidateBasic` 9 必填 | 适配器产出逐项满足，契约测试断言 |

## 3. B2 精确修复规格（先红后绿）

**定位** [Verified]：`apps/api/src/request-middleware.ts:116`（TS2345）、`:131`（TS2353）、`:139`（TS2345）、`sqlite-outbox.test.ts:122`（TS18048）——audit/ 4 个 TS 错误；运行时 `withActor(newAuditEvent(...))` 把 `actorUserId`/`actorDepartmentId`/`actorDepartmentSnapshot` 摊到 AuditEvent **顶层**（契约外键，治理端键集校验会拒收 [Verified S1]），`operation.actorUserId` 恒 null。

**修复写法**（与 `emitAuditEvent` 同款，[Verified] 该路径正确不动）：

```
// 错误（现网）：
withActor(newAuditEvent(operation, ...))          // withActor 只接受 OperationContext
// 正确：
const ctx = withActor(operation);                 // 先盖章进 OperationContext
const event = newAuditEvent(ctx, ...);            // 再构造事件
```

**E. null-actor 事件（终裁门 E 项）**：`app.ts` 审计中间件注册**先于认证** → 登录/401/403/404 探针全落 outbox 且 actor null [Verified]；governance `actor.id` 必填 → **L2 启用即保证永久积压**。**裁定（终裁门 B 项返工）**：ERP **无全局认证中间件**（认证在 authSession 级），"移到认证之后"不可行 → 改为 **authSession 挂载点跳过**：未认证请求不落审计（探针噪音，且 actor.id 必填无法满足）+ **认证事件范围裁定**（登录成功/失败本身是否入账，M1 内裁定，默认登录失败不落、成功落）；认证后请求 actor 恒非空（§3 断言覆盖）。测试：未认证 POST → outbox 0 行；认证 POST → actor 非空 [Proposed + Verified 现状缺陷]。

**先红后绿**（QA 退出标准 #3，当前必失败）：

1. 红：认证会话 + 未埋点 POST → 断言 `event.operation.actorUserId === "user-1"`（或等价非空）**且** `Object.keys(event)` 精确等于契约键集合（无顶层 actorUserId/actorDepartmentId/actorDepartmentSnapshot）——现有测试已走到缺陷行但只断言 requestId/traceId/status，套件绿 = 假绿 [Verified]。
2. 修复后绿；`bun` 不做类型检查 → tsc 0 错 + 断言测试**双保险**。

## 4. 门禁硬门槛

### 4.1 M1 准入（不绿不进 M2）

- **tsc 0 错**：当前 31 个 [Verified 五方复现]，含 audit/ 4 个（§3 清单）+ **sso-client 声明解析**（`@snaplink/sso-client` 是仓库外 `file:` 依赖，lock 只记路径不记内容 [Verified devops H3 / QA F7]——修复选项：版本锁定 + 声明修复，或纳入仓库管理，M1 内裁定）+ 其余 27 个
- **全部测试绿**：api bun test（133 pass / 1 skip，skip 为环境条件跳）、web vitest（115 pass）、web vue-tsc 0 错；CI 显式 pin 每个 package 的 runner（api=bun、web=vitest，不可互换）[Verified]
- **`forwarding.test.ts` 6 类用例**：激活（publisher=null 时 append 只落 outbox）/ 下游错误 / 并发追加（flush in-flight 期间 append 的事件在下次 trigger 送达，不丢失）/ 重启 / 空批 / 去重 [Verified 缺失 + Proposed 规格]
- **回退事件 actor 断言**（§3 红测试转绿）+ **4xx permanent / 409 三分支 / 批量部分成功 / 超时 uncertain** 测试（§2.2/§2.3，QA H3–H5 规格）
- **终态机制测试**（§2.2 终裁门 A 项）：permanent 行不再被 claim（单实例 + 并发 claim 双测）、claim 不抹 permanent 标记
- **毒丸隔离测试**（§2.2 终裁门 B 项）：同批 1 坏 9 好 → 坏 permanent、好 published；队首坏事件不饿死队尾健康事件
- **null-actor 语义测试**（§2.2 终裁门 E 项）：未认证请求不落 outbox；认证后 actor 非空
- **迟到 202 测试**（§2.2 终裁门 H 项，A1 修正）：abort/超时后响应成功到达 → 断言 **0 行变更 no-op**；恢复路径 = 重领重投 → 202 Duplicate → markPublished（与 §2.2 权威语义一致）
- **schema 30 条 + 发布门禁**（§2.1 终裁门 G 项）：注册清单全量枚举；CI 检查新 event_type 已注册
- **C 组完整性测试（终裁门 C 项，磁盘 0 处缺漏补全）**：
  - not_attempted 四态：postBatch 首错截断后，批内后续事件未被尝试 → 断言其保持 pending（下次 trigger 重投）；
  - 内容冻结：同一 event_id 二次投递内容不同 → permanent 检疫（digest 重算即永久，杜绝静默改写）；
  - receipts 按序 + event_id 交叉校验：key-conflict receipt 携带对方事件 ID 时按序配对断言；
  - L1 回滚测试：清 env 重启 → outbox 数据无损、纯本地 append 继续；
  - 租约接管 fake-clock：多实例 lease 过期 → 接管（论证→测试，终裁门 H 项补）；
  - fail-closed 红绿：`ValidateConfiguration` 缺配置拒启 + 断言；
  - 读侧授权：匿名读审计查询被拒；
  - 限流：未认证请求限流生效；
  - 5xx budget：连续 5xx 退避上限断言；
  - 坏行检疫：permanent 行不再被 claim（含并发）；
  - append 可见性：append 与 flush 并发时序断言；
  - countPending 拆分：pending/failed/permanent 分别计数，M2 指标用 pending + 新增 permanent
- **安全项**（新增阻塞级，见 §8）：demo 模式禁用、id_token 校验 + nonce、密钥脱敏——与门禁同批，作为 L2 路径 M1 准入项

### 4.2 M2 验收（全绿才宣布 L2）

审计 E2E 链路全绿：**ERP 操作（建单/审批/上传 3 类）→ outbox → governance 账本 → console 时间线**；具体断言：

1. governance `GET /api/v1/events?source_system=sverp-web` 命中且 `occurred_at`/`actor.id` 正确
2. outbox 无 `pending` 积压超 lease 周期（drainer 60s 内清空）；24h 无**新增** permanent（存量 failed 清零或全部永久检疫，permanent 单独计数，不与 published 混算——终裁门 D 项：`countPending` 含 failed 时"无 failed"恒不可达）[Proposed]
3. console 审计页可查（时间线）——**not_executed**：console 审计页当前全仓无实现、无 owner [Verified 终裁门]；前置 = 具名 owner（Proposed：ERP 前端 / console 维护者）+ M3 内实现
4. L2 宣布前置：`AUDIT_ALLOW_DEV_AUTH=false` + 真实签名/加密密钥轮换 + demo 禁用 + HTTPS（§8 N4/N3/N2）+ **`ValidateConfiguration` fail-closed**（配置缺失即拒启，不 fail-open）+ **读侧授权裁定**：审计查询必须经 governance 授权（OIDC scope），console 经能力清单接入，禁止匿名读 [Proposed]
5. E2E 的 skip 在 M2 **移除**（不是"CI 里永远跳过"）[Verified 纪律]
6. **not_executed 前置显式化**：governance 生产实例（owner #8）、tenant/schema 注册（owner #1/#2）、租户映射——未完成项一律标 not_executed 并具名 owner，不得默认完成
7. **D 组前置归属（终裁门 D 项）**：EncryptedFields 入 ERP schema（governance 能力现成未用，账本明文→加密字段清单 M1 内裁定）；ERP 侧 purge 缺失（retention 执行器，owner #5 具名）；读侧授权裁定（具名 owner：platform 安全负责人）；permanent 重放 admin 路径（具名 owner：ERP 后端负责人）——全部 not_executed + 具名任命，不得默认完成

### 4.3 M4 前置（pi-batch 嵌入的前提）

- **git init + 首次提交 + CI**（tsc/bun/vitest/E2E job/audit 契约测试）；否则 campaign 指纹/断点续跑不可用 [Verified B5]
- 然后才嵌入 pi-batch：**campaign 仅分析面**（发现 + 提案流水线），`git_commit: false`，不直接改生产代码 [Proposed，kickoff 工程运营项保留]
- 建议：首次提交在 M1 修复前完成（基线快照，保留红态与脏树作为决策证据，服务 C1 二进制溯源）——同一动作，时间点提前，owner 可批准

## 5. 修订后里程碑路线图 M0–M5

| 里程碑 | 内容 | 回退/风险 | 可执行验收（证据等级） |
|---|---|---|---|
| **M0 契约对拍**（半天） | golden payload 对拍（§2.1 ①）；裁定 `occurred_at` 格式（§2.5）；双侧契约测试落地 | 零生产影响 | 400 断言列表 = B1 字段清单；`test/erp_contract_test.go` 钉死 409 语义 [Verified 可执行] |
| **M1 门禁转绿 + 修复** | B2 修复 + 先红后绿（§3）；tsc 31→0；forwarding 6 类测试；4xx/409/批量部分成功；安全项 N1–N3；drainer 并入（默认 60s > lease 30s，lease+lockToken 多实例安全 [Proposed 论证（fake-clock 接管测试入 M1，终裁门 C）]）；schema 注册 30 条（**不启用**） | 零生产影响；schema 只读叠加 | **§4.1 全部绿**（tsc 0 + 全部测试绿） |
| **M2 L2 试运行** | 部署规则：URL/路径/token；governance 实例（owner #8，not_executed 显式）；OIDC client_credentials（`audit:event:write`）；`AUDIT_ALLOW_DEV_AUTH=false` + fail-closed 配置校验 + 读侧授权；deploy/README 核对清单 | **高**：H1 场景（拒收→积压）——permanent 兜底，24h 无新增 permanent 即验证 | **§4.2 E2E 全绿 → 宣布 L2**；回退 = 清 env 重启无损 |
| **M3 媒体迁移**（独立并行） | presign PUT 上传 + presign GET 下载适配器（`MediaStore` 新实现，前端零改动）；懒迁移 + 回填 job（幂等按 key 对账）；迁移期双读（vault 优先、本地回退），**禁双写** | 中：回填中断、权限模型差异 | 上传/下载/缩略图 3 条 E2E 走 vault；50MB 直传不落 BFF 内存（压测断言）[Verified presign 现成] |
| **M4 运营嵌入** | **前置：git init + 首次提交 + CI**；pi-batch 嵌入（campaign 仅分析面，`git_commit: false`）；platform-status/247 `PLATFORM_REPOS` +第 7 仓（iris-ui 不加，锁版本+升级门禁管）；备份自动化 + RTO/RPO；监控接线（/metrics/告警） | 低 | `platform-status.sh` 7 仓全绿；drainer"停机 10min → 恢复 5min 清空积压"；备份演练一次 |
| **M5 通知 + 事件注册** | aero-im best_effort_async 通知（审批/异常/任务分派）；审计事件类型注册表文档化 | 低-中：aero-im 跨主机项待验证 | 审批触发通知 E2E；通知服务宕机时业务无感、无重试风暴 |

跨里程碑风险：owner 决策 #1/#8 阻塞 M2；#3 阻塞 P1 aero-id 项（不阻塞主线）；governance 硬化进度未知 [Unknown]。

## 6. 8 项 owner 决策（附责任人）

> 全部责任人标注 **Proposed 待任命**——批准本路线图时逐项任命。阻塞关系列明。

| # | 决策 | 建议默认值 | 责任人（Proposed） | 阻塞 |
|---|---|---|---|---|
| 1 | **租户映射**：ERP 事件进哪个 tenant | 单租户 `sverp-web`（claims 盖章注入，事件不带 tenant_id）；每公司一租户待 ERP 公司维度数据成熟 | ERP 产品负责人 | **M2 验收** |
| 2 | **schema 治理主体**：v1 字段清单（30 条）批准人；升级走 governance 注册流程是否够 | 由 governance 维护者批准；升级流程=注册新版本（兼容 ADR） | governance 维护者 | M1 ③ |
| 3 | **aero-id org 路线**：org/department 域是否入路线图、投影形态 | 未定；冻结 P1 投影，先契约定义+预留（H2 降级） | aero-id 维护者 | P1（不阻塞主线） |
| 4 | **vault 合规约束**：质检图/报价附件保留策略与 Legal Hold 是否适用、与 governance retention 关系 | 附件保留随业务文档策略，审计事实不依赖 vault | 合规负责人 | M3 细节 |
| 5 | **审计保留期**：governance retention 具体值 | 与 governance 默认/合规要求对齐（值待定） | 合规负责人 | M1/M2 保留策略实现 |
| 6 | **通知归属**：模板在 aero-im 还是 ERP 拼接；偏好 UI 位置 | 业务文案 ERP 侧、模板 aero-im；偏好 UI 待定 | ERP 产品负责人 | M5 |
| 7 | **iris-ui 门禁细则**：0.2.x 锁版本 + 升级触发条件 | 安全修复/新组件需求触发，console 维护者裁定 | 前端/console 维护者 | 常设 |
| 8 | **governance 硬化负责人**：实例/URL、sverp-web 注册、凭据方案（OIDC client_credentials）、`AUDIT_ALLOW_DEV_AUTH=false`、真实密钥、备份 RTO/RPO、监控、发布脚本/保留、旧 sverpweb 下线标准 | 见 §8 N4；硬化验收=多节点/持久存储/性能基准 | platform 运维负责人 | **M2 部署面** |

devops 10 项待决策映射：#①⑧⑨→8、#②→2、#③→8、#④→M0/M1 技术项（契约测试钉死，非 owner 决策）、#⑤→1、#⑥→8/M4、#⑦→M4 前置、#⑩→8。

## 7. 保留的 gate 采纳项清单（修订必须保留，全部继承）

| 采纳项 | 出处 | 证据等级 |
|---|---|---|
| 选项 A 适配器 + schema 注册，`AUDIT_APP_URL` 留空**可逆回退 L1** | gate / architect §3 | [Proposed] |
| M0 契约对拍（golden payload）先行 | gate / architect §4 | [Proposed] |
| **4xx permanent 分类** + 终态机制（`permanent` 列零重建 + claim 排除 + 逐事件 receipts 毒丸隔离） | backend B1 + 终裁门 G2-A/G2-B | [Verified 现状缺陷] + [Proposed 修复] |
| **`idempotency_key = event.id`**，不得映射 Idempotency-Key 头，409 语义 | backend B3（`service.go:371-379`） | [Verified 陷阱] + [Proposed 裁定] |
| §3.7 hasField 陷阱：每 event_type 一条 schema（30 条） | backend §3.7 | [Verified] |
| B2 精确修复（withActor 作用于 operation）+ 先红后绿 | gate / QA H1 | [Verified] |
| **H2 降级**：aero-id 组织只读投影 → 契约定义 + 预留（`DepartmentSnapshot` 可空占位已做对，保持） | gate / architect H2 | [Verified] |
| **媒体 presign 直传禁双写**：presign PUT/GET 直连，BFF 不缓冲大文件；懒迁移 + 回填，双读禁双写 | gate / architect M2 | [Verified presign 现成] |
| **真源裁定**：outbox=投递真源、governance=审计事实真源 | kickoff / gate | [Verified] |
| 11 项不接清单 + architect 补充触发条件（"替换 outbox"仅当 governance 提供事务性中继时重评） | kickoff / architect | [Proposed] |
| drainer（60s > lease 30s，lease+lockToken 多实例安全） | architect M1 | [Verified 缺失] + [Proposed 论证（租约接管尚需 fake-clock 测试，终裁门 C）] |
| schema 注册顺序 runbook：tenant→source→schema→token→env | backend | [Proposed] |
| Persistence Design：本阶段无新表无迁移 | backend §12 | [Verified 复核] |
| 双侧契约测试（ERP golden 单测 + governance `erp_contract_test.go`） | backend / QA M2 | [Proposed] |
| kickoff 三待核点裁定：#1 schemaVersion → 已查明（问题深于 schemaVersion，M0/M1 处理）；#2 vault presign → 已解决（get/put 现成）；#3 governance 硬化 → 确认设计期+参考实现 | architect §5 末 | [Verified] |
| 组织上下文数据来源显式化：Snaplink userinfo 字段，停止 ~20 字段名启发式猜测 | gate 修订要求 #2 | [Verified 现状脆弱] + [Proposed] |

## 8. 新增阻塞级发现（gate 清单外，评审轮实测复现，补入修订清单）

| # | 发现 | 级别 | 证据 | 处置（里程碑） |
|---|---|---|---|---|
| N1 | **id_token 收到即存、从不校验，nonce 从不发送**（OIDC Core §3.1.3.7 MUST 违反） | High | [Verified] protocol P1，源码+测试走查 | M1：校验签名/iss/aud/exp + 发送并校验 nonce；先红后绿（伪造 id_token 拒绝）；责任人 Proposed ERP 后端 |
| N2 | **demo 模式授权码伪造 → `demo:root` + `["*"]` 全量管理员**（运行时复现） | High | [Verified] security S2 复现 ×3 | M1：生产禁用 demo + 删除死配置 `DEMO_PASSWORD` + 先红后绿；伪造身份污染审计 actor 真实性 → **L2 宣布前提** |
| N3 | **全链路明文 HTTP + `.env` 活体密钥**（client secret/会话密钥/死配置 demo 口令） | High | [Verified] security S3 / compliance F-4 快照走查 | M1：HTTPS 终止 + 密钥轮换/落盘脱敏（governance 凭据同链） |
| N4 | **账本认证默认 fail-open**：`AUDIT_ALLOW_DEV_AUTH` 默认 true + 开发默认签名/加密密钥 → 削弱"审计事实真源"可信度 | High | [Verified] protocol P4，`main.go` 实证 | L1 试运行前置（M2）：生产 = `AUDIT_ALLOW_DEV_AUTH=false` + 真实密钥；owner 决策 #8 |
| N5 | **未认证 POST 写放大 outbox**（复现 5 请求→5 行）+ 无保留策略 + 256MiB body/50MB arrayBuffer 内存 DoS | Medium | [Verified] security S4/S7 复现/走查 | M1/M2：未认证限流 + 保留策略（owner #5）+ 请求体边界 |
| G2-A | **permanent 终态不可落地**：claim UPDATE 抹 `last_error` + failed 被重领 → "400 不重试"无实现路径 | Critical | [Verified 终裁门] `sqlite-outbox.ts` claim | §2.2：新增 `permanent` 列（零重建）+ claim 排除（已并入 §2.2） |
| G2-B | **批量毒丸**：publish 全或无 + 队首坏事件饿死健康事件；"批量部分成功"测试无实现路径 | Critical | [Verified 终裁门] `client.ts`/`sqlite-outbox.ts` | §2.2：逐事件 receipts + permanent 隔离（已并入 §2.2） |
| G2-C | **文档自相矛盾**：§2.2"409=视为成功" vs §2.3 三分支互斥 | Critical | [Verified 终裁门] 磁盘 L55 vs L68 | 本修订已同步（§2.2 表改三分支） |
| G2-D | **M2 验收自相矛盾**：countPending 含 failed → "24h 无 failed"恒不可达；console 审计页无实现无 owner | High | [Verified 终裁门] | §4.2 重写：pending 主指标 + not_executed 显式化（已并入 §4.2） |
| G2-E | **匿名 null-actor 事件**：审计中间件先于认证注册 → 探针全落 outbox、actor null → governance 必拒 → L2 启用即积压 | High | [Verified 终裁门] `app.ts` 顺序 | §3：中间件后移 + 未认证不落审计（已并入 §3） |
| G2-F | **安全主密钥级**：dev-auth 默认 true + 默认密钥 = 账本主密钥公开；读侧授权未裁定 | Critical | [Verified 终裁门] `main.go` | §4.2 #4：fail-closed 校验 + 读侧授权（已并入 §4.2） |
| G2-G | **schema 计数低估**：22 vs 实测 30（-36%）；新 event_type 随发版即 422 | High | [Verified 终裁门] | §2.1：30 条 + 发布门禁（已并入 §2.1） |
| G2-H | **M1 清单缺口**：迟到 202 未测（幂等安全网核心）；多实例租约"论证≠测试" | Medium | [Verified 终裁门] | §4.1：迟到 202 + 并发 claim 测试（已并入 §4.1） |
| N6 | **刷新令牌轮换/重用零测试** | Medium | [Verified] protocol P7 | P2 待办（触发条件：token 生命周期安全评审） |
| N7 | 无 BCL/CAEP：SSO 撤销后 ERP 会话最长 1h（生产 token_ttl）+ 下次请求间隔 | Medium | [Verified] protocol P5 | **声明不支持**（SSF/CAEP/SAML/SCIM 平台能力声明为不支持，保留）；触发条件：合规要求实时撤销时重评 |
| N8 | 备份过期（07-29 止，WAL 写至 08-04，热拷无一致性、无恢复演练）；生产二进制脏树 | Critical | [Verified] devops C1/H2 实机 | git 基线（M4 前置提前至 M1 前）+ 备份自动化/RTO/RPO（owner #8） |

## 9. 不接清单（保留，11 项 + 补充触发条件）

aero-im 全量 IM、vault RAG/MCP/WebDAV、aero-id 账户生命周期、ERP 多租户数据模型、替换 outbox（触发条件：governance 提供事务性中继时重评）、gRPC、OTel 替代审计、SSF/CAEP/SAML/SCIM 接入（声明不支持，触发条件见 N7）等——克制原则落位 [Proposed，kickoff/architect 认可]。

## 10. 证据等级总表（关键结论速查）

| 结论 | 等级 |
|---|---|
| B1–B5 阻塞项（人工复核） | [Verified]（04 号证据） |
| 31 个 TS 错误（含 audit/ 4 个、sso-client 声明解析） | [Verified]（五方复现） |
| governance `go test ./...` 全绿、仓库本机存在 | [Verified]（backend + protocol 实测） |
| 409 Conflict 语义（`service.go:371-379`）、hasField 6 字段特判 | [Verified] |
| `AUDIT_ALLOW_DEV_AUTH` 默认 true、id_token 不校验 | [Verified] |
| demo 伪造全量管理员、未认证写放大 | [Verified]（运行时复现） |
| presign get/put 现成、aero-id 无 org 域、无周期 drainer | [Verified]（architect 源码核验） |
| schemaVersion 兼容值、`occurred_at` 格式裁定、租户映射值 | [Unknown] → M0/owner 决策回答 |
| 五步路径、4xx permanent、idempotency_key=event.id、owner 责任人 | [Proposed]（本文件，待批准） |

## 11. 人工批准检查单

1. **任命责任人**：§6 八项（Proposed → 具名）；N1–N3 修复负责人具名
2. **启动 M0**：契约对拍结果回读（400 断言 = 字段清单；schemaVersion/occurred_at 兼容值由数据回答 [Unknown] → [Verified]）
3. **顺序执行**：M0 → M1 门禁绿 → M2 E2E 全绿 → 宣布 L2 → M3/M4/M5；任一硬门槛不绿即停
4. **保留项**：§7 清单逐项确认继承；§9 不接清单无新增
5. **文档纪律**：本文件批准后提升到 `decisions/` 与 feature 文档；kickoff 原文继续保留为决策证据；证据 01/02 的降级标注以 04 号归一为准
6. **门禁不豁免**：本路线图不豁免 `make ci`、`pnpm check`、governance 测试等任何仓库硬门禁；一切产物按证据标准核对后提升

---
*本文件为 revision 阶段交付物（平台架构顾问）。性质：提案。未经人工批准不进入任何仓库门禁豁免。*
