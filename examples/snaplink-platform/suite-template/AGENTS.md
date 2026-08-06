# Platform Agent Rules（组合仓库执行契约）

本仓库管理 6 个独立产品的组合关系。下列规则是 Agent 在本仓库工作的硬约束。

## 1. 独立性优先

1. Treat every repository as an independently deployable product.
2. Do not assume all other Snaplink/Aero services are installed.
3. Keep external integrations optional and replaceable.
4. Core domain code must depend on ports/interfaces, not concrete sibling projects.
5. Prefer OAuth 2.1, OIDC, SCIM 2.0, CloudEvents, OpenAPI, OpenTelemetry standards.
6. Never access another project's database.

## 2. 变更粒度

7. A single-project requirement should not trigger unrelated repository changes.
8. Identify whether a change belongs to: core capability (单项目),
   optional integration (可选适配器), or suite composition (组合部署).
9. Cross-project communication must use versioned APIs or events.

## 3. 组合与降级

10. Degraded operation must be defined when an optional dependency is unavailable.
11. Audit integration levels: L0 local log / L1 standard protocol / L2 governance
    adapter. The suite may mandate L2 for the full topology, but product cores
    must only depend on the generic AuditPublisher contract.
12. Notification failures must not block the primary business flow.
13. Do not create synchronous circular dependencies between services.

## 4. 版本与兼容

14. Component versions must not be lockstep; only the suite BOM pins a window.
15. The suite maintains an explicit compatibility matrix (compatibility.yaml);
    every change to it must pass scripts/suite-check.py.
16. Contract changes require compatibility analysis and versioning;
    old client versions must keep working during the upgrade window.

## 5. 证据纪律

17. Every compose topology must be smoke-tested before it is documented as supported.
18. E2E scenarios live in e2e/ and must reference real service versions from the matrix.
19. AI 产出是提案：拓扑、兼容窗口、事件目录变更须人工批准后提升。
20. 生产凭据/供应商未验收项不得标注为已验收（沿用各仓库标注纪律）。
