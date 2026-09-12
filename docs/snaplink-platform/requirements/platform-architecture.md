# 平台总体架构与通用业务流程实现需求

在已有 Snaplink Platform 组合架构基础上，继续推进一条可落地的第一阶段实现契约：

1. 以插件/模块方式组合 identity、tenant、organization、asset、workflow、audit、telemetry 和 agent/automation 能力；
2. 保持高内聚低耦合：每个领域拥有自己的事实、存储、API、事件和测试，跨域只依赖版本化契约；
3. 提供通用的企业业务流程模型，支持申请、审批、会签/或签、确认、执行、撤销、超时、补偿和恢复；以采购申请→部门审批→财务审批→采购确认作为首个垂直切片；
4. 支持用户、部门、租户内部公共资产、跨租户显式共享和互联网公开资源，文件/媒体最终由 aero-vault 授权；
5. 所有用户、服务、定时任务、自动化和 Agent 操作均可通过统一 operation context 追踪，接入 snaplink-audit-governance 的 durable outbox、不可变账本、WORM、审批和恢复证据；
6. 统一 OpenTelemetry 指标/Trace 与业务审计，但不把技术埋点当作审计事实；
7. Agent 只能使用短期、限租户、限资源、限动作的 Capability Grant，不能使用人的长期 Token、提权或自我审批；
8. 支持 minimal/full/production 三个真实构建 Profile，并能按静态模块、预编译模块和外部 typed RPC/mTLS 插件独立扩展；
9. 当前 sverpweb 仍是单租户，第一阶段不得仅迁移用户和菜单就打开共享多租户；
10. 本批次先冻结跨仓库契约、责任边界、失败策略、迁移顺序和验收标准，再进入分仓实现，不直接对所有仓库做无边界修改。

参考并核对：

- docs/snaplink-platform/PLATFORM_ARCHITECTURE.md
- docs/snaplink-platform/BUILD_PROFILES_PLUGIN_ARCHITECTURE.md
- docs/snaplink-platform/COMBINATION_MODEL.md
- docs/snaplink-platform/MULTI_REPO_OPERATIONS.md
- docs/snaplink-platform/SCENARIO_ANALYSIS.md
- docs/snaplink-platform/decisions/01-intake.md 至 05-changes.md
- docs/snaplink-platform/decisions/change-manifest.yaml
- examples/snaplink-platform/platform-map.yaml
- /home/dwp/snaplink-audit-governance/README.md（只作已实现能力证据）
- /home/dwp/aero-vault/internal/access（只作已实现资源授权基础证据）
- /home/dwp/workspace/job/sverpweb-stable/apps/api/src/audit 和 routes（只作当前状态证据）

要求：不得把 Workflow 直接塞入用户、ACL、资产或审计表；不得把 audit-governance 当作业务状态源；不得把 Agent 的自然语言输出当作授权依据；不得以部门名称代替稳定的 tenant-scoped department_id。
