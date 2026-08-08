这是 evidence 阶段的第 5 个输入任务：`05-integration-design.md`（审计接入设计摘要）。已核对与 inputs 目录文件一致（完整草案在 `docs/snaplink-platform/audit-ingestion-design-draft.md`，17KB）。作为证据归一，提取各服务 connector 现状、目标态通用设计与推进时序：

---

# 证据归一输出：审计接入设计证据（05-integration-design）

> 任务：`05-integration-design.md`（证据归一 · 各服务审计接入设计摘要）。
> 等级：§0 锚点为 **Verified**（各仓库 connector 实际状态）；其余设计细节 **[PROPOSED]**，pending 逐代码库验证与人工批准。

## A. Verified 锚点与缺口（各服务 connector 现状——阶段 3 的直接输入）

| 服务 | connector 现状 | 等级 | 说明 |
|---|---|---|---|
| aero-id | **存在**（`connector/auditgovernance`，publisher/oauth） | Verified | 最快可推进；需核对 lease/batch/backoff 语义是否与通用设计一致 |
| aero-vault | **存在**（`internal/auditgovernance/token.go`） | Verified | 参照模式；delete 流需按 04-composition 决策：delete + `file.delete.admin` + notify 同事务 |
| snaplink | **无 connector（缺口）** | Verified | auth 行按 request_id 键控；`auth.token.issue` 保持 L1；`tenant.delete` 用同步最小确认；无字段则跳过 backfill |
| aero-im | **无 connector（缺口）**，需新 Rust connector | Verified | scope 授予以 relay 实现为条件（与 04-scope-matrix flow 3 一致）；moderation 事件进优先分区 |
| snaplink-console | **无 outbox（消费者，正确）** | Verified | 边缘事实（login via `client_id=console`）在边缘生成；审计查询由账本读 API 自审计，直连 API 无法绕过 |
| audit-governance | **无 outbox（自审计写入器，同事务，fail-closed）** | Verified | 治理操作自身的账本追加失败即中止；定义 sink 侧 ingest 契约：dedup、ADR-0002 校验、tenant 一致性、时钟偏差拒绝 |

**与 01 证据的交叉**：sverpweb 的本地 outbox（SQLite）+ 批量提交 + permanent 毒丸隔离 + 幂等重放（连接 2）与 §1 通用设计高度同构——sverpweb 是**已实装参照实现**；本设计把该模式推广到各服务（snaplink/aero-im 为 gap 修复项）。

## B. 通用设计目标态（sink 契约 + 生产者契约，阶段 3 验收标准的依据）

**Outbox schema**：`audit_outbox`（event_id = 确定性幂等键、`UNIQUE(service, event_id)`、status 0/1/2/3、attempt_count、next_attempt_at、lease_until；与业务操作**同事务**写入）。

**Relay**：SKIP LOCKED lease 认领、batch 100、client_credentials tokenSource、指数退避 + jitter（速率类 30–60s，cap 300s）、**never drop**；高频事件 L1 聚合；仅极端风险操作同步最小确认。

**幂等**：`event_id = SHA-256(service|event_type|tenant_id|trigger_key)`，trigger_key 用稳定业务键（request_id、file_id\|version、message_id\|edit_seq…）；重试复用、sink 去重。

**DLQ**：permanent 失败（422/schema/tenant 不匹配）→ dead 状态，毒丸不阻塞批次；产出 `audit.outbox.failure` / `audit.relay.dlq.replay` 事件；告警阈值。

**失败模式表**：10 类（含 发送与标记间崩溃由 dedup 吸收、sink 时钟偏差、队列增长）+ **6 阶段迁移骨架**（sink-ready → flag/dry-run → dual-write → cutover → bounded backfill → gate）。

→ 与 03 组合纪律一致：审计不阻塞业务主流程（同事务 outbox）、L2 是 full 组合部署规则（非产品硬编码）；与 04 scope flow 3 一致：`audit:event:ingest` append-only + 幂等 + tenant 一致性。

## C. 推进时序（§2，阶段 3/5 定序依据）

**sink（governance 契约就绪）→ aero-id → aero-vault → snaplink → aero-im → console**

- 每条依据：connector 现状（id/vault 已有 → 先对拍语义）、缺口复杂度（im 需新 Rust connector → 后置且带前置条件）、消费者无 outbox（console 天然最后）。
- snaplink 的 `tenant.delete` 同步最小确认需列 sync-ack 清单（开放决策 #4）。

## D. 6 个开放决策（阶段 3 归属项目须处置）

| # | 决策 | 归属 |
|---|---|---|
| 1 | backfill 策略（哪些事件/多少历史要补） | snaplink（无字段则跳过）+ 各服务 |
| 2 | L1 聚合阈值（‡ 高频事件门限） | governance（sink 契约）+ 各服务 |
| 3 | moderation 优先分区（aero-im） | aero-im |
| 4 | sync-ack 清单（极端风险操作集合） | snaplink + governance |
| 5 | connector 加固（与通用语义对拍） | aero-id / aero-vault |
| 6 | sink 不可用时的 DLQ 事件记账 | governance（sink 契约） |

## E. 对阶段 3 的映射（"为完成连接要做的改变"直接引用此目标态）

- **snaplink 分析**：缺口 = 新增 connector（或维持 L1 + 仅 tenant.delete 同步确认）；`recordAdminUserCRUDAction` 已审计的事实并入读侧授权分析。
- **aero-id 分析**：connector 已存在 → 验收标准 = 与 §1 语义逐条对拍（lease/batch/backoff/幂等键）。
- **aero-vault 分析**：delete 流同事务（delete + 事件 + 通知）；trigger_key = file_id\|request_id（区分不同下载）。
- **aero-im 分析**：新 Rust connector + moderation 分区 + relay 先行（scope 授予条件）。
- **console 分析**：维持无 outbox 消费模型；login 边缘事实生成 + 账本读 API 自审计。
- **governance 分析**：自审计 fail-closed + sink 契约五要素（dedup/ADR-0002/tenant 一致性/skew 拒绝/幂等）作为所有生产者的验收基线。

**归一结论**：本文件给出审计接入的**目标态工程规范**（通用 outbox/relay/幂等/DLQ/迁移）与**各服务现状定位**（2 已有 connector、2 缺口、1 消费者、1 自审计）。阶段 3 各项目分析的"审计接入"维度直接引用 §1 契约作为验收标准，缺口修复（snaplink/aero-im）按 §2 时序与 D 表开放决策排序；所有条目保持 [PROPOSED]，待逐代码库验证后由人工批准提升。
