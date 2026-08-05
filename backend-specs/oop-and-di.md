# Backend OOP / IoC / DI / AOP Spec — 面向对象与架构思想

## 1. 对象还是函数？（先判断，不默认 OOP）

**用对象**：有身份和生命周期的业务实体、需要维护不变量的状态、状态与
行为紧密关联、存在多个可替换实现、需要封装内部细节。

**用函数**：纯计算、无状态转换、确定性数据变换——不机械包装成类：

```
❌ class TaxCalculationManagerFactoryService { calculate(...) {} }
✅ function calculateTax(amount: Money, rate: Decimal): Money { ... }
```

规则：状态与行为紧密相关时用对象；纯数据变换用纯函数。禁止把一切
逻辑写成类、禁止贫血对象（只有 getter/setter 无业务行为）。

## 2. 封装（不是加 private 就完了）

- 外部不能制造非法状态；状态只能通过**有业务语义的方法**变化
- 对象负责维护自己的业务不变量；内部数据结构可独立变化

```
❌ order.setStatus('paid'); order.setAmount(100); order.setInventoryReduced(true)
✅ order.confirmPayment(payment); order.cancel(reason); order.ship(shipment)
```

## 3. 抽象（表达业务能力，不是技术泛型）

```
✅ interface InventoryReservation { reserve(cmd: ReserveInventoryCommand): Promise<Reservation> }
❌ interface GenericProcessor<T, R, C> { process(data: T, context: C): Promise<R> }
```

## 4. 继承（只用于稳定的"是一种"关系）

禁止为复用代码建立 BaseController → BaseBusinessController →
BaseAuthenticatedController 多层继承。默认组合优于继承、委托优于重写、
显式依赖优于隐藏依赖。

## 5. 多态（只用于真实变化轴）

`FileStorage ← LocalFileStorage / S3FileStorage / MinioFileStorage`，
调用方只依赖 FileStorage。没有变化轴时不需要接口。

## 6. IoC / DI（核心不是容器，是依赖方向）

高层业务定义自己需要的能力，低层实现服从高层契约：

```
✅ CreateOrderUseCase → PaymentGateway ← StripePaymentGateway
❌ CreateOrderUseCase → Stripe SDK
```

- 默认构造函数注入
- 禁止：属性/方法注入、全局容器、Service Locator、静态单例访问、
  业务代码里 `container.resolve()`（依赖被隐藏 → 无法判断依赖/测试/生命周期/循环依赖）

## 7. 依赖生命周期（Agent 必须分析）

| 生命周期 | 适合 |
|---|---|
| Singleton | 无状态服务、线程安全客户端、配置 |
| Request Scoped | 请求/租户/事务上下文 |
| Transient | 临时命令处理器、轻量状态对象 |
| Connection Scoped | 数据库连接、WebSocket 会话 |
| Job Scoped | 定时任务、消息消费上下文 |

禁止：Singleton 持有请求级用户信息/非线程安全状态；Request Scoped 被
Singleton 引用；每次请求重建昂贵 HTTP Client；连接不归还连接池。

## 8. AOP（横切关注点）

**适合**：日志、Trace、指标、认证、通用授权入口、事务、缓存、限流、
重试、审计、异常映射。

**禁止隐藏进切面**：扣库存、修改订单状态、计算价格、发起支付、核心
审批条件、重要业务补偿——调用代码看似简单，真实业务流程被隐藏。

引入切面必须明确：切点、执行顺序、是否同步、异常传播、事务关系、
重试关系、幂等性、性能开销、测试方式。防止 Transaction AOP 与
Retry AOP 嵌套顺序语义颠倒、不可追踪的隐式调用链。
