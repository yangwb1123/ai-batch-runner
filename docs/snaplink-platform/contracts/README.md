# Snaplink Platform Contracts

> **状态：DRAFT / PROPOSED**
>
> 这些文件是跨仓库契约草案，不是任一业务仓库的数据库模型。代码落地前必须经过人工批准、版本锁定和对应仓库的契约测试。

## Contract registry

| 契约 | 文件 | 首个消费者/提供者 | 目的 |
|---|---|---|---|
| Execution Context | [`EXECUTION_CONTEXT_V1.md`](./EXECUTION_CONTEXT_V1.md) | 所有服务 | 统一租户、身份、操作、委托和 Trace 上下文 |
| Workflow | [`WORKFLOW_CONTRACT_V1.md`](./WORKFLOW_CONTRACT_V1.md) | `sverpweb-stable` | 通用申请、审批、执行、撤销和补偿模型 |
| Audit Provenance | [`AUDIT_PROVENANCE_V1.md`](./AUDIT_PROVENANCE_V1.md) | `snaplink-audit-governance` | 人、服务、自动化和 Agent 的审计来源 |
| Agent Capability | [`AGENT_CAPABILITY_V1.md`](./AGENT_CAPABILITY_V1.md) | Agent Runtime / Domain PEP | 限租户、限资源、限动作的临时能力 |
| Plugin Manifest | [`PLUGIN_MANIFEST_V1.md`](./PLUGIN_MANIFEST_V1.md) | 各 Profile/组合仓库 | 模块、插件、Profile、依赖和失败策略 |

## Contract rules

1. 契约只描述跨边界语义，不描述对方数据库内部结构。
2. 每个契约有独立版本；运行中的流程、事件和插件锁定创建时版本。
3. 向后兼容字段先 optional，再通过迁移和对账变为 required。
4. 未知、缺失或不兼容的关键字段必须 fail closed。
5. `tenant_id` 由服务端上下文确定；客户端字段只能用于一致性校验。
6. 所有命令具备 `operation_id`、幂等键和版本条件；所有事件可关联因果链。
7. 契约通过 JSON Schema/OpenAPI/AsyncAPI/Protobuf 和真实组合 E2E 验证。
8. 这些文件不授权直接跨仓库改代码；变更必须有 Change Manifest。

## Promotion states

```text
DRAFT
  → EVIDENCE_REVIEWED
  → HUMAN_APPROVED
  → CONTRACT_LOCKED
  → IMPLEMENTED
  → COMPOSITION_VERIFIED
```

任何提案、审计角色输出或 Agent 建议在 `HUMAN_APPROVED` 前都不能被当成生产事实。
