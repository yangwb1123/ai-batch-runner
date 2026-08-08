这是 evidence 阶段的第 3 个输入任务：`03-combination-model.md`（组合式架构建议）。已核对与 inputs 目录文件一致。作为证据归一，我将本文件的组合纪律提取为 kickoff/per-project 阶段必须遵守的约束基线：

---

# 证据归一输出：组合式架构纪律基线（03-combination-model）

> 任务：`03-combination-model.md`（证据归一 · 组合纪律基线）。
> 本文件是**纪律性约束**而非连接事实：它规定连接分析的框架——哪些连接是产品固有、哪些是可选集成、哪些是组合行为，以及连接的同步/审计边界。

## A. 六项目定位骨架（连接分析的分类依据）

- **5 个独立基础服务**（snaplink / aero-id / aero-vault / aero-im / audit-governance）：可单独运行/部署/升级，不要求其他服务存在，不访问他库，通过 API/SDK/事件/扩展点对外。
- **1 个独立管理应用**（snaplink-console）：首先是 snaplink 管理端；组合场景经**能力发现**增加 aero-* 管理入口，不默认强依赖。
- **1 个组合仓库**（snaplink-suite，新增）：compose/Helm/兼容矩阵/契约测试/E2E——不承载业务代码。

→ 连接分析结论语义：**"为完成连接要做的改变"必须落在产品或 suite 组合层，禁止为了整体一致而跨仓库顺手改**。

## B. 六边形纪律（对连接分析的硬约束）

- 核心代码只依赖端口：`AuthorizationProvider` / `AuditPublisher` / `NotificationPublisher` / `StorageProvider`；适配器可替换（Local/Jwt/Snaplink、Noop/Webhook/GovernanceSink…）。
- 部署用配置选择集成：`authorization: {provider: snaplink}`、`audit: {provider: snaplink-audit-governance}`、`notification: {provider: aero-im}`——也可全本地。
- **禁止**：核心领域 `new SnaplinkService()` 锁死组合；硬编码兄弟服务 URL。
- 映射到连接分析：每条连接的现状（如 sverpweb BFF 代理、vault JWKS 验签）应标注是**端口适配器**还是**硬编码耦合**；缺口修复应优先走适配器/配置，而非在核心代码里加兄弟服务调用。

## C. 审计集成三级（连接 2/缺口分析的判定标尺）

| 等级 | 模式 | 判定含义 |
|---|---|---|
| L0 | 本地数据库/结构化日志/OTel | 独立部署合法 |
| L1 | 统一事件结构输出（Webhook/Kafka/NATS/HTTP） | 标准协议组合 |
| L2 | 适配器汇入 governance（幂等/哈希链/合规） | **full 组合部署的规则，非产品硬编码** |

- **审计不阻塞业务主流程**：业务事务内写 Outbox → 同事务提交 → relay → 事件总线 → governance；不可用不丢事件。
- 极高风险操作（删租户/重置主密钥）才允许"同步最小审计确认 + 异步完整事件"。
- 对连接分析的含义：sverpweb 的 outbox 链路（连接 2）与 05-integration-design 的通用 outbox 设计一致；aero-im 缺 relay 是 L2 组合部署的缺口，**不构成产品缺陷**（L0/L1 下合法）。

## D. 最小共享契约（连接分析的字段/协议基线）

| 契约 | 要点 |
|---|---|
| 身份上下文 | subject_id / tenant_id / organization_id / session_id / client_id / scopes / request_id / trace_id（HTTP 头/事件信封） |
| 权限命名 | `<domain>.<resource>.<action>`（identity.user.disable、vault.file.download、audit.event.read）→ 与 scope-matrix 的 `vault:blob:read` 命名需对照统一 |
| 事件信封 | CloudEvents 风格（id/type/version/source/tenant_id/time/trace_id/correlation_id/data） |
| 审计事件模型 | actor/subject/action/resource/result/authorization(decision_id)/request/change/integrity |
| 错误/分页 | OpenAPI：code/message/request_id/trace_id/details；cursor 分页 |

- **决策点**：统一授权接口 `POST /v1/authorization/check` 返回 decision + policy_id + **decision_id**，decision_id 必须进审计记录——这是连接 1（sverpweb→snaplink）权限校验与连接 2（审计）之间的契约桥，per-project 分析须核查 decision_id 是否已在 sverpweb 侧透传。

## E. 同步依赖上限与禁止项（连接矩阵的合规过滤器）

- **允许的同步**：console-bff → 各服务（管理 API）；业务服务 → snaplink（授权判断）。其余状态变化走 **Event Bus**。
- **禁止**：audit 为写事件回调业务服务；snaplink 每次校验实时调 aero-id；vault 下载路径串行调三四服务；console 浏览器直连服务 API（直接命中连接 5 现状——snaplink-console 浏览器直连 13015 属违规，BFF 化是必须项）；direct-database-access、bidirectional-sync、im-serial-fetch-context-on-send。
- 示例合规流：用户冻结 = console → aero-id → `identity.user.disabled` 事件 → snaplink 撤 token / vault 取消分享 / im 通知 / governance 记录（**事件驱动，不是 console 串联同步调用**）。
- 对连接分析的含义：连接 4（sverpweb→aero-im 通知）与 8（console BFF）的现状/建议都必须过此过滤器；任何"新增同步调用链"的建议默认违规，除非命中允许清单。

## F. 三层判断模板（per-project 分析每题必须自答的第一问）

1. **单项目能力** → 只改当前仓库（例：vault 文件版本恢复）。
2. **可选集成能力** → 改发布方 + 需订阅的适配器；通知失败不影响主流程（例：恢复后通知 = vault 发通用事件 + im 适配器）。
3. **组合产品行为** → 有直接责任的项目 + suite E2E。

**跨项目修改必须满足"确实需要改变双方契约"**：新增文件审计事件 → vault 发布；governance 已支持通用事件则**无需修改**；im 不订阅则**无需修改**。
→ 这是对阶段 3 七份项目分析"为完成连接要做的改变"的**克制红线**：每条改动必须先归入 1/2/3 层，跨仓改动必须能论证契约确实需要变。

## G. 版本与发布纪律

- 组件独立语义化版本，**禁止锁步发布**；suite 维护兼容矩阵（BOM）= 组件版本窗口 + 契约版本 + 集成等级，三者不混为一谈；`suite-check.py` 门禁保证矩阵与 compose 一致。
- 契约升级顺序：兼容性分析 → 版本化 → 旧客户端兼容窗口 → 灰度/flag。
- 连接分析建议如涉及接口变更（如 aero-id gRPC 定案、im Outbox relay 新增），须标注兼容窗口与灰度策略，否则按纪律驳回。

## H. 与 evidence 01 的对照结论

- 01 证据的 8 条连接中：连接 1/6/7（认证信任）属"可选集成能力"层（适配器/配置实现，已是端口形态）；连接 2（审计 outbox）已是 L2 合规形态；连接 5 现状（浏览器直连）**违反 E 节禁止项**，属必须修复项；连接 8 的 BFF 两 token 模型正是 console-bff → 各服务的允许同步形态。
- 组合纪律未与本批证据冲突；kickoff 阶段的连接矩阵应同时标注每条连接的**合规性**（端口化/事件驱动/同步上限），而不只是接口与状态。

**归一结论**：本文件为连接分析提供纪律过滤器（六边形端口化、审计 L0/L1/L2、同步上限与禁止项、三层判断、版本纪律）。阶段 2 连接矩阵与阶段 3 分项目"改动点"须逐条过此过滤器；跨仓改动默认驳回，除非满足"确实需要改变双方契约"。
