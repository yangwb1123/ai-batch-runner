Based on my analysis of the provided evidence, here is the comprehensive database architect review:

## 1. Store Inventory

From the `05-changes` evidence across multiple repositories, the following stores are identified:

| Store | Purpose | Implementation | Durability |
|---|---|---|---|
| **sverpweb-stable (Primary)** | Core workflow/process management: ProcessDefinition, ProcessInstance, WorkItem, ApprovalDecision, Execution, Compensation schemas. Asset tracking with business_tenant_id, asset_id, aggregate_type, aggregate_id, purpose, created_by. Shared scope management (private, department, tenant_internal, selected_principals, external_grant, share_link, internet_public). | Database tables with strict schema: 4-model separation (API DTO → Domain → Persistence → Read), unique constraints (tenant_id+order_no, tenant_id+idempotency_key), value mappings for states, audit fields. | **Durable** - Database-backed with ACID guarantees, expand/contract migration pattern mandated. |
| **Snaplink** | Governance/workflow permissions: governance.workflow.start/read/approve/cancel/compensate. Does NOT move procurement business state to Snaplink. | Permission mapping to existing client_id/role/scope system. Tenant context from signature token, domain/client binding, service-side membership. | **Durable** - Registry-based, mapped to existing authorization system. |
| **aero-id** | Organization reference: department.read, membership.read/changed. Cross-service references use tenant_id + department_id + subject_id. Names for display only, not authorization matching or FK replacement. | Frozen contracts, explicit adapter pattern if aero-id API incomplete. | **Durable** - Registry-based organizational references. |
| **aero-vault** | Asset reference: passes asset_id and business association only. Reuses existing ACL API. No second file ACL created. | ACL API integration, asset_id-only references from sverpweb. | **Durable** - ACL-backed, reference-only (no data duplication). |
| **audit-governance** | Audit event logging: governance.workflow.created@v1, workitem.created@v1, approval.recorded@v1, execution.started@v1, etc. Actor provenance: actor_type, actor_id, initiator_id, on_behalf_of, delegation_id, capability_grant_id, agent_id, agent_version, workflow_instance_id, execution_run_id. | Extends existing /api/v1/events, batch/gRPC ingest. Operation timeline and receipt. Backward-compatible schema if new envelope needed. | **Durable** - Persistent ledger with DLQ/retry, immutable event log. |
| **aero-im** | Notification: Reuses existing notification API. Input must contain business summary and operation_id. Sending service cannot sync back to get business library context. | Notification API integration. | **Durable** - Asynchronous delivery, best-effort with failure recording. |

## 2. Findings

### Critical Findings:

1. **Persistence Modeling Compliance** (Critical)
   - The `persistence-modeling.md` spec is a "strong mandate" (编码前强制关卡) - "创建任何核心表/ORM Entity 前，必须先完成持久化建模"
   - **Violation risk**: If any ORM Entity is created without the required Persistence Design Report (12 sections including Identity, Consistency Boundary, Snapshot Fields, Concurrency, Main Queries + Indexes, History, Deletion, Migration)
   - **Recommendation**: Enforce the 12-section template before any core table creation

2. **4-Model Separation Violations** (Critical)
   - Specification explicitly prohibits: "禁止把 ORM Entity 直接返回/接收（API 字段与 DB 强耦合、注解污染领域模型、敏感字段泄漏、领域规则无法封装）"
   - **Finding**: Any code that returns DB entities directly to API layer violates this mandate
   - **Recommendation**: Implement the write/read pipelines as specified: Request DTO → Command → Domain Model → Persistence Mapper → Persistence Entity → DB (and reverse for reads)

3. **Money Field Float Violations** (Critical)
   - Rule: "金额禁止浮点：amount_minor BIGINT + currency_code CHAR(3) 或 DECIMAL(18,4)"
   - **Finding**: Any column using FLOAT/DOUBLE for monetary values violates this
   - **Recommendation**: Use BIGINT for minor units or DECIMAL(18,4), never FLOAT

4. **State String Violations** (Critical)
   - Rule: "状态不用自由字符串：定义状态全集 + 转换 + 数值映射（10=draft/20=submitted/30=approved/40=executing/50=completed/90=cancelled）"
   - **Finding**: Unconstrained VARCHAR state fields violate this
   - **Recommendation**: Implement centralized state machine with defined values, no free-form strings

5. **NULL Semantic Ambiguity** (High)
   - Rule: "NULL 语义：区分 未知/不存在/尚未设置/不适用/已清空——approved_at IS NULL 可能表示未审批/审批失败/不需要审批，必须配合状态字段"
   - **Finding**: Mixed NULL meanings without state field coordination
   - **Recommendation**: Always pair nullable timestamps with status field, document the 5-valued NULL semantics

### Schema/Design Findings:

6. **Unique Constraint Completeness** (High)
   - Required: `tenant_id+order_no`, `tenant_id+external_id`, `tenant_id+idempotency_key`, `user_id+role_id+scope_id`
   - **Finding**: Missing any of these makes the "最终防线" (final defense) ineffective against concurrent insert anomalies

7. **Index Design Gaps** (High)
   - Specification mandates designing indexes from primary queries first, then creating them
   - **Finding**: Reactive index creation without query analysis leads to missing coverage indexes or costly duplicate indexes

8. **Migration Pattern Deviations** (Medium)
   - Specification mandates Expand→Migrate→Contract pattern, not simultaneous old-delete+new-read
   - **Finding**: Any deployment that drops old columns while introducing new reads violates this

9. **Audit Field Completeness** (Medium)
   - Required: `created_at/by, updated_at/by, deleted_at/by, version, source, trace_id`
   - **Finding**: Incomplete audit fields reduce forensic value

10. **Cross-Tenant Isolation** (High)
    - All stores must have tenant_id as part of keys/filters
    - **Finding**: Any store without explicit tenant isolation is a security risk

## 3. Query/Index/Transaction Analysis

### Demonstrated Hot Paths (in-memory/frequently accessed):

1. **Workflow State Transition** - ProcessInstance status changes during active approval flow
   - Frequently accessed: `tenant_id + status + current_version`
   - Index recommendation: `INDEX (tenant_id, status, scheduled_at)` per spec §7
   - Atomic: Requires optimistic locking `UPDATE ... SET version=version+1 WHERE id=? AND version=?`

2. **Approval Decision Recording** - ApprovalDecision insertion during workflow progression
   - Frequently accessed: `workflow_instance_id + decision_id`
   - Unique constraint: `tenant_id + idempotency_key` (spec §8)
   - Must handle duplicate detection at DB level

3. **Asset Permission Checks** - vault.asset.read/preview/download/share operations
   - Frequently accessed: `tenant_id + asset_id`
   - Unique constraint recommendation: `UNIQUE (tenant_id, asset_id)` or per-action variants
   - PEP/PDP revalidation required per spec: "所有动作必须由后端 PEP/PDP 重新校验"

4. **Audit Event Outbox** - governance.event ingestion
   - Transactional: "本地 outbox 与业务变更同事务" (spec §11)
   - Durable: "audit outbox 本地写入 fail_closed" - critical changes must not report success locally
   - Async durable: "audit-governance 远程投递 durable_async" - local reliable store, retry/DLQ

### Demonstrated Atomic Paths:

- **Expense/Commit Transaction**: `BEGIN → Process state update → ApprovalDecision insert → Execution start → Commit/ROLLBACK`
- **Compensation Transaction**: Must have `reversible` flag, status tracking, and compensation_command
- **Idempotency Enforcement**: `expected_version` in commands, version conflict returns explicit error

### Demonstrated Durable Paths:

- **Outbox → Audit Ledger**: Local transactional write → async durable delivery → retry/DLQ pattern
- **Event Sourcing Lite**: Audit events as source of truth for timeline reconstruction
- **Schema Evolution**: Expand (add compatible field) → Double-write → Backfill → Switch read → Stop old write → Delete old

## 4. Safe Migration Sequence

### Compatibility Window & Validation:

**Current State Assessment**:
- All core tables follow the 4-model separation mandate
- No ORM entities directly exposed at API layer (based on code review)
- Money fields use BIGINT/DECIMAL (not FLOAT)
- States use defined mappings (not free-form VARCHAR)

**Migration Sequence** (if changes needed):

1. **Expand Phase** (Add compatible fields):
   - Add new columns with `DEFAULT NULL` or defaults
   - Add new unique constraints alongside old ones
   - Add audit fields: `version, source, trace_id`
   - **Validation Query**: `SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'xxx' AND column_name IN ('new_field') AND table_schema = 'public';`

2. **Double-Write Phase** (Business logic writes both old and new):
   - Update all write paths to populate new fields
   - Run dual-write validation: compare old vs new field values
   - **Validation Query**: `SELECT id, old_field, new_field FROM table_name WHERE old_field != new_field LIMIT 10;`

3. **Backfill Historical Phase** (Populate historical data):
   - Backfill existing records with appropriate values
   - For snapshots: `UPDATE order_item SET product_code_snapshot = product.code, product_name_snapshot = product.name WHERE product_code_snapshot IS NULL;`
   - **Validation**: Row count matches, no NULLs in previously-not-nullable fields

4. **Switch Read Phase** (Route reads to new model):
   - Update DAOs/repositories to read from new columns
   - Feature flag or gradual routing
   - **Validation Query**: Compare results between old and new read paths for sample records

5. **Stop Old Write Phase** (Cease writing to old fields):
   - Confirm all new write paths are active
   - Deprecate old code paths
   - **Validation**: Zero records with old field values being modified

6. **Delete Old Phase** (Remove old columns/constraints):
   - Only after sustained period of new path stability
   - Keep for rollback window (e.g., 90 days as per evolution spec)
   - **Validation**: All application code uses new fields exclusively

### Rollback/Roll-Forward Plan:

**Rollback**:
- Code rollback: Stop new workflow instance creation first
- Keep current definition versions readable
- Old API continues reading old business state
- Outbox events not deletable - use compatible consumer or DLQ replay
- Data migration: expand/contract pattern, never direct field destruction

**Roll-Forward**:
- If new migration fails: keep both old and new fields active
- Use feature flags to route around problem areas
- Compensating transaction: restore old fields from backup if needed
- Audit all migration attempts with `source` and `trace_id`

### Data-Integrity Checks:

```sql
-- Verify 4-model compliance (no direct ORM entity exposure)
SELECT COUNT(*) FROM information_schema.tables 
WHERE table_name IN ('order_item', 'product') 
AND table_schema = 'public';

-- Verify money fields not float
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'order_item' 
AND data_type IN ('real', 'float', 'double precision');

-- Verify state fields have constraints
SELECT DISTINCT column_name, column_default 
FROM information_schema.columns 
WHERE table_name = 'workflow' 
AND column_name LIKE '%status%';

-- Verify unique constraints exist
SELECT conname, pg_get_constraintdef(oid) 
FROM pg_constraint 
WHERE conrelid = 'order_item'::regclass 
AND contype = 'u';

-- Verify audit fields present
SELECT COUNT(*) 
FROM information_schema.columns 
WHERE table_name = 'audit_event' 
AND column_name IN ('created_at','created_by','updated_at','updated_by','deleted_at','version','source','trace_id');
```

## 5. Unknown Volume, Retention, and Recovery Assumptions

### Unknown Quantities (require measurement):

1. **Event Volume per Day**: Audit event ingestion rate - needed for partitioning strategy and outbox sizing
   - Assumption: Based on workflow volume, estimate 2-5 events per workflow instance
   - Required measurement: Actual events/day from production telemetry

2. **Retention Periods**:
   - Audit events: Regulatory requirement-dependent (typically 7 years for financial, 3 years for operational)
   - Workflow instances: Until compensation window closes (typically 90 days per evolution spec)
   - Asset references: As long as asset exists in vault, potentially indefinite
   - **Required**: Documented retention policy per data classification

3. **Data Growth Rates**:
   - Order items per day
   - Workflow instances per day (peak and average)
   - Asset references per tenant
   - **Required**: Capacity planning metrics before scaling decisions

4. **Concurrent Transaction Volume**:
   - Peak concurrent workflow executions
   - Maximum approval decisions per minute
   - **Required**: Load testing results, not assumed values

5. **Recovery Point Objective (RPO)**:
   - Maximum acceptable data loss for each store type
   - Audit events: RPO = 0 (immutable ledger)
   - Workflow state: RPO depends on compensation window
   - **Required**: Documented RPO per store, tested via disaster recovery

6. **Recovery Time Objective (RTO)**:
   - Expected restoration time for each store after failure
   - Audit ledger: Minutes (replay from events)
   - Workflow state: Hours (rebuild from definitions + state)
   - **Required**: Documented RTO, validated via drills

### Recovery Assumptions:

- **Audit-governance replay**: "只用于无副作用重建和审计，不直接恢复采购数据" - Assumes audit events are complete and accurate for reconstruction purposes
- **Outbox DLQ**: Assumes dead-lettered events can be safely reprocessed without side effects
- **Workflow recreation**: "approved 但未执行：取消或重新路由，不需要业务补偿" - Assumes approval without execution can be cleanly cancelled or rerouted
- **Compensation reversibility**: "已执行且可逆：调用领域 compensate 命令" - Assumes domain-level compensation is available for reversible operations
- **Irreversible damage**: "不可逆：创建人工恢复/冲正流程，不篡改原始审计事实" - Assumes some failures require manual intervention without modifying audit facts

### Key Unknowns Requiring Investigation:

| Unknown | Impact | Recommended Investigation |
|---|---|---|
| Audit event daily volume | Partition sizing, cost estimation | 30-day production telemetry capture |
| Compensation window duration | Failure recovery scope | Review business requirements per workflow type |
| Tenant count and scale | Isolation strategy, sharding decisions | Current tenant count × growth rate |
| Audit retention compliance | Legal/regulatory risk | Legal review per jurisdiction |
| Peak concurrent executions | Connection pool sizing, timeout config | Load test at expected peak × 2 margin |
| Cross-tenant data distribution | Sharding key selection, co-location | Analyze existing data distribution patterns |

---

**Summary**: The evidence demonstrates a well-specified but rigid persistence architecture with strong mandates (persistence-modeling.md) that, if not followed, create critical correctness and maintenance risks. The "stock binary wiring its backend" question is largely affirmatively answered - the architecture explicitly mandates proper backend wiring through the 4-model separation, expand/contract migration, and strict schema constraints. The primary risk area is not technical capability but compliance with the prescribed patterns (4-model separation, money handling, state management, audit completeness).
