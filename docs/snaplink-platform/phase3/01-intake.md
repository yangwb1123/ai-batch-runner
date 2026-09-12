## Interpreted Goal

在本阶段先做**架构 intake（非实现）**：冻结 `sverpweb` 采购审批试点的通用 Workflow 契约，并复用现有平台服务能力完成第一切片验证。  
范围限定为：

- 通用 Workflow 契约（状态、实例、WorkItem、审批决策、执行/补偿边界）
- `sverpweb` 首个试点：采购申请与审批（`draft -> pending_approval -> approved/rejected`）
- 与现有服务集成与故障策略、迁移顺序、验收门禁冻结

不在本阶段设计数据库实现细节或跨仓库全面改造实现。

## Actors and Responsibilities

- **业务角色**
  - 申请人（user/tester）：发起采购申请
  - 审批人（岗位/组织角色）：审批决定
  - 平台管理员：配置、可观测、治理
  - 审计/合规角色：基于审计真相核验

- **服务角色**
  - `snaplink`：身份、租户、授权与撤销（权限决策权威）
  - `sverpweb-stable`：采购业务与第一阶段流程执行；不承载组织/租户权威核心事实
  - `aero-vault`：文件/资产生命周期与 ACL 能力
  - `aero-im`：通知交付能力（非审批决策）
  - `snaplink-audit-governance`：不可变审计事实源，不承载业务状态
  - `aero-id`：组织/部门长期权威目标，当前受限于现状阶段性衔接
  - `snaplink-console`：管理视图和审计时间线展示面

## Required Scope

### 范围边界（本轮）
- 冻结 **Workflow Contract**：ProcessDefinition / ProcessInstance / WorkItem / ApprovalDecision / Execution / Compensation 的字段和状态边界（不与 user/ACL/audit 表混写）
- `sverpweb` 采购流使用现有表与状态机作为试点契约，保留现有 API 形态，仅补齐契约与边界定义
- 审批结果与业务执行分离：`approved` 不等于 `executed`
- 工作流与业务、资产、审计、组织引用解耦

### 复用策略（限定）
- 授权：`snaplink`
- 审计发送：`sverpweb` outbox + `snaplink-audit-governance`（治理真源）
- 资产与 ACL：`aero-vault`
- 通知：`aero-im`
- 组织/部门：优先使用 `aero-id`，`sverpweb` 过渡实现不作为长期标准
- 管理面：`snaplink-console`

### 失败策略（冻结）
- 授权：`fail_closed`
- 审计关键事件：`durable_async`（本地 outbox 入账后异步投递）
- 通知：`best_effort_async`
- 部门/组织：跨服务主键使用 `tenant_id + department_id`，禁止名称作为稳定约束主键

### 迁移顺序（先契约后实现）
1. 锁定 ExecutionContext/Workflow/审核事件/部门租户标识 schema
2. 在 sverpweb 采购试点中对齐事件与状态边界
3. 统一组织 ID 映射策略并冻结（避免按姓名映射）
4. 形成 `snaplink -> sverpweb -> audit-governance -> aero-im -> console` 的验证链路
5. 通过验收门禁后再扩展至更复杂流程（会签/超时/补偿）

### 首阶段验收门禁（建议）
- Workflow 与业务域状态不混用数据库边界
- 任一业务状态变化有唯一权威写入者
- 审计来源不作为业务状态源
- tenant/dept 由服务端上下文校验，禁止前端文本字段改写权限语义
- 保持 `sverpweb` 当前**单租户**不做跨租户改造（不声明其为多租户）

## Explicit Non-Goals

- 不实现代码层变更（仅 intake）
- 不把工作流内嵌入 `user/acl/audit` 表
- 不用审计记录代替业务数据库真值
- 不用 LLM/自然语言作为授权依据
- 不以部门名称作为稳定主键（部门 ID 与 tenant_id 优先）
- 不做一次性全部仓库改造；按单仓库、先冻结后执行

## Evidence and Unknowns

### Verified
- `docs/snaplink-platform/PLATFORM_ARCHITECTURE.md` 明确 Workflow 与业务事实分离、审计事实源边界、租户/组织/授权边界
- `decisions/01~05` 与 `change-manifest.yaml` 已裁决首切片方向，`aero-vault`/`sverpweb`/`snaplink` 协作路径已明确
- `sverpweb` 采购审批代码 `routes/purchase-requests.ts` 有最小状态机与审批路径
- `sverpweb` 审计实现已具备 outbox + forwarder + governance 映射（`audit/*` 相关文件）
- `snaplink-audit-governance/README.md` 明确其不可变审计治理定位（非业务状态源）
- `aero-vault/internal/access/*` 有 tenant-scoped 与授权动作模型
- `build/profiles` 文档已定义 minimal/full/production 与生产约束边界

### Transitional
- `sverpweb` 仍有本地 `org.ts`/`file-shares.ts` 的组织与共享表，属于过渡实现
- 组织/部门同步仍存在按名称匹配 ID 的桥接逻辑（`findDepartmentByName`）
- `sverpweb` 审计发送有 best-effort 历史行为，关键事件 required/ledger 策略尚需收紧
- `sverpweb` 当前为单租户约束（`MULTI_TENANT=true` 已被拒绝）

### Proposed
- 将流程模型独立为版本化通用 Workflow 契约，并将首阶段事件映射到该合同
- 将组织权威与部门引用最终收敛为 `aero-id` 与 `aero-vault` 的稳定 ID 契约
- 统一“组织/部门 ID 映射”与“工作流事件 schema”两份版本化合同文档
- 补齐“失败策略矩阵”和 `make ci`/回归可执行门禁映射

### Missing
- 当前缺少独立落盘的 Workflow Contract v1 文档（字段、状态、事件码、错误码）
- 缺少组织与部门跨服务 ID 契约的最终签字版（`aero-id`↔`aero-vault`↔`sverpweb`）
- 缺少采购审批首切片的独立端到端验收脚本（含超时、拒绝、撤销、补偿场景）
