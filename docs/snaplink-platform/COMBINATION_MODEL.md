# 组合式架构建议（6 项目 = 独立产品 + 组合协议）

> 修正结论：6 个项目是**可独立开发、独立部署、独立演进**的通用产品，
> 通过标准协议按需组合成不同业务系统。**不是**一个必须整体开发部署的
> 平台。过度统一会带来发布耦合与部署复杂化；完全不共享契约会产生
> 六套标准。目标是：内部低耦合、协议可组合、部署可裁剪、版本可兼容。

## 1. 三类定位

| 类别 | 项目 | 要求 |
|---|---|---|
| 独立基础服务 | snaplink（认证授权）/ aero-id（账户身份）/ aero-vault（文件）/ aero-im（消息通知）/ snaplink-audit-governance（审计治理） | 可单独运行/测试/部署/升级；不要求其他 5 个存在；不访问他库；提供 API/SDK/事件/扩展点 |
| 独立管理应用 | snaplink-console | 首先是 snaplink 管理端；组合场景下通过**能力发现**（运行时能力清单）增加 aero-* 管理入口；不默认强依赖所有服务 |
| 组合仓库 | snaplink-suite（新增，不承载业务代码） | compose 多拓扑 / Helm / 兼容矩阵 / 契约测试 / E2E / 版本兼容 |

## 2. 核心能力与集成能力分离（六边形架构）

每个项目按此结构：核心（domain/application/infrastructure/api）与
integrations（snaplink / aero-id / aero-im / audit-governance）分离。

- 核心代码只依赖**端口**：`AuthorizationProvider` / `AuditPublisher` /
  `NotificationPublisher` / `StorageProvider`
- 适配器可选可替换：Local / Jwt / Snaplink；Noop / Local / Webhook /
  message-bus / SnaplinkAuditGovernancePublisher
- 部署时用配置选择：
  ```yaml
  integrations:
    authorization: { provider: snaplink }
    audit:         { provider: snaplink-audit-governance, transport: nats }
    notification:  { provider: aero-im }
  ```
  也可以：`authorization: local-jwt`、`audit: local-database`、
  `notification: smtp` —— 这才是真正的可组合。
- 禁止：核心领域代码 `new SnaplinkService()` 锁死组合；硬编码兄弟服务 URL。

## 3. 审计集成分级（"所有审计操作都进 audit-governance"的正确含义）

| 等级 | 模式 | 适用 |
|---|---|---|
| L0 | 本地数据库/结构化日志/OTel | 独立部署、开发测试 |
| L1 | 统一事件结构输出（Webhook/Kafka/NATS/HTTP），接收方不限产品 | 标准协议组合 |
| L2 | 适配器汇入 snaplink-audit-governance（幂等/哈希链/合规） | **full 组合部署的规则** |

- 各项目核心只依赖通用 `AuditPublisher` 契约；L2 是 suite 部署规则，
  不是产品硬编码规则。
- **审计不阻塞业务主流程**：业务事务内写 Outbox，同事务提交 →
  relay → 事件总线 → audit-governance；审计不可用不丢事件。
- 极高风险操作（删租户/重置主密钥）可用"同步最小审计确认 + 异步完整事件"。

## 4. 最小共享契约（防六套标准，但不绑定实现）

| 契约 | 内容 | 载体 |
|---|---|---|
| 身份上下文 | subject_id / tenant_id / organization_id / session_id / client_id / scopes / request_id / trace_id | HTTP 头/事件信封 |
| 权限命名 | `<domain>.<resource>.<action>`（identity.user.disable、vault.file.download、audit.event.read） | permissions.yaml |
| 事件信封 | id / type / version / source / tenant_id / time / trace_id / correlation_id / data | CloudEvents 风格 |
| 审计事件模型 | actor / subject / action / resource / result / authorization(decision_id) / request / change(before/after) / integrity | audit-event schema |
| 错误格式 | code / message / request_id / trace_id / details | OpenAPI |
| 分页 | items / page.cursor / next_cursor / has_more | OpenAPI |
| 行业标准 | OAuth 2.1 / OIDC / SCIM 2.0 / S3 API / OpenTelemetry / OpenAPI / AsyncAPI | — |

契约以**协议文档**形式存在于 suite 仓库（contracts/），各产品通过
适配器实现，不共享数据库、不共享代码、不同步发布。

## 5. 权限模型（不只要 RBAC）

RBAC（角色）+ ABAC（属性/环境）+ ReBAC（关系）组合：
```
allow download when:
    tenant matches
    AND (role contains file_admin OR relation contains owner OR shared_reader)
    AND classification policy allows device
```
统一授权接口（snaplink）：`POST /v1/authorization/check` 返回
decision + policy_id + **decision_id**；decision_id 进审计记录。

## 6. 跨系统流程 = 事件驱动，不是 console 串联调用

用户冻结：console → aero-id 冻结 → 发布 `identity.user.disabled` →
Event Bus → snaplink 撤 Token / aero-vault 取消分享 / aero-im 通知 /
audit-governance 记录。禁止 console 依次同步调用四服务（中间失败难恢复）。

同步依赖上限：console-bff → 各服务；业务服务 → snaplink（授权判断）；
其余状态变化走 Event Bus。禁止：audit 为写事件回调业务服务、
snaplink 每次校验实时调 aero-id、vault 下载路径串行调三四服务。

## 7. Agent 三层判断模板（任何需求的第一步）

1. **单项目能力**：能否作为当前项目独立能力完成？→ 只改当前仓库。
   （例：aero-vault 文件版本恢复 → 只改 aero-vault）
2. **可选集成能力**：是否需要事件/适配器？→ 改发布方 + 需要订阅的适配器；
   通知失败不影响主流程。（例：恢复后通知 → aero-vault 发通用事件 + aero-im 适配器）
3. **组合产品行为**：跨项目完整流程 → 有直接责任的项目 + suite E2E。
   （例：console 恢复文件 + snaplink 授权 + governance 记录 + im 通知）

**跨项目修改必须满足"确实需要改变双方契约"**，而不是顺手保持整体一致。
例：新增文件审计事件 → aero-vault 发布事件；audit-governance 若已支持
通用事件则**无需修改**；aero-im 不订阅则**无需修改**。

## 8. 版本与发布

- 组件独立语义化版本，**禁止锁步发布**。
- suite 维护兼容矩阵（BOM）：组件版本窗口 + 契约版本 + 集成等级，
  三者不混为一谈；`suite-check.py` 门禁保证矩阵与 compose 一致。
- 契约升级：兼容性分析 → 版本化 → 旧客户端兼容窗口 → 灰度/feature flag。

## 9. ai-batch-runner 如何支持这个模型

| 特性 | 组合模式下的用法 |
|---|---|
| campaign / advance | **默认单仓库**运行（第一层判断=单项目能力）；跨仓库组合需求在各产品仓库独立实现，suite 跑 E2E |
| classify / rules / assess | 每仓库独立；需求先过三层判断模板（assess 评估 + 影响范围识别） |
| 组合门禁 | suite 仓库 `suite-check.py` 验证器：compatibility.yaml 一致性 + compose 拓扑完整 |
| E2E 验证 | suite 仓库 E2E 清单（20 项边界场景，见 suite-template/e2e/README.md），引用真实版本 |
| 跨仓库契约 | 契约草案在各产品仓库产出（提案）→ suite 汇总为矩阵/事件目录变更 → 人工批准 |
| learn | 组合事故（E2E 失败、兼容窗口破坏、幂等缺陷）→ RCA → 规则草案 → 批准后并入各仓库注册表 |
| context | 每仓库 docs/agent-context/ 放"本产品职责边界 + 可选集成契约"，不假设其他服务存在 |
| 7×24 / status 看板 | 每仓库独立轮巡 + suite 仓库独立状态（矩阵版本/E2E 通过率） |

**与既有交付物的关系**：`docs/snaplink-platform/` 中的流水线、campaign、
247 脚本仍然有效，但语义从"统一平台推进"修正为"独立产品自治 + suite
组合验证"。`examples/snaplink-platform/suite-template/` 即组合仓库骨架。

## 10. 一句话总结

> 修改任何代码前，先判断它属于哪个领域、谁拥有数据、需要什么权限、
> 产生什么审计事件、影响哪些仓库、如何兼容和回滚——但**默认只改当前
> 仓库**；只有确实需要改变双方契约时才跨仓库，且由 suite 组合验证兜底。
