# Backend DDD Spec — 领域建模（战略 + 战术 + 分级使用）

DDD 不是"多建几个文件夹"，而是用业务语言识别边界、规则、模型和
一致性范围。

## 1. 战略设计

**统一语言**：代码概念与业务一致。业务说"排程单"，代码不得混用
Schedule/Plan/Job/Task/ProductionOrder。修改前提取：业务术语、动作、
状态、规则、角色、事件。

**限界上下文**：同一个词在不同上下文含义不同（销售上下文的"订单" vs
生产上下文的"订单"）。禁止设计一个超级 Order 承载所有系统含义。

**防腐层 ACL**：对接老 ERP/外部系统时，旧字段（FInterID、FEntryID、
BillStatusCode、Use_Qty）必须在 Adapter 内转换，不得传播进新领域模型。

## 2. 战术设计

| 概念 | 要点 |
|---|---|
| Entity | 稳定身份（Order/Customer），属性变化身份不变 |
| Value Object | 由值决定身份，**不可变** + 创建时校验（Money/Address/DateRange/EmailAddress） |
| Aggregate | 一致性边界（非对象集合）；外部只能通过聚合根修改内部 |
| Aggregate Root | 维护不变量、控制内部修改、产生领域事件、定义事务边界 |
| Domain Service | 不适合归属实体/值对象的领域操作（PricingPolicy）；禁止把上帝 Service 换个名字 |
| Application Service | 用例编排、权限上下文、事务边界、调用领域对象与 Port；不承担核心业务计算 |
| Domain Event | 已发生的业务事实，过去式命名（OrderSubmitted）；区分强一致编排与最终一致 |
| Repository | 对应**聚合根**而非每张表 |

```
❌ orderItem.quantity = quantity
✅ order.changeItemQuantity(itemId, quantity)
```

## 3. DDD 分级使用（按复杂度选择，禁止默认全套）

| 等级 | 结构 | 适合 |
|---|---|---|
| L0 事务脚本 | Controller → Service → Repository | 简单 CRUD、配置、字典、简单报表 |
| L1 模块化业务 | Module → Application Service → Repository | 少量规则、有模块边界、无复杂状态机 |
| L2 领域模型 | Application → Aggregate → Repository | 状态复杂、规则多、业务常变、需要不变量 |
| L3 完整 DDD | 限界上下文 + 上下文映射 + ACL | 多上下文、多团队、ERP/生产/订单大型领域 |

Agent 必须判断等级：普通 CRUD 不强制 DDD；复杂状态/复杂规则/多系统
集成/长期演进领域优先 DDD。

## 4. 领域建模决策链

1. 业务属于哪个限界上下文？统一语言是什么？
2. 哪些是实体、哪些是值对象（默认不可变）？
3. 聚合边界在哪里？事务一致性边界多大？
4. 哪些规则属于领域对象/领域服务（非 Application/Service）？
5. 跨聚合一致性：领域事件 / Saga / 最终一致？
6. 需要防腐层的边界有哪些？
