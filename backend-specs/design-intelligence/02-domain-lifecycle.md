# 后端设计智能 02：业务生命周期与状态机（Domain Lifecycle）

> 一个"上传"不是 upload API，而是一条业务管道。
> 后端必须理解业务生命周期，而不是 CRUD 切片。

## 1. 生命周期管道模式

用户上传文件的体验：

```
上传 → 扫描病毒 → 生成缩略图 → OCR → AI 解析
    → 权限检查 → 索引 → 通知
```

后端不能只有 upload/download/delete，需要：

```
File Service（入口 + 状态机）
Event Bus（异步步骤解耦）
Worker（扫描/缩略图/OCR 消费者）
Audit Service（全程审计）
Search Service（索引）
Notification Service（完成通知）
```

**规则**：每个"看起来简单"的操作先画生命周期图（状态 + 事件 + 处理者），再定服务边界。

## 2. 状态机优先于状态字段

❌ `status varchar(20)` + 代码里散落字符串比较

✅ 集中定义的状态机：

```
Created → Confirmed → Producing → QualityCheck → Shipping → Completed
              ↘ Cancelled
              ↘ Failed
```

- 状态转换表（from × event → to + 副作用）
- 非法转换显式拒绝（fail closed）
- 状态变化写历史（ChangeLog），不覆盖

## 3. 事件驱动的业务闭环

生命周期长/跨服务时：

```
订单确认 → OrderConfirmed 事件
  → 库存预占（InventoryReserved）
  → 排产（ProductionScheduled）
  → 通知（NotificationSent）
```

**规则**：跨服务状态流转用事件（最终一致），同服务内用事务；事件带幂等键与版本。

## 4. 业务规则的位置

- 规则属于领域层（不是 Controller/Service 里散落）
- 规则可测试（纯函数/决策表）
- 规则随状态机集中定义（同一状态的所有约束在一起）

## 5. 检查清单

```
□ 每个业务操作是否画过生命周期图（状态×事件×处理者）？
□ 状态是否集中定义的状态机（枚举/决策表）而非散落字符串？
□ 状态变化是否写历史（不覆盖）？
□ 跨服务流转是否事件驱动 + 幂等？
□ 业务规则是否在领域层可测试？
□ 异步管道是否 Event Bus + Worker + 审计？
```
