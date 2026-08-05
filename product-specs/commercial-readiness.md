# Commercial & Open Source Readiness Spec — 商业/开源要素与克制边界

按 productization_level 触发（L2 商业 / L3 商业+开源）。核心纪律：
**低成本预留必须做，高成本实现需明确信号，无信号禁止提前设计。**

## 1. 低成本预留（设计时默认，必须做）

- 多租户：`tenant_id` 进入主查询条件、唯一索引、缓存 Key、事件、审计、
  对象存储路径、日志上下文（禁止 `WHERE id = ?` 无租户条件）
- 事件/审计：事件带组织上下文（org_id/tenant_id）；核心表含
  created_by/updated_by/trace_id 审计字段
- API 版本化：公共 API 从 v1 起带版本；Schema 变化按契约管理
- 幂等键：写操作预留 `idempotency_key` + 唯一约束
- 标识：主键选型考虑分布式演进（UUIDv7/ULID），避免未来无法迁移

## 2. 高成本实现（需明确商业信号，禁止提前设计）

| 能力 | 触发信号 | 无信号时 |
|---|---|---|
| Billing/Subscription/计费 | 商业化关键词 + 定价/收费/套餐 | 不设计；仅预留 billing 上下文字段 |
| 多级 License（Community/Enterprise） | 商业化 + 版本分级信号 | 不设计 |
| 插件市场/Marketplace | 生态/插件市场信号 | 不设计 |
| 完整租户计费体系 | 多租户 + 计费信号 | 只做隔离，不做计费 |

## 3. 开源要素（L3 且有开源意图才加载）

- 目录：`docs/ examples/ packages/ apps/ scripts/ benchmark/ design/ .github/ docker/ helm/`
- 文档：README（安装/使用/API/示例）、LICENSE、CHANGELOG、CONTRIBUTING、
  CODE_OF_CONDUCT、SECURITY、Issue/PR 模板、Roadmap
- 工程：CI、语义化版本、Docker/Compose/Helm、覆盖率、Dependabot/Renovate、
  SBOM、基准测试
- 无开源意图 → 不生成开源文档全套（克制）

## 4. 克制红线（与 product-thinking.md 一致）

- 推演出的商业/开源维度是**待确认问题**，不是立即实现的功能
- 无第二使用方不抽象；无商业信号不建 Billing；L0 不建产品结构
