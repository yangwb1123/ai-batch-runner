# Product Thinking Spec — 产品化分级与需求推演

Agent 处理需求时的思维层级必须达到"产品"层，但**推演深度由需求的产品化
等级决定**——小工具不做产品推演，产品需求不做局部修补。

## 1. 五层思维

| 层级 | 思考 | 适用 |
|---|---|---|
| L0 完成功能 | 写代码 | 任何任务的基础 |
| L1 完成模块 | 模块边界/复用 | 通用能力 |
| L2 完成系统 | 架构/扩展/演进 | 平台能力 |
| L3 完成产品 | 用户旅程/角色/商业价值 | 产品需求 |
| L4 完成生态 | 开源/商业化/生态 | 战略需求 |

## 2. productization_level 决策表（assessor 自动判定）

| 信号 | 等级 | 推演深度 |
|---|---|---|
| 小工具/脚本/内部/临时/demo/utility/script | **L0 local_feature** | 不推演——只做需求本身，禁止产品化结构 |
| 模块/通用/复用/组件库/SDK/library/module/reusable | **L1 reusable_module** | 模块边界、通用接口、文档、示例 |
| 平台/多租户/多端/开放/集成/插件/API Key/Webhook/platform/multi-tenant | **L2 platform_capability** | 租户隔离、配额、开放 API、审计 |
| 产品/商业化/开源/上线/对外/SaaS/product/commercial/open source/release | **L3 product_feature** | 全量：商业要素 + 开源要素 + 演进 |

## 3. 隐含需求推演（产出"问题清单"，不是"功能清单"）

推演出的维度是**要回答的问题**，是否实现由需求确认决定——
**禁止未确认就实现推演出的功能**。

按场景关键词触发推演链：

| 场景 | 必须推演的问题 |
|---|---|
| 登录/认证 | 权限/RBAC？令牌刷新/过期？安全与风控？审计？SSO/OAuth/OIDC？多端/设备管理？ |
| 上传/文件 | 失败重试？断点续传？秒传？大文件/对象存储（S3/OSS/MinIO）？权限/外链？CDN？生命周期/过期？ |
| 审批流 | 审批节点/条件？模板/版本？历史/回滚？代理？超时？通知/事件？ |
| 消息/聊天 | 群组/频道？未读/已读回执？多端同步？离线消息？搜索/引用？撤回/删除？推送？ |
| ERP/订单 | 域划分（采购/销售/库存/财务）？状态机/生命周期？审批/库存/支付/物流/售后？ |
| 搜索 | 精确/前缀/全文/模糊？排序/相关性？权限过滤？索引重建？ |

## 4. 开源项目思维（L3 触发）

如果需求有开源意图（产品/开源/发布关键词），检查：

- 目录规范：`docs/ examples/ packages/ apps/ scripts/ benchmark/ design/ .github/ docker/ helm/`
- 文档：README（安装/使用/API/示例）、LICENSE、CHANGELOG、CONTRIBUTING、
  CODE_OF_CONDUCT、SECURITY、Issue/PR 模板、Roadmap
- 工程：CI、语义化版本、Docker/Compose/Helm、覆盖率、Dependabot/Renovate、SBOM
- 无开源意图时不加载（克制）

## 5. 商业项目思维（L2+ 触发）

- 商业能力：License 分级（Community/Enterprise）、多租户
  （tenant/organization/workspace）、Billing/Subscription、Quota/Usage、
  API Key/Webhook、SSO/RBAC/Audit、Feature Flag、插件/Marketplace
- **预留 vs 实现**（关键克制规则）：
  - **低成本预留**（设计时考虑，必须做）：`tenant_id` 进主查询/唯一索引、
    事件带组织上下文、审计字段、API 版本化
  - **高成本实现**（有明确商业信号才做，禁止提前设计）：完整 Billing/
    Subscription/计费系统、插件市场、多级 License
- 无商业化信号时禁止设计 Billing（"先测量，后收费"）

## 6. 过度产品化红线（克制原则）

- L0 需求禁止建产品结构（无 packages/enterprise/plugins 目录）
- 无第二真实使用方不抽象模块（三次原则）
- 无商业化信号不设计 Billing/Subscription
- 无开源意图不生成开源文档全套
- 推演出的隐含需求必须**显式标注为待确认**，不得直接实现
