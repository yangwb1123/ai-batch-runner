# Gate 裁决 — ERP（sverpweb-stable）接入平台六件套场景分析

> 独立裁决者核验（2026-08-06）。亲自复核：`sverpweb-stable` 源码（`forwarding.ts`/
> `request-middleware.ts`/`contracts.ts`/`env.ts`）、`snaplink-audit-governance`
> （`server.go:76-77`、`models.go:360-387`）、`data/app.sqlite` 表清单、`.git` 状态。

## VERDICT: FAIL - 提案中心论断被证据推翻，五项阻塞项无一解决或驳回

### 阻塞项（全部经独立复核，无一被带证据驳回）

| # | 阻塞项 | 复核证据 |
|---|---|---|
| B1 | L2 契约不兼容：ERP 默认 `AUDIT_INGEST_PATH=/api/v1/audit/events` vs governance `POST /api/v1/events:batch`；`ValidateBasic` 必填 9 字段在 `newAuditEvent` 产物中全部缺失；还要求已注册 active schema。设 env 即全批 400 → outbox failed 积压 | `env.ts:76`、`server.go:76-77`、`models.go:360-387`、`contracts.ts` newAuditEvent |
| B2 | 回退审计事件 actor 丢失：`request-middleware.ts:115` `withActor` 作用于 AuditEvent 而非内嵌 OperationContext，`operation.actorUserId` 恒 null（亦是 31 个 TS 错误之一） | `request-middleware.ts` 源码走查、`contracts.ts:171` withActor 签名 |
| B3 | 门禁红（pnpm check 31 错误，QA/devops/backend 三方复现）+ forwarding 激活路径零测试（`apps/api/src/audit/` 无 `forwarding.test.ts`） | 目录清单实测 |
| B4 | 生产零审计事实：部署快照 `data/app.sqlite` 无 `audit_outbox` 表；ERP 无 purge、governance retention 只读报告无执行器 → Not audit-ready | sqlite3 .tables 实测 |
| B5 | 工程运营前提缺失：`.git` 空目录（无 HEAD/objects）、生产二进制脏树+dirty、无 CI、备份过期（07-29 止）、governance 认证凭据（OIDC claims + audit:event:write）、租户映射等 8 项 owner 决策待决 | `.git/` 实测、devops 实机核验 |

### 逐项裁定

1. **证据支撑**：评审自身证据纪律优秀，但 kickoff P0"零代码、设置 env 即激活"被证伪且文档未修订；证据 01/02 需降级（载荷吻合→Partial、provenance 失实）未回写。
2. **组合纪律**：大体遵守（真源裁定成立、单仓适配器选项 A、审计异步、11 项不接清单）。例外：governance 认证凭据方案与租户映射未决。
3. **路线图可执行**：M0-M5 形状齐备，但 M1 验收（pnpm check 绿）、M2 验收（governance 实例 + 租户映射）、M4 前提（git 初始化）当前不可达。
4. **无过度设计**：满足（11 项不接清单带触发条件，architect 认可并补充 1 条）。
5. **阻塞项解决/驳回**：B1-B5 全部未解决、无一被带证据驳回（反而被 2-4 个独立评审交叉确认）。

### 采纳项（修订时必须保留）

- 选项 A（ERP 侧 transport 适配器 + governance schema 注册，`AUDIT_APP_URL` 留空可逆回退 L1）
- M0 契约对拍（golden payload）先行；M2 账本 E2E 全绿才宣布 L2
- backend B1（4xx 分类 permanent + `[permanent]` 标记，不新增状态）、B3（`idempotency_key = event.id`，不得映射 Idempotency-Key 头，409 语义）、§3.7 hasField 陷阱（每 event_type 一条 schema，~22 条）
- B2 精确修复（withActor 作用于 operation）+ 先红后绿测试
- H2 重排：aero-id 组织只读投影降为"契约定义+预留"
- M2 媒体迁移改用 presign PUT/GET 直传直连（BFF 不缓冲大文件），禁双写

### 修订要求（修订后重新对抗审查再裁决）

1. kickoff 回写：P0 改为"零架构改动 + 契约适配器 + schema 注册 + 门禁修复"；证据 01/02 条目降级
2. P1 aero-id 项降级；组织上下文数据来源显式化（Snaplink userinfo 字段，停止 20 字段启发式猜测）
3. 下一 gate 硬门槛：M0 契约对拍 → M1（pnpm check 绿 + forwarding 6 类测试 + 回退 actor 断言）→ M2 E2E 全绿
4. 8 项 owner 决策（租户映射、schema 治理主体、aero-id org 路线、vault 合规约束、保留期、通知归属、iris-ui 门禁细则、governance 硬化负责人）附责任人
5. git 初始化 + CI 纳入 M4 前置（否则 campaign 指纹/断点续跑不可用）

本裁决不批准任何仓库门禁豁免。提案状态维持 Proposed。
