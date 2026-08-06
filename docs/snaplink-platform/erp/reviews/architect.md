Verification complete. I have everything needed for the review. All major claims have been checked against source; two evidence assertions are contradicted by actual code, which changes the P0/P1 calculus. Here is the architecture review.

---

# Architecture Review — ERP（sverpweb-stable）渐进式接入平台六件套

## 1. 范围、假设与已验证架构摘要

**范围**：评估 sverpweb-stable（Vue3 + iris-ui + Hono BFF + Bun/SQLite）与平台六件套（snaplink / snaplink-console / snaplink-audit-governance / aero-id / aero-im / aero-vault）的组合路线图。输入为 kickoff 交付物 + 证据 01/02/03；仅做架构评估，不实现代码。

**本次实际运行的核验**（Verified，与 kickoff/证据独立对照）：
- ERP 侧：`apps/api/src/audit/forwarding.ts`、`client.ts`、`sqlite-outbox.ts`、`contracts.ts`、`request-middleware.ts`、`app.ts`（wiring 段）、`config/env.ts`、`media/store.ts` + `routes/media.ts`、`routes/audit-admin.ts`、`.env`/`.env.example`/`deploy/*.service`
- governance 侧：`internal/httpapi/server.go`（路由注册 + `postEvent`/`postBatch`）、`internal/domain/models.go`（`ValidateBasic`）、`internal/service/service.go`（`Ingest`/`validateEvent` schema 门）
- aero-vault：`internal/api/rest/presign.go`、`router.go`（presign get/put、multipart、thumbnail、version）
- aero-id：`internal/domain/` 全量文件名扫描 + `internal/handler/` 关键字扫描
- 未重跑 `pnpm check` 与 governance 测试套件（仓库门禁，本次仅静态核验）

**架构摘要（Verified）**：

```
浏览器 → Hono BFF（认证代理 + 能力聚合 + 审计埋点中间件）
           ├─ snaplink OIDC（userinfo/权限/角色/菜单）        [已接入，信任根]
           ├─ 审计 outbox（SQLite，pending→publishing→published/failed，
           │     lease + 批量 50 + 指数退避，AUDIT_APP_URL 为空 → 纯本地）  [已接入，L1]
           ├─ 媒体本地 SQLite+文件（MediaStore 接口，getContent 全量载入内存） [L0]
           └─ aero-id/aero-im/aero-vault                         [未接入]
```

- 分层职责清晰：BFF 是唯一服务端入口（browserOriginGuard + 权限中间件），业务路由全部经 audit 中间件，领域事件用 `emitAuditEvent` 显式补充且避免与 HTTP 通用事件双计（Verified `request-middleware.ts`）。
- outbox 工程质量高：WAL、幂等 append（`ON CONFLICT(id) DO NOTHING`）、lease 过期回收、重试退避 2^n 封顶 5min、错误截断 2000 字符、库文件 0600、审计载荷刻意不含凭据（Verified）。
- 组合纪律与 kickoff 的"outbox=投递真源、governance=审计事实真源"裁定成立：2xx 后 outbox 退为运维语义，业务永不读 outbox 当事实——与 governance 去重语义（按 event_id + 内容摘要）兼容。

## 2. 发现表（按严重度排序）

| # | 严重度 | 发现 | 证据 | 影响 | 建议 |
|---|---|---|---|---|---|
| H1 | **High** | **governance ingest 契约与 ERP 载荷不兼容，直接设 env 会被整体拒收**——kickoff P0"最大杠杆、零代码"不成立 | Verified：governance 路由是 `POST /api/v1/events` 与 `/api/v1/events:batch`（`server.go:76-77`），ERP 默认 `AUDIT_INGEST_PATH=/api/v1/audit/events`（`env.ts`/`.env.example`）；`ValidateBasic` 强制 `event_id`/`source_system`/`schema_id`/`outcome`/`data_classification`/`retention_class`/`idempotency_key`/`actor.id`/`payload|payload_ref`（`models.go:366-387`），ERP 事件只有 `event_type`/`action`/`aggregate_*`/`schema_version`/`occurred_at` 能对上；`operation{}` 嵌套对象映射不到扁平 `operation_id`/`trace_id`/`actor`；`source`/`sentAt` 在信封层被 `batchRequest{events,wait_for}` 静默丢弃；`validateEvent` 还要求**已注册的 active schema**（`service.go:690-713`） | 按 kickoff 描述部署 → 每批 400 → outbox 全部 `failed` 永久重试，账本零事实；"L2=部署规则"缺三块：路径、字段映射、schema 注册 | 先做契约测试（golden payload 对拍），再建 ERP 侧 transport 适配器（选项 A，见 §3）；证据 02"载荷格式吻合"降级为 **Partial**（仅去重语义吻合） |
| H2 | **High** | **aero-id 当前代码无 organization/department/position/membership 域**——证据 02"拥有"不成立，P1"组织只读投影"没有上游可投影 | Verified：`aero-id/internal/domain/` 仅 account/file/comment/message/notification/…，无 org 相关文件；`internal/handler/` 无相关路由；`DECISIONS.md:105` 的 `account_memberships` 是 workspace/bucket/tenant 作用域，非组织树。ERP 侧 `DepartmentSnapshot` 可空预留位真实存在（`contracts.ts`），但现网部门数据来自对 Snaplink userinfo 的 ~20 个字段名的启发式猜测（`request-middleware.ts` `toDepartmentSnapshot`，脆弱、现网几乎为空） | P1 项建立在 Proposed 能力上；提前 wiring 会在 aero-id 无契约时反复返工 | 保留 ERP 侧可空占位（已做对），P1 重排为"与 aero-id 所有者定义 org 投影契约"，不实现即接线；部门数据来源问题单独跟踪 |
| M1 | Medium | **无周期 drainer**：publisher 只在 append 触发 + 启动 flush；审计应用宕机且低流量时，failed 事件要等下一次变更请求才重试 | Verified：`forwarding.ts` 注释明示"dedicated worker can still call AuditOutboxPublisher periodically"；`app.ts` 仅启动时 flush 一次；全树无 setInterval/cron | 长时间停机后审计事实延迟进入账本，与"审计是合规事实源"的定位冲突 | L2 启用前加调度 drain（进程内 lease 保护的定时 flush，或 systemd timer 调小脚本）；可复用现有 claimPending/lease 机制，改动极小 |
| M2 | Medium | **媒体读取全量载入内存**：`getContent` 返回 `Uint8Array`，上限 50MB，BFF 代理模式会整块缓冲 | Verified：`media/store.ts`、`routes/media.ts`（`MAX_MEDIA_FILE_SIZE = 50MB`） | vault 迁移若走 BFF 流式代理，大文件占用 BFF 内存；但 aero-vault 已有 **presign `op=get\|put` + expires + multipart + thumbnail + version**（Verified `presign.go`/`router.go`）——下载可直连、上传可直传，BFF 只签发 URL，根本不需要大文件过 BFF | 迁移模式定为：上传 = presign PUT 直传，下载 = presign GET 直连（或 302），BFF 仅代理元数据与权限校验。kickoff 未知点 #2 已解决 |
| M3 | Medium | **租户上下文缺失**：governance 多租户（tenant 路由/配额/投影），ERP 事件无 tenant 归属映射；`actorDepartmentId` 现网为 null | Verified：governance `Ingest` 按 claims+source 解析 tenant；ERP `OperationContext.actorDepartmentId` 默认 null | 账本查询/租户隔离/合规导出的正确性取决于 tenant 映射决策；部门维度审计分析现网为空 | 定义 ERP→tenant 映射（单租户默认值 vs 每公司一租户），事件带 `tenant_id` 由 governance claims 侧注入；审计事件组织上下文（P1）与租户决策绑定 |
| L1 | Low | schemaVersion 双方硬编码 1，governance 要求 schema 注册表（SchemaID+Version+RequiredFields） | Verified：ERP `newAuditEvent` 恒 `schemaVersion:1`；governance `validateEvent` schema 门 + `SchemaVersion<=0` 校验 | ERP 事件形态演进（新增字段）会与已注册 schema 冲突；governance 侧已有事件编码 Schema 兼容 ADR | adapter 设计时同步定义 ERP schema v1（含字段清单），版本升级走 governance schema 注册流程；kickoff 未知点 #1 即此，已查明 |
| L2 | Low | **证据链冲突**：证据 01/02 的"载荷格式吻合""aero-id 拥有 organization/department"被源码否定 | 见 H1/H2 证据 | 若下游（meta 对抗、gate）基于错误断言裁决，会批准错误方案 | 回写修正 01/02 相关条目为 Partial/Proposed；后续证据扫描对"接口路径+必填字段"级别做对拍，不停留在"格式相似" |
| L3 | Low | 生产 `.env` **完全没有** AUDIT_* 键；systemd `EnvironmentFile` 是部署规则的正确落点 | Verified：`.env` 无 AUDIT 键、`.env.example:33-37` 有文档；`deploy/sverpweb-stable.service:12` 加载 EnvironmentFile | 好处：L2 天然是部署规则（与 kickoff 方向一致）；风险：规则内容必须含 `AUDIT_INGEST_PATH=/api/v1/events:batch` + `AUDIT_APP_TOKEN` + schema 注册，缺一即 H1 场景 | 把 L2 部署规则写成 deploy/README 核对清单（URL/路径/token/schema/验收 curl） |
| I1 | Info | 本地 `.env` 含 `SNAPLINK_CLIENT_SECRET` 明文 | Verified | demo 环境风险低；若该快照会分发给他人需轮换 | 提醒运维轮换即可，不进本路线图 |

**已验证的正面事实**（支撑"克制、不重造"）：outbox 状态机、幂等与去重语义、审计防双计、载荷脱敏纪律、MediaStore 适配器接口、vault presign 能力、menu-normalizer 白名单、权限通配符边界——这些是"只改配置/加薄适配器"路线的底气。

## 3. 决策选项（L2 审计接入路径）

| 选项 | 方案 | 优点 | 缺点 | 判定 |
|---|---|---|---|---|
| **A（首选）** | ERP 侧新 `AuditApplicationTransport` 适配器（如 `apps/api/src/audit/governance-transport.ts`）：字段映射（`id→event_id`、信封 `source→source_system`、`operation.*`→扁平字段、`metadata.outcome→outcome`、派生 `idempotency_key=operationId`、`payload={before,after,metadata}`、补 `data_classification`/`retention_class` 常量）+ governance 侧注册 ERP schema v1（`POST /api/v1/schemas`，admin 一次性操作） | 单仓改动；复用全部现成机制（lease/批量/去重/退避）；`AUDIT_APP_URL` 留空即回退 L1，**可逆**；契约由 golden 测试钉死 | ERP 侧需维护字段映射知识（靠契约测试防漂移）；schema 注册是带外 admin 操作 | **推荐**。试运行门槛：账本 E2E（ERP 操作 → outbox → governance → 查询/console 时间线）全绿后才宣布 L2 |
| B | governance 侧新增兼容端点 `/api/v1/audit/events`（接受 ERP 信封、隐式映射+隐式 schema） | ERP 零改动 | 跨仓契约变更（违反组合纪律"改变双方契约必须套件验证"）；governance 是设计期参考实现（19 测试、文件存储），过早为单一客户端开兼容面 | 否决（现阶段） |
| C | 停留 L1（本地 outbox）+ 仅补契约测试 | 零风险 | 无统一账本；audit-admin 仍是单系统视图 | 作为 A 的**阶段 0 前身**，不是替代（A 的适配器可以先建、不启用，天然等于 C+） |

**P1 重排建议**（因 H2 修正）：
- 媒体 BFF 迁移：维持（MediaStore 接口 + presign 直连，懒迁移 + 回填、禁双写）——**不受 H2 影响，可独立推进**
- aero-id 组织只读投影：从"接入"降为"契约定义 + 预留"，等待 aero-id 侧路线（owner 决策，见 §5）
- 审计事件组织上下文 + 事件注册：维持，但组织上下文的**数据来源**先以 Snaplink userinfo 字段显式化（不再启发式猜测 20 个名字）或等 aero-id 契约
- aero-im 通知：维持 best_effort_async（不阻塞业务，与审计 failure_policy 对齐）

**不接清单**（11 项）：认可，无修改建议；补充一条触发条件——"替换 outbox"仅在 governance 提供事务性中继（当前无）时重新评估。

## 4. 优先级实施序列（里程碑 + 兼容 + 风险 + 可执行验收）

| 里程碑 | 内容 | 兼容计划 | 风险 | 可执行验收检查 |
|---|---|---|---|---|
| **M0 契约钉死**（本周） | golden payload 对拍：用真实 ERP 事件样本 POST 到 governance 测试实例 | 无生产影响 | 低 | `curl -X POST <gov>/api/v1/events:batch -d @erp-sample.json` 预期 400 且错误列出缺失字段 → 记入契约测试，断言列表 = H1 字段清单 |
| **M1 适配器 + schema**（试运行，**不启用**） | 新建 transport 适配器 + 契约单测（vitest，仿 `client.test.ts` 模式）；governance 注册 ERP schema v1；`AUDIT_APP_URL` 仍留空 | 零生产影响；schema 注册只读叠加 | 低-中 | `pnpm check` 绿；契约测试断言映射后载荷通过 `ValidateBasic` 全部字段；`curl <gov>/api/v1/schemas` 可见 ERP schema |
| **M2 L2 试运行**（部署规则） | 设置 `AUDIT_APP_URL`/`AUDIT_INGEST_PATH=/api/v1/events:batch`/`AUDIT_APP_TOKEN`；加周期 drainer（M1 前合并亦可）；deploy/README 核对清单 | 回退 = 清空 env + 重启，outbox 数据无损 | **高**：H1 场景（拒收→failed 积压） | 账本 E2E 门禁：1) 生产操作 3 类（建单/审批/上传）→ 2) governance `GET /api/v1/events?source_system=sverp-web` 命中且 `occurred_at`/`actor.id` 正确 → 3) outbox `published` 占比 100% 且 24h 后无 `failed` 积压 → 4) console 审计页可查。**全绿才宣布 L2**，与 kickoff 门槛一致 |
| **M3 媒体迁移**（独立并行） | presign PUT 上传 + presign GET 下载适配器（`MediaStore` 新实现）；懒迁移 + 回填 job；旧文件只读→下线 | 前端零改动（接口不变）；迁移期双读（vault 优先、本地回退），**禁双写** | 中：回填中断、权限模型差异（vault bucket policy vs ERP 权限） | 上传/下载/缩略图 3 条 E2E 走 vault；回填 job 幂等（按 key 对账）；50MB 上限文件直传不落 BFF 内存（压测断言） |
| **M4 运营嵌入** | 周期 drainer（若未并入 M2）；platform-status/247 `PLATFORM_REPOS` +第 7 仓；pi-batch 嵌入仅分析面（campaign + 提案流水线，`git_commit:false`） | 不动生产代码 | 低 | `platform-status.sh` 7 仓全绿；drainer 在"审计应用停机 10min → 恢复"场景 5min 内清空积压 |
| **M5 通知 + 事件注册** | aero-im best_effort_async 通知（审批/异常/任务分派）；审计事件类型注册表文档化 | 通知失败不影响业务（对齐 failure_policy） | 低-中：aero-im 跨主机项"待验证"（NAT/TURN/供应商凭据） | 审批触发通知 E2E；通知服务宕机时业务操作无感知、无重试风暴 |

**跨里程碑风险**：aero-id org 契约（H2，阻塞 P1 之一但不阻塞主线）；governance 生产硬化进度（L1 试运行门槛的验收人/时间未定）；ERP 无 git 仓库（当前是快照目录，`git log` 失败——M4 嵌入 pi-batch campaign 前需先初始化版本管理，否则指纹/断点续跑不可用）。

## 5. 需 owner/产品决策的未知项

1. **租户映射**（阻塞 M2 验收标准 2）：ERP 事件进 governance 哪个 tenant？单租户默认值即可，还是每公司一租户？——决定账本查询与配额语义。
2. **ERP 审计 schema 的治理主体**：schema v1 字段清单由谁批准？升级流程走 governance 的 schema 注册（版本兼容 ADR）是否够？
3. **aero-id organization/department 域的路线**（H2 直接相关）：是否在 aero-id 路线图内？预计形态（gRPC 投影 API？）？——决定 P1"组织只读投影"是等待还是降级为不接。
4. **vault 迁移的合规约束**：质检图片/报价附件的保留策略与 Legal Hold 是否适用（governance retention 与 vault retention 的关系）？
5. **审计保留期**：governance retention policy 的具体值（默认/合规要求）。
6. **通知模板/偏好归属**：ERP 业务文案在 aero-im template 还是在 ERP 侧拼接？通知偏好 UI 放 ERP 管理页还是 console？
7. **iris-ui 升级门禁细则**：0.2.x 锁版本 + 升级门禁的触发条件（安全修复/新组件需求）谁定？
8. **governance 生产硬化里程碑**：L1 试运行门槛（M2 验收）的负责人与时间点；硬化的验收标准（多节点/持久存储/性能基准）是否纳入。

**kickoff 三个待核点的裁定**：#1 schemaVersion 兼容 → **已查明**，问题深于 schemaVersion（H1 全字段不兼容），M0/M1 处理；#2 vault 预签名 → **已解决**，presign get/put 现成（`aero-vault/internal/api/rest/presign.go`），BFF 代理非必须；#3 governance 硬化进度 → **确认**设计期+参考实现（文件存储、单节点），L1 试运行门槛维持。

**结论**：方向正确（outbox 机制、克制清单、L2=部署规则），但 P0"零代码"需修正为"零架构改动 + 一个契约适配器 + 一次 schema 注册"；P1 中 aero-id 组织项基于不存在的上游能力，须重排。建议按 M0→M2 推进审计主线（契约测试先行），媒体迁移并行，aero-id 项冻结等待 owner 决策。本评审为咨询性分析，不批准任何仓库门禁豁免；全部证据等级与核验范围见 §1。
