# 软件工程重构规范（Refactoring Standard）

> **定位**：Agent 发现上帝文件（God Object/Class/Component）时**不应该
> 简单拆文件**，而应按几十年软件工程积累的方法做**结构化重构**：
>
> ```
> 发现问题 → 代码度量分析 → 职责识别 → 设计模式选择
> → 依赖重构 → 测试保护 → 渐进迁移 → 更新架构记忆
> ```
>
> 核心判断：**重构不是拆文件（order1/order2 只是换名字），而是拆变化
> 原因**。`pi-batch refactor` 是该规范的可执行化（确定性启发式，零 LLM）。

## 1. 上帝文件检测（规则化，不凭感觉）

| 指标 | warning | critical | 仓库门禁 |
|---|---|---|---|
| 文件行数 | 300 | 600 | quality.py（500/1000）+ check-backend-quality（400 god-file） |
| 函数数 | 20 | 40 | check-backend-quality（public 方法 >12） |
| imports | 20 | — | `refactor` 报告 |
| 模块依赖 | 15 | — | `capabilities check`（module_dependencies ≤15） |
| 圈复杂度 | 11（重构） | 20（必须拆） | quality.py（≤15）+ `refactor` 分级 |

圈复杂度分级：1-5 简单 / 6-10 可接受 / 11-20 重构 / >20 必须拆。

## 2. 职责识别（Responsibility Map）

`refactor` 按方法名关键词聚类到职责域（order/pricing/notification/audit/
file/payment/auth）。**>1 域命中 = SRP 违规**，输出"按变化原因拆分为：
order, pricing, notification..."——不是 `order1/order2` 式改名。

## 3. 设计模式选择（信号 → 建议）

| 信号 | 建议模式 | 检测器 |
|---|---|---|
| if/elif ≥3 判断业务类型 | Strategy / Factory（封装变化点） | `refactor` |
| 业务层直接 `new Xxx()` | DI（构造函数注入，Mysql→Repository 接口） | `refactor` + check-backend-quality |
| try/except ≥3 处横切 | AOP（装饰器/中间件处理日志/权限/事务/指标） | `refactor` |
| 一个动作触发 ≥3 副作用 | 事件驱动（OrderCreated → Email/Inventory/Invoice） | 人工/架构评审 |
| 万能接口 | ISP（拆为 Create/Delete/Export UseCase） | 架构评审 |
| 依赖方向违反 | DIP（Presentation→Application→Domain，Infrastructure 实现 Domain） | check-backend-quality（domain→infra） |

## 4. 分层依赖方向（禁止事项）

```
Presentation → Application → Domain ← Infrastructure(implements)
```

禁止：Controller 直接访问数据库 / Service 直接 new Database / 跨模块引用
内部实现 / 复制权限逻辑 / 硬编码配置（architecture-constitution.md）。

## 5. 数据库层

Service 中禁止裸 `db.query()`——应使用 Repository / Query Object /
Specification（backend-specs/persistence-modeling.md 强制关卡）。

## 6. 前端上帝组件

`OrderPage.vue` 2000 行 → 拆为 OrderTable/OrderFilter/OrderForm/
OrderDetail/useOrder()/order.api.ts（ui-specs/engineering/
architecture-budgets.md）。

## 7. 迁移计划（`refactor` 输出，可执行）

```
Step 1 测试保护：特征测试锁定现有行为（不变量）
Step 2 提取最大职责模块（按变化原因）
Step 3 依赖注入：直接构造 → 构造函数注入
Step 4 横切逻辑：try/日志/权限 → 装饰器/中间件
Step 5 调用方逐个切换 → 删除原代码 → 更新 ADR 与 project memory
```

## 8. 最高级：生成前自检（不让 Agent 产生问题）

Coding Agent 生成代码前检查清单（`pi-batch refactor` 检测项的反面）：

```
□ 我是否创建了 God Class / God Function？
□ 是否 Circular Dependency？
□ 是否 Duplicate Logic？
□ 是否 Hidden Coupling（直接 new 依赖）？
□ 是否 Missing Interface？
□ 是否 Wrong Responsibility（职责污染）？
□ 函数圈复杂度是否 ≤10？
□ 业务层是否无直接 new 数据库/服务？
```

## 9. 仓库落地形态

| 能力 | 实现 |
|---|---|
| 检测器（确定性） | `pbatch/refactor.py` + `pi-batch refactor --file PATH` |
| 门禁（已有） | quality.py / check-backend-quality / check-frontend-quality / capabilities check |
| 迭代（已有） | `pi-batch advance`（P0/P1/P2 批次：be_architecture/fe_complexity） |
| 反思（已有） | `pi-batch reflect`（复杂度维度）+ prompts/critic.md |
| 知识更新（已有） | `pi-batch adr new` + `project facts`（重构后记录决策） |
| 规范资产（已有） | backend-specs/oop-and-di.md、design-patterns.md、ddd.md、architecture-constitution.md |
