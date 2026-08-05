# Backend Domain Modeling Spec — 领域对象/状态机/业务不变量/数据所有权

复杂业务规则不得全部堆积在 Service 中。

## 1. 领域对象保护自身状态

优先将以下规则放入领域对象或领域策略：状态变化、金额计算、资格判断、
数量约束、业务不变量、状态迁移条件。

```
❌ order.status = "paid"; order.total = -100
✅ order.pay(payment); order.cancel(reason); order.changeQuantity(productId, qty)
```

领域对象必须拒绝非法状态，不暴露可随意修改的关键字段。

## 2. 状态机集中定义

实体状态 >3 或转换存在权限/条件/副作用时，集中定义：

- 状态、事件、允许转换
- 前置条件（业务条件，非仅权限）
- 权限条件
- 转换副作用（发事件、写日志）
- 非法转换错误

示例：`order.cancel()` 在 `status !== PendingPayment` 时抛
`InvalidOrderTransitionError` 并携带当前/目标状态；领域对象保证不变量。

禁止：Controller/Service/Job/Consumer 各写一份 `if (status === ...)`。

## 3. 业务规则用 Policy 集中，不散落模板

按钮/动作可见性 = 业务状态 × 权限 × 数据条件 × 系统状态。规则集中到
Policy（如 `canApproveOrder(context)`），UI/接口只消费结果。

## 4. 聚合边界与数据一致性

- 一个用例一个事务边界（Unit of Work 位于 Application Use Case，
  不在 Controller 或单个 Repository 手动 begin/commit/rollback）
- 同事务内必须完成的强一致逻辑明确编排；非关键扩展动作可异步事件
- 跨模块/跨服务：幂等键 + 可重试 + 可补偿 + Outbox/Inbox

## 5. 服务分层职责

| 层 | 职责 | 禁止 |
|---|---|---|
| Transport | 协议适配、参数校验、DTO 转换 | 业务规则、直接碰数据库 |
| Application | 用例编排、事务边界、权限 | 框架/ORM 细节 |
| Domain | 状态机、策略、不变量、仓储接口 | 依赖框架/SDK/Controller |
| Infrastructure | 实现 Domain 端口 | 业务规则 |

DTO / 领域模型 / ORM 实体三者不得混用（接口返回结构变化不渗透业务层）。

## 6. 领域审查清单

- 状态转换是否集中、有无非法转换保护
- 金额/数量等关键值是否由领域对象保证（非负数、精度）
- 业务规则是否复制到多个模块
- 聚合边界是否清晰、事务是否在用例层
- 核心业务能否脱离基础设施（DB/HTTP/消息）独立单测
