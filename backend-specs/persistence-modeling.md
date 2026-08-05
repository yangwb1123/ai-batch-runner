# Backend Persistence Modeling Spec — 持久化建模（编码前强制关卡）

被操作和持久化的数据对象决定系统未来五到十年的维护成本。代码可以
重构，但错误的数据模型、主键、关系、状态、审计字段和历史策略，后期
修改成本极高。**创建任何核心表/ORM Entity 前，必须先完成持久化建模。**

## 1. 四种模型必须分离（禁止合并成一个类）

| 模型 | 关注点 |
|---|---|
| API DTO | 协议输入输出、字段名/校验/版本 |
| Domain Model | 业务规则、状态、不变量 |
| Persistence Model | 表结构、类型、索引、序列化 |
| Read Model | 查询效率、展示结构 |

```
写: Request DTO → Command → Domain Model → Persistence Mapper → Persistence Entity → DB
读: DB → Persistence Entity/Projection → Read Model → Response DTO
```

禁止把 ORM Entity 直接返回/接收（API 字段与 DB 强耦合、注解污染领域
模型、敏感字段泄漏、领域规则无法封装）。不得为减少映射代码合并模型。

## 2. 持久化建模流程（建表前必须回答）

业务身份 / 数据库主键 / 业务唯一键 / 外部标识 / 幂等键 / 实体生命周期 /
状态机 / 事务边界 / 聚合边界 / 历史与快照需求 / 审计需求 / 多租户与
数据归属 / 查询与索引需求 / 数据增长规模 / 删除与归档策略 / 迁移与兼容。

## 3. 身份与主键决策表

| 场景 | 选择 |
|---|---|
| 单库小规模 | 自增 ID |
| 分布式 / 需时间有序 / 离线生成 | UUIDv7 / ULID / Snowflake 类 |
| 对外暴露 / 需时间有序 | UUIDv7（推荐）或 ULID |
| 需索引局部性 | Snowflake / ULID（时间前缀） |

区分：内部主键（关系用）/ 业务编号（业务识别）/ 第三方标识（单独保存）/
幂等键（幂等用）。禁止用可变业务字段、手机号、身份证、邮箱作主键。

## 4. 字段硬规则

- **金额禁止浮点**：`amount_minor BIGINT` + `currency_code CHAR(3)` 或
  `DECIMAL(18,4)`；明确最小单位、舍入、汇率/税率/分摊精度
- **时间语义明确**：`created_at/updated_at/submitted_at/approved_at/
  effective_from/effective_to/deleted_at/scheduled_start_at`；明确 UTC 还是
  本地、精度、客户端还是服务端产生、是否可空
- **状态不用自由字符串**：定义状态全集 + 转换 + 数值映射
  （10=draft/20=submitted/30=approved/40=executing/50=completed/90=cancelled），
  状态码发布后不复用；状态机集中定义
- **快照字段**：订单项保存 `product_code/name/unit_price/tax_rate` 快照，
  禁止只存 product_id（商品改名/改价后历史失真）。判断哪些信息需
  实时关联 vs 历史快照
- **NULL 语义**：区分 未知/不存在/尚未设置/不适用/已清空——`approved_at IS NULL`
  可能表示未审批/审批失败/不需要审批，必须配合状态字段
- **JSON 谨慎**：仅用于不稳定扩展属性/第三方原始报文/配置快照；禁止
  高频查询字段、外键关系、需精确统计、需独立更新的数据
- 禁止：全部 `VARCHAR(255)`、全部 `INT`、"0"/"" 代替 NULL、逗号字符串
  存多值、JSON 逃避建模

## 5. 关系设计

- 一对一：仅当生命周期/访问频率/敏感级别/字段规模不同才拆表，禁止机械拆
- 一对多：明确子对象是否独立存在、谁拥有外键、删除传播、是否可移动
  （订单项属于订单聚合，不被其他订单引用）
- 多对多：中间表带时间/状态/范围/来源属性时是**业务实体**（角色分配），
  不是简单 user_role 表；禁止逗号字符串表达关系

## 6. 规范化与反规范化

事实数据/事务写模型 ≥3NF；反规范化仅用于高频查询/报表/历史快照，
且必须说明冗余字段的**维护方式**（同事务更新/事件更新/定时重建/可重算/
校验任务）。禁止无一致性方案的冗余字段。

## 7. 索引设计（访问路径驱动，禁止机械加索引）

先列出主要查询（如 `tenant_id+order_no`、`customer_id+created_at`、
`status+scheduled_at`、`idempotency_key`），再设计：

```
UNIQUE (tenant_id, order_no)
INDEX  (tenant_id, customer_id, created_at)
INDEX  (tenant_id, status, scheduled_at)
UNIQUE (tenant_id, idempotency_key)
```

考虑：最左前缀、选择性、排序/范围、覆盖索引、写入成本、索引大小。
禁止重复索引、低选择性单列索引堆、只看单条 SQL 不看写入成本。

## 8. 唯一约束 = 业务规则的一部分

应用层"先查后插"并发下失效。数据库唯一约束是最终防线（应用层负责
友好错误）：`tenant_id+order_no`、`tenant_id+external_id`、
`tenant_id+idempotency_key`、`user_id+role_id+scope_id`。

## 9. 外键与完整性

单体单库默认使用外键（引用完整性、防孤儿）；分库分表/微服务不用外键
时**必须提供替代**：应用层校验、删除保护、一致性巡检、对账补偿、
孤儿清理。禁止一句"微服务不用外键"就放弃完整性。

## 10. 审计 / 历史 / 并发 / 删除

- 审计：`created_at/by, updated_at/by, deleted_at/by, version, source, trace_id`；
  按需选择 最终状态 / 操作日志 / 状态历史 / 字段变更历史 / 完整快照。
  订单状态历史必须落表（`order_status_history: from/to/event_type/operator/
  reason/occurred_at/trace_id`），禁止只靠应用日志恢复
- 乐观锁：低冲突写操作 `UPDATE ... SET version=version+1 WHERE id=? AND version=?`，
  影响行数=0 即冲突；高冲突计数用原子更新/锁
- 软删除非默认：需恢复/审计/误删保护才用；必须处理唯一索引排除、
  查询过滤、关联数据、定期归档、恢复冲突。高频大表/临时数据/隐私
  删除要求用硬删除

## 11. 迁移（Expand–Migrate–Contract）

禁止一次部署同时删旧字段+改代码读新字段。顺序：新增兼容字段 → 双写 →
回填历史 → 切换读取 → 停旧写 → 删旧字段。每次 Schema 变更必须有：
向前迁移、回滚/前滚、新旧兼容、回填、大表/索引/锁风险、灰度顺序。

## 12. 持久化设计报告（编码前输出模板）

```markdown
## Persistence Design
Aggregate / Tables / Identity（内部主键+业务键+幂等键）/
Consistency Boundary（一个事务保存什么）/
Snapshot Fields / Concurrency（乐观锁/原子更新）/
Main Queries + Indexes / History / Deletion / Migration
```

未完成此报告前，不得将 ORM Entity 视为最终设计。
