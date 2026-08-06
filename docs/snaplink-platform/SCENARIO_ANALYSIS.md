# Snaplink 平台场景分析与建议（6 仓库）

> 生成方式：本文件由 ai-batch-runner 对 6 个仓库做证据扫描后人工整理。
> 所有结论标注来源（仓库/文件/测试数）；未核实项标 **Proposed**，不作为事实。

## 0. 现状盘点（证据扫描结果）

| 仓库 | 语言 | 测试量 | 角色 | 成熟度信号 |
|---|---|---|---|---|
| `~/workspace/demo/snaplink` | Go | 1456 | 认证授权中心（OAuth2/OIDC SSO SDK + sso-server） | 最成熟；大量 domains、gate 文化（AGENTS.md 硬门禁） |
| `~/workspace/demo/snaplink-console` | Flutter Web | 111 | 管理端 + 登录/门户/开发者/设备/设置入口 | 能力驱动导航（运行时 `/api/v1/admin/endpoints` 清单），有审计页 |
| `~/workspace/demo/snaplink-audit-governance` | Go | 19 | 审计与治理平台（不可变账本/哈希链/Legal Hold/导出/完整性校验/Outbox relay） | **架构设计期 + 单节点参考实现**；5 个 ADR；`docs/BENCHMARKS.md` |
| `~/aero-id` | Go | 631 | 账户业务域（文件/消息/评论归属账户，身份在外部 SSO） | 已有 `internal/connector/auditgovernance/`（publisher/oauth），Snaplink gRPC 校验 |
| `~/aero-vault` | Go | 185 | 文件平台（REST/S3/WebDAV/MCP + RAG） | 已有 `internal/auditgovernance/token.go`，Snaplink 浏览器流认证 |
| `~/aero-im` | Rust | 大功能矩阵 | IM + 直播 + 通知渠道 | 附件经 Aero Vault BlobStore（Snaplink client_credentials）；功能矩阵逐项 ✅ 但含"仅本机验证"项 |

**已存在的集成（Verified）**：aero-vault 认证走 Snaplink 浏览器流；aero-im 附件走 aero-vault BlobStore + Snaplink client_credentials；aero-id/aero-vault 已有 audit-governance 连接器雏形；console 已有审计日志服务页（`lib/services/audit_log_service.dart`）。

**已发现的缺口（Proposed，需逐项验证）**：
1. snaplink 主仓库未见 audit-governance 连接器（域内只有本地审计日志概念）——**认证中心的策略/RBAC/token 撤销操作目前可能不进入统一审计账本**。
2. aero-im 未见向 audit-governance 发送事件的连接器（只有内容治理逻辑）。
3. console 审计页的数据源边界未确认：直接查 audit-governance API 还是各服务自曝审计接口。
4. aero-id / aero-vault 仓库内嵌的 `pi-batch.py` 为旧版（7 月 10 日快照，落后主仓库）。
5. aero-im 的跨主机/公网验收项仍标注"待验证"（NAT/TURN/供应商凭据）。

## 1. 架构定位建议（每个服务一句话）

> 定位模型（见 COMBINATION_MODEL.md）：6 个项目是**独立产品**，通过标准
> 协议按需组合。下图是 full 组合拓扑下的职责图，不是强制整体部署模型。

```
                    ┌─────────────────────────────────────────────┐
                    │          snaplink-console（平台控制面）        │
                    │  /admin/ 聚合各服务管理能力 + 统一审计时间线    │
                    └──────┬──────────┬──────────┬──────────┬─────┘
                           │          │          │          │
              ┌────────────▼───┐ ┌────▼────┐ ┌───▼────┐ ┌──▼──────────┐
              │ snaplink (IdP) │ │ aero-id │ │ aero-  │ │ aero-im     │
              │ 认证/授权/租户  │ │ 账户域  │ │ vault  │ │ 消息/通知    │
              └───────┬────────┘ └────┬────┘ │ 文件    │ │ (审计告警渠道)│
                      │  OIDC/        │      └───┬────┘ └──────┬──────┘
                      │  client_creds │          │ BlobStore   │ 附件
                      ▼              ▼          ▼              ▼
        ┌───────────────────────────────────────────────────────────┐
        │  snaplink-audit-governance（唯一审计事实源）                 │
        │  各服务 Outbox → relay → 不可变账本 → 查询/导出/合规/回放    │
        └───────────────────────────────────────────────────────────┘
```

- **snaplink = 信任根**：认证、授权、租户上下文、scope 注册表。所有服务不得自建身份。
- **aero-id = 账户业务域**：持有业务账户数据，行级引用 snaplink `uid`（已是现状）。注意其 README 中的 "files/messages/comments" 表述是历史职责——**文件事实在 aero-vault、消息事实在 aero-im，aero-id 只做账户维度聚合视图**，避免三份真相（Proposed，需核对 aero-id 当前代码是否还在存文件/消息实体）。
- **aero-vault = 文件事实源**：上传/删除/共享/导出/RAG 索引；向审计发送文件生命周期事件。
- **aero-im = 消息与通知渠道**：会话/消息/直播；附件引用走 vault；**通知 ≠ 审计事实**——审计告警可以经 aero-im 送达管理员，但事件记录必须先进 audit-governance。
- **snaplink-audit-governance = 审计事实源**：全平台唯一审计账本 + 治理（Legal Hold/留存/完整性/导出）。当前是**单节点参考实现，是全平台最高风险依赖**——所有服务都依赖它而它最年轻。
- **console = 平台控制面**：保持"不猜测接口"原则，扩展为按服务能力清单聚合（账户/存储/消息/审计各一张 tab），统一审计时间线页直接读 audit-governance。

## 2. 六个关键建议（按优先级）

### P0-1 先生产化 audit-governance，再扩大依赖面
它是唯一"设计期"组件却要被 5 个服务依赖。用 backend-implementation-pipeline（plan 含强制持久化设计报告 + backendquality/completion 双门禁 + 对抗审查 + gate）推进：
- 哈希链 checkpoint 密钥管理、多副本一致性（已有 state snapshot 乐观锁）、归档加密导出（已有）
- 性能基线（docs/BENCHMARKS.md）转成可重复的基准门禁
- 事件 schema 注册表从 ADR 落到代码（ADR-0002）
- 先让 snaplink 和 console 接入，跑通闭环，再铺到 aero-*

### P0-2 审计接入按部署等级设计（full 组合的规则，不是产品硬编码）
定义审计三级：L0 本地日志 / L1 标准事件协议 / L2 governance 适配器。
**各项目核心只依赖 AuditPublisher 端口**；full 组合部署时统一切 L2 汇入
audit-governance（即“所有审计操作都通过 audit-governance 管理记录”的
正确实现方式）。事件类型清单（认证/授权变更/账户/文件/消息/管理操作）
作为 suite 契约文档维护，每服务对照自检：
- 统一接入模式：Outbox 表 + relay（aero-id 的 `connector/auditgovernance`
  已是模板）→ audit API 幂等接入 → 死信可见；审计不阻塞业务主流程
- **门禁（full 组合）**：新增写操作/管理操作没有审计事件类型 = 评审
  FAIL。此规则可沉淀进 learn 流程（Rule draft）进后端注册表。

### P0-3 平台级 service-to-service 凭证与 scope 矩阵
- 统一 client_credentials 流程（aero-vault 的 tokenSource 已是模式）
- 在 snaplink 建立平台 scope 注册表文档：`vault:blob:read/write`、`im:room:write`、`audit:event:ingest`、`account:uid:resolve` 等；每服务最小权限矩阵表
- 禁止跨服务用用户 token 冒充服务身份（BFF 模式除外，需评审）

### P1-4 console 扩展为平台控制面
- 保持能力清单驱动：每个服务暴露自己的 admin API + 运行时能力清单（snaplink 模式复制到 aero-*）
- 新增/整合页面：账户管理（aero-id）、存储管理（vault）、消息治理（im）、**统一审计时间线**（audit-governance，按用户/租户/资源关联聚合）
- 前端门禁全套：`uispacing/uicolor/uistyle` 产物验证 + `uicheck` 项目扫描 + frontend-engineering-review（异步/防抖/上帝文件）

### P1-5 统一登录体验与令牌生命周期
- 所有服务浏览器流指向 console 的 `/login/`（品牌化 + MFA/WebAuthn + 同意）
- 令牌撤销（snaplink）后各服务会话立即失效的验收测试（跨服务联调用例）
- aero-im 已消费 Snaplink SDK（fix: consume generated Snaplink SDK directly）——对齐其他服务

### P2-6 高风险操作编排与告警
- 删除/导出/封禁等操作走"console 发起 → 授权 → 服务执行 → 审计事件 → （可选）aero-im 通知"闭环；先不做跨服务分布式事务，用 outbox + 幂等 + 人工确认
- 审计规则告警（如"连续 N 次认证失败""break-glass 使用"）→ aero-im 通知管理员，作为后续方向（P2）

## 3. 风险与克制

- **防两种极端**：六项目互不理解（ID/Token/租户/权限名/审计无法关联）→
  用最小共享契约（见 COMBINATION_MODEL.md 第 4 节）；六项目像单体互相绑定
  （改一升全、单点拖垮、无法裁剪）→ 组合式架构 + 独立版本 + suite 矩阵。
- **工作流风险分级：L3 平台产品**（合规、不可变账本、多租户）——关键变更走对抗审查 + gate；关键路径设审批点（`approval`）。
- **产品化分级：L2-L3**。克制：tenant_id 进主查询/唯一索引、事件带组织上下文、API 版本化是**低成本预留（必做）**；Billing/插件市场是**高成本实现（禁止提前设计）**。
- **AI 产出是提案**：audit 事件类型清单、scope 矩阵等文档先按证据标准（Verified/Partial/Missing/Proposed）标注再提升。
- **不动仓库硬门禁**：每个仓库自己的 `make ci`/`cli.py check`/AGENTS.md 不变量优先于任何 AI 建议。

## 4. 立即可以做的三件事（零成本）

1. 同步 ai-batch-runner 新版本到 aero-id / aero-vault（旧内嵌 pi-batch.py 落后；`pip install -e .` 或复制薄壳 + pbatch/ + pi-batch.yaml）。
2. 用 `pi-batch.py assess` 把 P0-1/P0-2 需求过一遍评估（本场景评估结果见 OPERATIONS_PLAN.md 附录）。
3. 初始化 snaplink-suite 组合仓库（模板在 `examples/snaplink-platform/suite-template/`），用 campaign 对 audit-governance 跑第一轮方向发现（配置已就绪：`examples/snaplink-platform/campaign-audit-governance.yaml`）。
