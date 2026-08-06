# Compliance Review — sverpweb-stable（ERP）审计/数据合规面

> 合规评审（咨询性）。依据 `prompts/README.md`：仅评估输入所展示的法规、
> 合同控制与保证框架；未建立管辖域/分类/框架适用性的地方一律标注 Unknown。
> **实现的控制不等于认证证据**；本评审不构成法律意见、合规声明或认证。

## 1. 适用性范围声明（Applicable Scope）

**评审对象**：`/home/dwp/workspace/job/sverpweb-stable`（Vue3 + Hono BFF + Bun/SQLite，
部署 `192.168.123.52:13014` 等）及其审计链（本地 outbox → 可选 governance 账本），
以及规划中的 aero-id / aero-im / aero-vault 集成。

**本次实际运行的核验（Verified，与 kickoff/architect/qa_lead 独立对照）**：
- `apps/api/src/audit/`：`forwarding.ts`（激活分支）、`client.ts`（信封/认证头/超时）、
  `sqlite-outbox.ts`（WAL/0600/幂等/lease/退避）、`contracts.ts`（事件模型、
  `withActor`）、`request-middleware.ts`（回退事件路径）
- `apps/api/src/config/env.ts`、`.env`（无 AUDIT_* 键，含明文密钥）、`.env.example`
- `apps/api/src/app.ts`（wiring、启动 flush）、`routes/audit-admin.ts`、
  `http/middleware.ts`、`http/cookies.ts`、`auth/sqlite-stores.ts`（AES-256-GCM）、
  `media/store.ts` + `routes/media.ts`、`routes/quality-inspections.ts`、
  `docs/audit-integration.md`
- 数据面：`data/app.sqlite`（全部表、`audit_outbox` 缺席）、`data/auth.sqlite`
  （auth_records）、`data/quality-inspection-files/`
- governance 侧：`internal/httpapi/server.go`（路由/认证）、`internal/domain/models.go`
  （`ValidateBasic` 必填清单、RetentionPolicy/LegalHold）、`internal/service/service.go`
  （`EvaluateRetention`、`SetRetentionPolicy`、tenant 解析）
- 部署面：`deploy/sverpweb-stable.service`、`deploy/README.md`
- 未重跑 `pnpm check`/governance 测试套件（QA 已实测 `pnpm check` 红：31 个 TS 错误）

### 框架适用性

| 维度 | 状态 | 说明 |
|---|---|---|
| 司法管辖域（jurisdiction） | **Unknown** | 输入未确立。系统为中文 ERP、私有 LAN 部署；若属中华人民共和国境内运营，可能触发《个人信息保护法》《数据安全法》《网络安全法》/等保 2.0——**须由 owner 书面确认管辖域后另行评估** |
| 数据分类（classification） | **Unknown** | ERP 事件模型无分类字段（见 §3 R6）；快照数据含订单/客户号/定价（疑似商业敏感）与质检图片（内容不明，可能含人员影像），均未标注分类 |
| 合同控制（contractual） | **Unknown** | 无客户合同、DPA、审计条款输入；不得推断存在合同义务 |
| 保证框架（assurance） | **Unknown** | 无 SOC/ISO/等保等级承诺输入 |
| 系统自声明义务（in-scope，可评估） | **适用** | ①`docs/audit-integration.md` 审计契约（投递真源/去重/防双计）；②governance 账本契约（schema 注册、retention policy、legal hold、导出）；③`deploy/README.md` 部署契约（BFF 唯一入口、secret 仅存受保护文件）；④本仓库 `AGENTS.md` 硬门禁 |

### 排除项
- aero-id/aero-im/aero-vault 未接入（无代码路径），其自身合规属性不评估；
- 前端 `apps/web` 与 iris-ui 不在本评审范围（无服务端数据面）；
- 法律建议、认证判定：明确不做。

---

## 2. 控制矩阵（Control Matrix）

图例：✅ Implemented（源码实证）/ ⚠️ Partial / ❌ Missing / ❓ Unknown / 🔴 Broken（已实证缺陷）

| # | 控制要求 | 状态 | 仓库证据 | 流程证据 | 缺口 | Owner | 验证方法 |
|---|---|---|---|---|---|---|---|
| R1 | 变更留痕（写操作产生审计事件） | ⚠️ | `request-middleware.ts` 通用中间件 + 24 处 `emitAuditEvent`（aps/quality-inspections/bs-pricing/contents/system-routes/media/auth）；**但回退路径 actor 丢失（F-1）** | 快照 `data/app.sqlite` **无 `audit_outbox` 表、零审计事件**——当前部署未物化任何通用审计记录 | 审计事实缺失于运行态；回退事件无法回答"谁做的" | 后端 owner | `sqlite3 data/app.sqlite ".tables"` 应见 `audit_outbox` 且有行；回退路径断言测试（QA 退出标准 #3） |
| R2 | 审计事件投递到统一账本（L2） | ❌ | `forwarding.ts`：`AUDIT_APP_URL` 空 → publisher=null；`.env` 无 AUDIT_* 键 | 部署规则未写（`deploy/README.md` 无激活清单） | **契约不兼容（F-2）**：路径 `/api/v1/audit/events` vs governance `/api/v1/events:batch`；`ValidateBasic` 必填 10 字段（`data_classification`/`retention_class`/`schema_id`/`source_system`/`outcome`/`idempotency_key`/`actor.id` 等）ERP 事件全无 → 启用即整体拒收、failed 积压 | 后端 owner + governance owner | M0 golden 对拍 curl（预期 400 + 字段清单）；适配器 + schema 注册后契约测试 |
| R3 | 审计记录保留/删除策略 | ❌ | ERP 全树无 retention/purge/delete（`grep -rn retention` 仅命中 action 枚举）；outbox 记录永久累积；`before/after` 含业务载荷 | governance `EvaluateRetention` = 仅报告 `archive_only_immutable_ledger`，无删除执行器（`service.go:231-290`） | 无保留期、无分类驱动保留、无删除路径（若管辖域要求删除权则无法履行）；两侧无归档/导出作业 | 平台 owner + 合规 owner | 书面 retention schedule；ERP 侧分类常量（`data_classification`/`retention_class`）入 schema；归档 job 验收 |
| R4 | 法律保留（Legal Hold） | ❌ | governance 有 `LegalHold` 域（`models.go:193`）；**ERP 侧无任何 hold 钩子** | 无流程 | 源数据（outbox/媒体/业务库）无法执行 hold | 平台 owner | hold 场景 E2E（ERP 数据 + 账本同时冻结） |
| R5 | 访问控制（会话/权限/CSRF/来源） | ✅ | `requireSession`+`requirePermission` 逐路由；`browserOriginGuard` 非 GET 校验 Origin；`requireCSRF` 于全部变更；audit-admin 仅 `admin:read/write`（`routes/audit-admin.ts`） | 无定期权限复核记录（❓） | 权限复核/离职回收流程未见 | 运维 owner | 季度权限复核记录；`audit-admin` 越权测试 |
| R6 | 数据分类与最小化 | ⚠️ | 载荷纪律实证：审计载荷不含凭据/原始 body（`request-middleware.ts` 注释 + `media.ts` snapshot 白名单）；**无分类字段、无 records-of-processing**；媒体保留 `original_name`；`before/after` 可能含 `customer_no`/`order_no`/定价（商业敏感） | 无数据清单文档 | 分类方案缺失；质检图片内容未分类（可能含人员影像） | 数据 owner | 数据清单 + 分类标签落地；快照字段最小化评审 |
| R7 | 传输加密 | ❌ | 生产部署全 HTTP：app `:13014`、idP `:13015`、console `:13017`（`deploy/README.md`）；`COOKIE_SECURE=false`；`SNAPLINK_ALLOW_INSECURE_HTTP=true`（`.env`） | 无 TLS 终止配置 | 会话 cookie、OAuth 凭据、审计事件（含业务载荷）明文过 LAN；审计事件投递到 governance 亦明文 | 运维 owner | TLS 终止 + `COOKIE_SECURE=true` + `allowInsecureHttp=false` + 部署核对清单项 |
| R8 | 静态加密与密钥管理 | ⚠️ | 会话记录 AES-256-GCM（`auth/sqlite-stores.ts`，iv+tag 打包）；outbox 文件 0600/0700、审计载荷脱敏 | `SESSION_ENCRYPTION_KEY` 明文 `.env`（0600，与 DB 同主机）；**快照 `.env` 含活体密钥**：`SNAPLINK_CLIENT_SECRET`、`DEMO_PASSWORD`、会话密钥（F-4）；无轮换流程 | 密钥生命周期管理缺失；快照分发即泄露面 | 运维 owner | 密钥托管/受保护 env 文件 + 轮换 runbook；快照脱敏或轮换 |
| R9 | 监控与告警（投递失败可见性） | ❌ | 仅 `console.warn/error`（`forwarding.ts`/`request-middleware.ts`）；无 drainer（`client.ts` 注释明示由外部调度）；无指标暴露 | 无日志采集/告警配置证据；无 IR 计划 | 审计应用停机 + 低流量时 failed 积压不可见；无事件响应入口 | 运维 owner | 周期 drainer + `countPending` 指标告警；停机恢复演练（architect M4 验收） |
| R10 | 备份/恢复 | ❓ | SQLite 数据在 `shared/data`（`deploy/README.md`），原子发布不替换数据 | **无备份/恢复文档** | 无 RPO/RTO、无演练证据 | 运维 owner | 备份 job + 恢复演练记录 |
| R11 | 供应商/第三方 | ⚠️ | Snaplink idP 是信任根（OIDC userinfo 权限）；`@snaplink/sso-client` 为仓库外 file: 依赖、构建时打包（`deploy/README.md`）；规划 aero-* 三件套 | 无供应商风险评估/DPA 证据 | 信任根供应链无记录 | 平台 owner | 供应商清单 + 风险评估；锁版本 + 升级门禁（kickoff 既定） |
| R12 | 测试与验证门禁 | 🔴 | QA 实测 `pnpm check` 红（31 个 TS 错误，含 `request-middleware.ts`）；`forwarding.test.ts` 不存在（激活路径零测试，F-3）；E2E 默认 CI 跳过（手动 `SNAPLINK_E2E=1` 可跑）；仓库无 git、无 CI（`.git` 空目录） | 证据 01 声称的 git log/vitest/无 campaign 与现状不符（QA F9） | 审计代码本身的正确性无门禁保证；保证程序证据链不实 | 工程 owner | QA 退出标准 #1–#6 全项；`make ci` 等效门禁接入 |

---

## 3. Findings（按严重度）

### F-1 [High] 回退审计事件丢失 actor 归属——审计痕迹无法回答"谁做的"
- **证据（Verified）**：`request-middleware.ts:130-165` 回退路径把 `withActor(...)` 作用于 `newAuditEvent(...)` 的结果（`AuditEvent`）而非 `OperationContext`——actor 字段被展开到事件顶层（运行时被序列化为多余顶层字段），`operation.actorUserId/actorDepartmentId` 保持 null；同时传入 `newAuditEvent` 的 `actorDepartmentId/actorDepartmentSnapshot` 属多余属性（运行时忽略，TS 报错——构成 `pnpm check` 31 错误之一）。`sqlite-outbox.ts` 的 `actor_user_id` 列取 `event.operation.actorUserId` → 回退事件全部为 NULL。`emitAuditEvent` 路径（24 处领域事件）无此缺陷，但回退事件覆盖所有未显式埋点路由。
- **业务/合规风险**：通用 HTTP 审计记录是访问问责的基线；actor 缺失使"谁在何时对什么做了什么"不可答，直接影响 R1 与任何管辖域下的访问日志义务；审计管理员按 `actorUserId` 过滤将静默漏查。
- **整改**：先 `withActor` 于 operation 再构造事件；回退路径单测断言 `operation.actorUserId` 非空（QA 退出标准 #3 已要求，当前必失败）。
- **关闭证据**：`request-middleware.test.ts` 新增回退路径 actor 断言用例转绿；`pnpm check` 转绿。

### F-2 [High] L2 契约不兼容——"启用即拒收"，审计事实源为空
- **证据（Verified）**：见 §2 R2 与 architect H1（路径、信封、`ValidateBasic` 10 必填字段、schema 注册门 `service.go:690-713` 均实证）。`ValidateBasic` 要求的 `data_classification`/`retention_class`/`schema_id`/`source_system`/`outcome`/`idempotency_key`/`actor.id` 在 ERP 事件模型中**全部不存在**（`contracts.ts` 全字段核对）。
- **业务/合规风险**：按 kickoff 的"设 env 即 L2"部署 → 每批 400 → outbox 永久 failed → 账本零事实。宣称的"审计事实真源"为空，任何基于账本的合规查询/导出均无数据；同时产生"已接入"的假象（实现 ≠ 证据）。
- **整改**：architect 选项 A（ERP 侧 transport 适配器 + governance schema v1 注册）+ M0 golden 对拍先行；`AUDIT_APP_URL` 保持空直至账本 E2E 全绿。
- **关闭证据**：契约测试断言映射后载荷通过 `ValidateBasic` 全字段；账本 E2E（建单/审批/上传 → 查询命中 → outbox 100% published → 24h 无 failed）。

### F-3 [High] 审计事件保留/删除义务两侧均无实现——最小化与删除权无法履行
- **证据（Verified）**：ERP 侧无任何保留/清理/删除路径（全文检索仅命中 action 枚举）；governance 侧 `EvaluateRetention` 为只读报告（`archive_only_immutable_ledger`），无执行器。`LegalHold` 仅存在于 governance 域。快照 `data/app.sqlite` 中 `audit_outbox` 表**不存在**（零记录）——当前部署连本地审计事实都没有物化。
- **业务/合规风险**：无法证明保留期符合任何既定策略（策略也不存在）；若管辖域赋予数据主体删除权/更正权，无任何履行通道；`before/after` 中的订单/客户号/定价无限期留存。
- **整改**：①书面 retention schedule（含各事件类保留期）；②ERP 事件补 `data_classification`/`retention_class` 常量（同时满足 F-2 的 schema）；③定义归档/清理作业（governance 只读账本可归档不可删，需明确"归档"语义并落为可执行 job，而非仅报告）；④法律保留流程 owner。
- **关闭证据**：保留策略文档 + 分类常量进 schema + 归档 job 演练记录 + 删除/导出 E2E。

### F-4 [Medium] 传输链路全明文 + 密钥明文旁置——加密在途/密钥管理不达标
- **证据（Verified）**：`deploy/README.md` 三服务全 HTTP；`.env` 中 `COOKIE_SECURE=false`、`SNAPLINK_ALLOW_INSECURE_HTTP=true`；会话 cookie `httpOnly+SameSite=Lax` 但非 Secure（`cookies.ts`）；`SESSION_ENCRYPTION_KEY` 与 `SNAPLINK_CLIENT_SECRET` 明文存于快照 `.env`（0600，快照含活体密钥，I1 合并至此）。
- **业务/合规风险**：OAuth 凭据、会话 cookie、审计事件（业务载荷）明文过 LAN——被动嗅探即可获得；密钥与密文同主机同文件树，静态加密的实际防护被削弱；快照外流 = 凭据泄露。
- **整改**：TLS 终止（反代）覆盖 :13014/:13015/:13017；`COOKIE_SECURE=true`；`SNAPLINK_ALLOW_INSECURE_HTTP=false`；密钥移入受保护 env/托管 + 轮换 runbook；快照脱敏。
- **关闭证据**：部署清单核对项 + 抓包验证无明文凭据 + 轮换演练记录。

### F-5 [Medium] 投递失败无监控、无 drainer、无 IR——审计连续性与事件响应缺口
- **证据（Verified）**：`client.ts:109` 注释明示不内置定时器；`app.ts:147-149` 仅启动 flush 一次；告警仅 `console.warn`；无指标/日志采集/IR 文档；`deploy/` 无备份/恢复文档（R9/R10）。
- **业务/合规风险**：审计应用停机且低流量 → failed 事件滞留至下次业务请求；长时间"审计空窗"不可见、不可恢复；任何"审计连续覆盖"的保证均不可证明。
- **整改**：进程内 lease 保护周期 drainer（复用 `claimPending`，改动极小）或 systemd timer；`countPending` 指标 + 告警；IR/备份 runbook。
- **关闭证据**：停机 10min → 恢复 5min 内清空积压的演练记录（architect M4 验收）。

### F-6 [Medium] 保证程序自身的证据链不实——"已实施"声明与仓库现状不符
- **证据（Verified）**：QA F1/F7/F9——`pnpm check` 红（审计中间件本体在其中）、无 `forwarding.test.ts`、无 git、无 CI、证据 01 provenance 失实；本评审追加：`data/app.sqlite` 无 `audit_outbox` 表（零审计事实）。
- **业务/合规风险**：向监管/审计方出示的"控制已实施"证据若基于失实 provenance，属比缺陷更重的信任问题；门禁红的代码路径正是产生合规记录的路径。
- **整改**：证据 01 归一（QA 退出 #5）；git 初始化 + CI 接入（QA #6）；审计激活路径 6 类测试补齐（QA #2）。
- **关闭证据**：归一后的证据文档 + `pnpm check` 绿 + CI job 日志。

### F-7 [Low] 数据清单/分类缺失，质检图片内容未分类
- **证据（Verified）**：`qa_prod_check`（`customer_no`/`order_no`/`item_code`/审批者与检查者 subject id）、`qa_prod_check_file`（质检图片/扫描/PDF）、`media`（`original_name`、`uploaded_by`）、`bs_standard_pricing`（定价，商业敏感）、`contents`。快照量小（质检 1 条、media 2 条），生产规模 Unknown；无分类标签、无处理记录文档；质检图片内容是否含人员影像 Unknown。
- **整改**：数据清单 + 分类标签；图片内容抽查；`original_name` 最小化评估。
- **关闭证据**：records-of-processing 文档 + 抽查记录。

### F-8 [Info] 已实证的正面控制（维护基线，勿因 F-1..F-7 否定）
- outbox 工程纪律：WAL、幂等 append（`ON CONFLICT DO NOTHING`）、lease 过期回收、退避封顶、错误截断、文件 0600/目录 0700、载荷不含凭据（`sqlite-outbox.ts`/`client.ts`/`request-middleware.ts` 注释）。
- 会话静态加密 AES-256-GCM（iv+tag 打包，`sqlite-stores.ts`）。
- 访问控制三层（会话+权限+CSRF+Origin，`middleware.ts`）；audit-admin 仅 admin 角色可查且参数全部校验。
- systemd 加固（`ProtectSystem=strict`、`NoNewPrivileges`、`UMask=0077`、`ReadWritePaths` 收敛）。
- governance 侧不可变账本 + 去重（event_id+摘要）、法律保留域、admin 操作留痕（`models.go`）。
- 遗留域审计 `system_route_definition_audit`（46 条，含 actor）——可作为回退事件 actor 修复的对照实现。

---

## 4. 审计就绪度总结（Audit-Readiness）

### 当前状态
**Not audit-ready**。审计基础设施在代码层成形（控制设计良好），但运行态零事实
（无 outbox 表、无投递）、契约不兼容（L2 不可启用）、保留/删除/分类/加密在途/
监控/备份全缺，且保证程序自身门禁红、provenance 失实。**任何"已合规"表述均无
证据支撑**；合规就绪的可行里程碑与架构评审 M0→M5 一致，合规角度要求：

### 必办文档（当前缺失）
| 文档 | 内容 | 对应 Finding |
|---|---|---|
| 管辖域与适用法规确认书 | 司法域、是否含个保/等保义务、数据主体权利适用性 | §1 |
| 数据处理清单（records of processing） | 数据项/用途/法律依据/分类/共享方/保留期 | F-3/F-7 |
| 数据分类与保留策略 | 分类常量（含 ERP schema v1 的 `data_classification`/`retention_class`）、保留期表、归档/删除作业定义 | F-2/F-3 |
| 审计激活与运营核对清单 | URL/path/token/schema/验收 curl/回退条件（deploy/README 扩展） | F-2 |
| 密钥管理与轮换 runbook | 密钥清单/存放/轮换/泄露响应；快照脱敏 | F-4 |
| 监控告警与 IR 计划 | outbox 指标、告警阈值、停机恢复、备份/恢复（RPO/RTO） | F-5 |
| 权限复核记录 | 季度 admin 权限复核、离职回收 | R5 |
| 供应商评估 | Snaplink/aero-* 风险与责任边界（含 sso-client 供应链） | R11 |

### 待 owner 决策（阻塞项）
1. **管辖域确认**（决定 §1 全部框架适用性与 F-3 删除权是否触发）；
2. **租户映射**（ERP 事件进 governance 哪个 tenant——决定查询/导出/隔离正确性，architect 未知 #1）；
3. **保留期取值**（默认值/合规要求/法律保留流程 owner）；
4. **L2 go/no-go**：以 M0 契约对拍 → M1 适配器+schema → M2 试运行验收全绿为门槛；试运行期间 `.env` 保持无 AUDIT_* 键（现状即 C+ 阶段）；
5. **证据 01 归一**与 git/CI 运营前提（QA 退出 #5/#6）。

### 关闭证据链（每个 Finding 的验收）
见 §3 各条"关闭证据"；全部以**可执行命令/测试结果/演练记录**为据，
仓库存在 ≠ 已实施 ≠ 已认证。

---
*证据等级：Verified（本次源码/数据/部署实证）、Unknown（输入未确立）。本评审为
咨询性分析，不批准任何仓库门禁豁免；法律适用性结论须由具备管辖域资质的法律顾问作出。*
