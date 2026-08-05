# Backend Architecture Spec — 模块组织 + 依赖方向 + 高内聚低耦合

核心原则：**先识别变化点和业务边界，再选最简单合适的结构；没有变化
需求时，不提前制造抽象。**

## 1. 模块划分（业务能力优先，非技术分层）

中大型项目禁止纯技术全局分层：

```
❌ controllers/ services/ repositories/ models/ utils/ dto/   ← 订单功能散落十几个目录
```

推荐：先按业务模块划分，模块内再做技术分层：

```
modules/order/
├── application/    # use cases：create-order.use-case.ts / cancel-order.use-case.ts
├── domain/         # order.ts / order-status.ts / order-policy.ts / order.repository.ts(接口)
├── infrastructure/ # order.orm-entity.ts / order.repository.impl.ts
└── transport/      # order.controller.ts / order.request.ts / order.response.ts
```

每个模块必须有：明确业务职责、明确公共出口（index.ts 只导出契约）、
隐藏内部实现、不直接访问其他模块内部文件、不直接操作其他模块的数据表。

## 2. 依赖方向（强制）

```
Transport → Application → Domain
Infrastructure → Domain/Application 定义的接口（向上实现）
```

禁止：Domain → Controller / ORM / Redis SDK / HTTP Client / 框架装饰器。
核心业务规则必须能在不启动 Web Server、不连真实数据库的情况下单测。

分层按复杂度选择（Agent 不得强制套领域层）：

| 复杂度 | 结构 |
|---|---|
| 简单 CRUD | Controller → Service → Repository |
| 复杂业务 | Transport → Application → Domain → Port ← Infrastructure |

## 3. 低耦合：依赖业务语义接口，不依赖具体 SDK

```
❌ CreateOrderUseCase → PrismaService + RedisClient + StripeSDK + KafkaClient
✅ CreateOrderUseCase → OrderRepository + PaymentGateway + EventPublisher
```

- 第三方 SDK 用 Adapter 隔离：业务代码不直接 `stripe.paymentIntents.create(...)`
- 模块间通过 Public API 协作：`OrderService → CustomerReader`（模块公共出口），
  禁止 `OrderService → UserTable`
- Repository 表达业务查询，不复制 ORM API：
  `findPendingOrdersBefore(time)` ✅ / `repository.find({where, include, select, orderBy, rawSql})` ❌

## 4. 高内聚检查（命中任一 → 评估拆分）

- 类拥有多个不相关的变化原因（改价格规则、改数据库、改接口格式都会动它）
- 一个 Service 同时负责业务、数据库、缓存、网络、权限、日志（超级 Service）
- 模块无法用一句话说明职责
- 修改一个功能需要修改大量无关代码
- 文件名只能用 Common/Manager/Helper/Utils/BaseService 这类模糊词描述

## 5. 数据所有权

每个核心数据实体有唯一负责模块；其他模块不直接修改其数据、不绕过
公开用例、不复制其核心业务规则、不假设其内部结构稳定。跨模块读取：
Public Query Interface / Read Model / Domain Service / API-RPC / 事件投影。

## 6. 组合优于继承

默认组合 + 构造函数显式注入。继承仅当同时满足：稳定的"是一种"关系、
子类可完全替代父类、父类契约稳定、层级 ≤2-3 层。禁止为复用少量代码
而继承。禁止 BaseController/BaseService/BaseRepository/BaseEntity 承载
不断增长的通用逻辑。

## 7. SOLID 可执行化

- SRP：一个模块只应有一个主要**变化原因**（不是"一个类一个方法"）
- OCP：只有出现**明确变化轴**（支付渠道/存储供应商/定价策略/通知渠道/导出格式）
  才抽象；不为未来可能的需求提前制造十层抽象
- LSP：实现必须保持契约——返回语义、错误语义一致，不加强前置/削弱后置
- ISP：接口围绕调用方需求设计（OrderCreator/PaymentAuthorizer 分离，
  禁止巨型 SystemService 接口）
- DIP：高层业务依赖抽象端口，不依赖 SDK 具体类

## 8. 状态管理

实体状态 >3 个，或转换存在权限/条件/副作用时，必须集中定义状态机
（状态/事件/允许转换/前置条件/权限/副作用/非法转换）。禁止把状态判断
散落在 Controller、Service、Job、Consumer 中；禁止 `order.status = "paid"`
直接赋值（应 `order.pay(payment)` 由领域对象保证不变量）。

## 9. 文件与复杂度预算（超过须分析原因，非机械拆分）

| 指标 | 参考值 |
|---|---|
| 函数 | ≤40 行 |
| 圈复杂度 | ≤10 |
| 嵌套 | ≤3 层 |
| 文件 | ≤400 行 |
| 类 | ≤300 行 |
| 构造函数依赖 | ≤7 个 |
| 公共方法 | ≤12 个 |
| 参数 | ≤5 个 |
