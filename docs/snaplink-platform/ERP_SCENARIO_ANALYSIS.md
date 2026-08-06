# ERP 场景分析与建议 — sverpweb-stable + iris-ui 接入平台六件套

> 生成方式：ai-batch-runner 五轮对抗流水线（证据扫描 → 分析 → meta 对抗审查
> ×5 轮 → 裁决门 PASS）+ 人工证据复核。完整证据链见
> `docs/snaplink-platform/erp/`（kickoff / reviews / reviews2 / revision /
> gate / gate2）与决策日志 `decisions/erp-scenario.md`。
> 本文件所有结论标注证据等级；未核实项标 **Proposed**。路线图本身是提案，
> 批准要求见 §6。

## 0.1 实施进度（2026-08-06 更新，人工验证）

| 里程碑 | 状态 | 证据 |
|---|---|---|
| M1 门禁转绿：tsc 31→0 | ✅ **达成** | sverpweb-stable `bunx tsc --noEmit` 0 错；`pnpm check` 9/9 全绿（154 api pass + web 115 pass） |
| §2.2 终态机制（permanent + 毒丸隔离） | ✅ **达成** | permanent 列零重建迁移 + claim 排除；transport 逐事件 outcomes（202/409/4xx/5xx 分类 + event_id 交叉校验）；publisher 逐事件裁决；测试 +11（commit bf7f952） |
| B2 修复（withActor 类型误用） | ✅ **达成** | `request-middleware.ts` 改为先 `withActor(operation)` 再 `newAuditEvent`；新增 B2 回归测试（先红后绿：红态 actorUserId=null → 绿） |
| sso-client 声明解析（TS7016） | ✅ **达成** | snaplink `docs/sdks/typescript` v0.3.0：exports 指向 dist + 构建产物入库（commit d19838d8） |
| forwarding.test.ts 6 类用例 | ✅ **达成** | 8 个用例：激活×2 / 下游错误 / 并发单飞 / 文件重启 / 空批 / 同 id 去重 / 幂等重试（`apps/api/src/audit/forwarding.test.ts`） |
| M0 契约对拍 | ✅ **达成** | governance `internal/httpapi/erp_contract_test.go` 5 测试：202+Duplicate / 409×2 冲突分支 / 400 字段清单 / 批首错截断（commit 476d083） |
| M4 前置：git init + 首次提交 | ✅ **达成** | sverpweb-stable `git init` + 基线快照（commit d546647）——B5 修复 |
| M2 L2 试运行 | ⏳ 未开始 | 前置：governance 生产实例（owner #8 任命）+ schema 30 条注册 + 适配器 `governance-transport.ts` |

**本轮额外发现**：audit_outbox 表已由隐式迁移创建（0 行，无版本戳）；
governance 未注册 schema 返回 **422**（修订版 §2.2 permanent 分类已含）——
适配器实现时须把 422 归入 permanent。

## 0. 场景定位

- **平台六件套**（独立产品、标准协议组合，见 `COMBINATION_MODEL.md`）：
  snaplink（认证授权 IdP）、snaplink-console（管理控制面）、
  snaplink-audit-governance（审计事实源）、aero-id（账户域）、
  aero-vault（文件平台）、aero-im（消息/通知/直播）。
- **ERP 场景**：`~/workspace/job/sverpweb-stable`（Vue3 + iris-ui + Hono BFF，
  生产 ERP，渐进式移植）由 `~/workspace/job/iris-ui`（AI-native 跨框架 UI
  基础设施）构建，接入六件套能力。
- **组合纪律**：数据权威源唯一、同步依赖有向 ≤3 跳、审计异步不阻塞业务、
  默认只改当前仓库、L2 审计是部署规则、AI 产出是提案。

## 1. 现状盘点（Verified 证据扫描）

| 仓库 | 角色 | 现状 |
|---|---|---|
| sverpweb-stable | ERP 业务应用 | **已接 snaplink 认证**（@snaplink/sso-client PKCE/刷新轮换/菜单白名单映射/服务端权限边界）；**已有完整审计 Outbox 体系**（sqlite-outbox + HTTP publisher，AUDIT_APP_URL 可配置但未指向 governance）；媒体/附件本地 SQLite+文件；无 aero-im/aero-id/vault 接入；**tsc 31 错门禁红**；**无 git 仓库**（.git 空目录） |
| iris-ui | UI 基础设施 | npm 发布包 0.2.x（sverpweb 用 @iris-ui-kit/vue 0.2.18）；Alpha 期 154 组件 × 4 框架、12 插件、MCP/manifest AI 原生生态；**作为 npm 上游依赖管理（锁版本+升级门禁），不并入平台轮巡** |
| snaplink | 认证授权 | 最成熟；OIDC/OAuth2、admin API、gRPC、ssoclient 三态 |
| snaplink-audit-governance | 审计事实源 | **架构设计期+参考实现**；ingest `POST /api/v1/events:batch`、ValidateBasic 9 必填字段、逐事件 receipts（202 Duplicate / 409 两冲突分支）、EncryptedFields 能力现成未用 |
| aero-id / aero-vault / aero-im | 账户/文件/通知 | 已有跨产品组合先例（im 附件走 vault BlobStore + snaplink client_credentials；vault/id 已有 governance 连接器雏形） |

## 2. 核心建议（P0–P2 路线图，全部经五轮对抗验证）

### P0 审计 L2 接入（最大杠杆，但**不是**"设 env 即激活"）

初判"设置 AUDIT_APP_URL 即激活、零代码"已被对抗轮**证伪**（B1 实锤：
ingest 路径/字段契约/active schema 三缺一即全批 400，outbox 永久积压）。
正确路径（修订版 §2）：

1. **M0 契约对拍**（半天）：golden payload 打 governance 测试实例，400 断言
   列表 = 字段清单；`occurred_at` 唯一可行 **RFC3339**（Go 解码层拒 ms）。
2. **M1 适配器 + schema 注册**：`governance-transport.ts` 字段映射
   （AUDIT_APP_URL 留空不启用）；schema **30 条**（实测，早期枚举 22 低估 36%）
   + 发布门禁（新 event_type 必须先注册）；每事件稳定 `idempotency_key=event.id`。
3. **M1 终态机制重设计**（终裁实锤：现 claim UPDATE 抹 last_error 且重领 failed）：
   新增 `permanent INTEGER DEFAULT 0` 列（零重建）+ claim 排除 `AND permanent=0`；
   批量**毒丸隔离**：transport 返回逐事件 `AuditPublishOutcome[]`（governance
   postBatch 本就逐事件回 receipts），坏事件落 permanent 不饿死队尾健康事件。
4. **409 三分支语义**：同 event.id 重投 = **202+Duplicate**（去重成功）；
   409 仅 `event_id_content_conflict` / `idempotency_key_conflict`（发端缺陷
   → permanent+告警，**绝非**去重成功）。
5. **M2 L2 试运行 → 宣布 L2**：pending 主指标（drainer 60s 清空）+ 24h 无新增
   permanent；L2 前置 = `AUDIT_ALLOW_DEV_AUTH=false` + 真实密钥轮换 + demo 禁用
   + HTTPS + **ValidateConfiguration fail-closed** + 读侧授权裁定；
   console 审计页/governance 生产实例标 **not_executed** 并具名 owner。

### P1 其他能力接入

| 能力 | 建议 | 关键点 |
|---|---|---|
| aero-vault 文件 | **接**（M3，独立并行） | presign PUT/GET 直传（BFF 不缓冲大文件，现成能力）；懒迁移+回填，**禁双写**；版本/分享/保留/RAG 后接 |
| aero-im 通知 | **接**（M5） | best_effort_async；审批/异常/任务分派 3 类事件；与审计 failure_policy 对齐；跨主机项待验证 |
| aero-id 组织/部门 | **缓接**（H2 降级） | 先契约定义+预留（DepartmentSnapshot 可空占位已做对），人员选择器/组织树待业务数据成熟 |
| snaplink-console 管理面 | **接**（M3） | 统一审计时间线查询经 governance 授权；不绕过服务 API |

### P2/不接清单（克制原则，11 项 + 触发条件）

aero-im 全量 IM、vault RAG/MCP/WebDAV、aero-id 账户生命周期、ERP 多租户
数据模型（低成本预留：tenant 上下文由 claims 盖章注入）、替换 outbox
（仅当 governance 提供事务性中继时重评）、gRPC、OTel 替代审计、
Billing/插件市场等。完整清单见修订版 §9。

## 3. 工程与运营建议

1. **sverpweb-stable 嵌入 pi-batch**：前置 = `git init` + 首次提交 + CI
   （M4）；campaign 仅分析面（`git_commit: false`）；campaign 配置已就位
   （`pi-batch-campaign.yaml`，20 模块发现，dry-run 零成本）。
2. **platform-status.sh / platform-247.sh 扩展**：`PLATFORM_REPOS` 加
   sverpweb-stable 为第 7 仓；iris-ui **不加**（npm 上游依赖，锁版本
   0.2.x + 升级门禁）。
3. **门禁优先级**：先把 tsc 31 错清零（含 audit/ 4 处 withActor 类型误用 +
   sso-client 声明解析）+ 测试全绿（M1 准入），再谈接入。
4. **跨仓库验证**：suite-template（compose + E2E + suite-check）+
   check-change-manifest.py + 决策摘要门（decisions/ 01→05 流程现成）。

## 4. 五轮对抗的实质发现（人工复核全部 Verified）

| 轮 | 裁决 | 关键实锤 |
|---|---|---|
| 1 | FAIL | "设 env 即激活"证伪（B1 路径/字段/active schema 不匹配）；withActor 类型误用（B2，tsc 31 错实证）；无 forwarding 测试（B3）；零审计事实（B4）；无 git（B5） |
| 2 | FAIL | 交付物自指空壳（agent 工具写文件、回复只回摘要，协调器捕获 15 行）；409 语义错误（202+Duplicate vs 409 冲突分支，源码实测） |
| 3 | FAIL | permanent 终态不可落地（claim 抹 last_error）；批量毒丸（全或无+队首饥饿）；§2.2/§2.3 自相矛盾；null-actor 事件（审计中间件先于认证）；schema 22 vs 30；dev-auth 默认 true |
| 4 | FAIL | 迟到 202 无 no-op 语义（markPublished 守卫实证）；Retry-After 承诺不存在功能；occurred_at 二选一含不可行分支；"22 条"残留；隐式迁移已建表；M1 测试 12 项缺口 |
| 5 | **PASS** | 全部转可执行门禁；1 处非阻塞措辞残留（已人工同步） |

## 5. 待人工批准项

1. 任命修订版 §6 八项 owner + D 组责任人（租户映射/schema 治理/vault 合规/
   保留期/通知归属/iris-ui 门禁/governance 硬化/读侧授权）。
2. M0 契约对拍结果回读（[Unknown] → [Verified]）。
3. 顺序执行 M0→M1→M2→L2→M3/M4/M5，任一硬门槛不绿即停。
4. 本路线图不豁免任何仓库门禁（tsc 0、pnpm check、make ci 等）。

## 6. 建议的下一步（本工具集驱动）

```bash
# 1) 修 B2 缺陷 + tsc 清零（sverpweb-stable，实现面）
cd ~/workspace/job/sverpweb-stable   # 先 git init + 首次提交
python ~/ai-batch-runner/pi-batch.py campaign --config pi-batch-campaign.yaml \
  --top-only --reuse --skip-passed --retry-failed   # 方向发现 + 提案
# 2) 审计契约对拍（M0，跨仓提案走 Change Manifest 门禁）
python pi-batch.py examples/snaplink-platform/audit-onboarding-pipeline.yaml
# 3) 7×24 轮巡（含第 7 仓）
bash examples/snaplink-platform/platform-247.sh 3 300
```
