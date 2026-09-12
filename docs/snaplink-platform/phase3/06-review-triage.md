# Adversarial Review Triage

> 状态：PROPOSED / REVIEW-TRIAGE；此文档不是通过门，而是对 Agent review 工件进行证据分级。

## Review Run Evidence

- `adversarial_review` 通过 `ai-batch-runner` 的 meta stage 执行了 4 个角色：`backend_engineer`、`security_engineer`、`capability_architect`、`database_architect`。
- 角色输出已落盘于 `phase3/reviews/`，但其内容仍是不可信提案，必须经过本文件和独立 gate 复核。
- 第二轮 meta/gate 因 prompt 超过 pi 的 122880 bytes 硬上限而 fail-closed，未伪造 PASS。
- `summary.json` 已通过机械 `check-decision-summary.py`，但机械通过不等于架构批准。

## Accepted or Partially Accepted Findings

### 1. Workflow 与业务/ACL/Audit 表分离

- **Disposition：ACCEPT**
- Workflow 只拥有 ProcessDefinition、ProcessInstance、WorkItem、Decision、Execution、Compensation；采购聚合、文件 ACL 和审计账本仍由各自权威服务拥有。
- 领域服务通过 typed command 执行业务变化，Workflow 不直接写业务表。

### 2. Persistence Design Gate

- **Disposition：ACCEPT AS IMPLEMENTATION GATE**
- 任何新增核心表前必须完成 Persistence Design Report：DTO/Domain/Persistence/Read 四模型分离、主键/业务键/幂等键、金额/时间/状态、唯一约束、查询索引、历史、删除和 expand/contract migration。
- 该 finding 是编码前门禁，不代表当前已完成数据库设计。

### 3. Agent/Automation Provenance and Least Privilege

- **Disposition：ACCEPT**
- Agent、service、automation、human 必须区分 actual executor、initiator、on_behalf_of、approval、grant、workflow 和 run。
- Agent 只能使用短期、限租户、限资源和限动作 Grant；不能自我审批、提权或使用人的长期 Token。

### 4. Tenant and Department Boundary

- **Disposition：ACCEPT**
- 所有流程、资产引用、审计和授权上下文必须绑定服务端解析的 `tenant_id`。
- 部门引用使用 `tenant_id + department_id`，名称只用于展示；历史无法可靠映射的数据标记 unresolved，不猜测。

### 5. Outbox and Recovery

- **Disposition：ACCEPT WITH SCOPE**
- 业务变更与本地 audit outbox 同事务；远程 audit 投递 durable_async；通知可 best-effort。
- audit-governance 的 replay 只做无副作用重建；实际补偿/恢复由业务领域命令执行。

## Rejected or Untrusted Findings

### 1. Review Target Drift

`backend_engineer` 的主体输出变成了“请提供具体任务”，并没有审查本次平台变更；不能作为批准依据。

### 2. Incorrect Project Ownership

`capability_architect` 把 Agent Grant/Agent rule enforcement 归给 `aero-im`，与总体架构中的 Agent Runtime/Workflow 边界不一致；`aero-im` 仅负责通知，不能成为 Agent 权限源。该结论拒绝。

### 3. Unrelated or Hallucinated Evidence

`database_architect` 输出混入与采购/平台无关的内容（包括不属于本需求的 order_item、足球/搜索等文字），不能作为事实、容量或数据库现状证据。仅保留其一般性 persistence gate 建议，不采纳任何现状断言。

### 4. ai-batch-runner 自身安全审查偏移

`security_engineer` 大量审查了 ai-batch-runner 的 runner、memory 和 role system，而不是本次平台业务合同。其关于 fail-closed、Agent provenance、租户隔离和敏感字段不入审计的通用建议可保留；涉及目标项目现状的断言必须回到实际仓库重新验证。

## Gate Decision Input

独立 gate 只能基于：

1. `01-intake.md` 与 `02-scenario.md`；
2. `03-capabilities.md`、`04-composition.md`、`05-changes.md`；
3. 本 triage 的 accepted/rejected disposition；
4. `platform-map.yaml` 和现有项目文档中的 Verified 证据。

不得因为角色输出存在就将其视为已验证事实。若证据不足，决策应保持 Proposed/Missing，不能自动扩大实施范围。
