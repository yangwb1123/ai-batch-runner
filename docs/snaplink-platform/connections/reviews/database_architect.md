数据库架构审查已完成，产出 `docs/snaplink-platform/connections/reviews/database_architect.md`（230 行）。

## 审查概要

**执行验证**：独立核查了六个仓库的存储层（sverpweb 本机不可达，标 [doc]），重点回答"每个 store 是 hot 还是 durable、stock binary 是否真实接线后端"：

| 项目 | 耐久结论 | stock 接线 |
|---|---|---|
| snaplink | **默认全 hot**（identity/oauth/audit/permissions 均 `backend: memory`，重启即失） | HA 门禁只挡"声明多副本+memory"；sqlite/pg 为显式选项 |
| aero-id | durable（Postgres，哈希链账本） | `auto_migrate: false` + 显式 `cmd/migrate`；relay worker 已接线 |
| aero-vault | durable（Postgres/SQLite 双方言） | boot 自动迁移 + 审计 runtime `Start+WrapRepository` 已接线 |
| aero-im | durable（Postgres，237 embedded 迁移 boot 执行） | 消息发送 = 同事务 outbox + 30s 租约 relay，全部接线 |
| audit-governance | durable 但**结构异质**：全账本 = 单行 JSONB 快照（file 默认 / PG 单行 CAS） | 二进制**不跑迁移**；001 关系表是死代码 |

**核心发现（Critical×1 + High×5）**：
- **DB-1（Critical）**：governance 每次 ingest 全量重写整个账本快照（O(N) 写放大），多副本 CAS 争用；`ledger_events`/`event_receipts` 关系表零 Go 引用、RLS 挂在死表上——建议**测量后规模触发式演进**，不立即重写
- **DB-2**：vault 删除审计 emit 在提交后 + sink 错误吞掉 →"删了没记"崩溃窗口（确认 backend_engineer B-2）
- **DB-3**：aero-id relay `FinishPublication` 错误吞掉 + dead_letter 无告警 → 丢失不可发现
- **DB-4**：aero-id 哈希链账本 append-only 但**无归档执行者**（7y 保留只是标签），无限增长
- **DB-5**：snaplink stock 全 memory 默认——生产按默认配置部署即静默丢审计
- **DB-6**：governance 迁移不随二进制执行，首次 PG 启动被 hazard guard 挡死，部署路径缺失

**证据精度修正**（供裁决门引用）：vault 部门表**有** `UNIQUE (tenant_id, parent_id, name)`（backend_engineer B-3 的"无唯一约束"表述需修正为"约束含 parent_id"）；vault presign GET 在签发时刻确有 `ActionDownload` ACL 检查。

**迁移序列**：M0 配置止血（无 schema 变更）→ M1 审计语义修复（vault 同事务化 + relay 告警）→ M2 规模触发式演进（governance 关系化、aero-im 分区 runbook）→ M3 保留/归档。**未知量 10 项**（事件量、CAS 冲突率、合规窗口、备份责任等）均无实测，需先测量再定 DB-1 触发阈值。

**对裁决门的影响**：无方向性阻塞；DB-2/DB-3 与既有审查一致建议并入 gate 引用，防止下游按"同事务/有兜底"表述编码。
