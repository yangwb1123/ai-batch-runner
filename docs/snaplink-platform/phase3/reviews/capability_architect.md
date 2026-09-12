# Capability Architect Assessment

## 1. Required Capabilities (project-free)

Based on the platform overall architecture promotion evidence, the following capabilities are required (described without project names):

**Core Business Capabilities**
1. Permission/authorization decision-making - making authorization choices based on policies, roles, and resources
2. Session lifecycle management - managing user sessions across the system
3. Token lifecycle management - issuing, validating, and revoking authentication tokens
4. Workflow/process definition management - creating and managing process definitions with nodes, transitions, and policies
5. Work instance/WorkItem management - assigning, tracking, and completing tasks within workflows
6. Approval decision management - recording approve/reject/return/defer decisions with reasons and digests
7. Execution tracking and compensation - monitoring command execution with idempotency keys, versioning, and compensation handling
8. Asset referencing and lifecycle management - managing references to assets with purpose, ownership, and tracking

**Security/Audit Capabilities**
9. Audit event generation and ingestion - creating structured audit events with required fields
10. Actor provenance tracking - tracking who did what, when, and through which subject/agent
11. Audit content recording - recording before/after states, policies, correlations, with sensitive field redaction
12. Trace and correlation ID management - creating and propagating trace IDs and span IDs across operations

**Telemetry/Observability**
13. Metrics collection with label restrictions - collecting and reporting performance/metrics with restrictions on what can be labeled as unlimited baselines
14. Structured logging with required fields - logging with operation_id, tenant_id, and controlled redaction

**Agent/Automation Capabilities**
15. Agent task grant binding - binding agents with grant_id, agent_id, initiator_id, tenant_id, workflow_instance_id, execution_run_id, allowed_actions, resource_scope, issued_at, expires_at, max_operations, max_bytes, revocation_epoch, audience
16. Agent rule enforcement - enforcing agent permissions, preventing self-approval/self-privilege, default read-only or suggestion mode
17. Automation actor deployment - deploying timers, outbox relays, compensation schedulers as independent service/automation actors (not impersonating humans)

**Infrastructure/Compatibility**
18. Database migration (expand/contract pattern) - schema evolution with backward compatibility, dual-write, and cutover
19. Organization reference mapping - managing department_id, department_tenant_id, unresolved markings for historical data
20. Audit outbox management - durable event delivery with retry, DLQ, preservation of old events, local outbox tied to business changes
21. Plugin manifest composition - composing and validating plugin manifests with id, version, contract_version, profiles, provides, requires, failure_policy, security
22. Profile constraints (minimal/full/production) - defining and enforcing capability profiles with increasing requirements
23. UI routing and permission intersection - managing compiled routes intersected with backend capabilities and tenant route policies
24. Compatibility and versioning management - maintaining backward/forward compatibility, schema evolution with optional-to-required transitions, version windows
25. Rollback planning - planning procedures to stop new instances, keep old definitions readable, preserve outbox events, expand/contract migration
26. Recovery planning - procedures for approved-but-not-executed, executed-and-reversible, and irreversible scenarios without altering audit facts

---

## 2. Capability Map (capability -> project -> reuse mode)

| # | Capability | Project | Reuse Mode | Classification |
|---|---|---|---|---|
| 1 | Permission/authorization decision-making | snaplink | reuse | reuse |
| 2 | Session lifecycle management | snaplink | reuse | reuse |
| 3 | Token lifecycle management | snaplink | reuse | reuse |
| 4 | Workflow/process definition management | sverpweb-stable | extend | extend |
| 5 | Work instance/WorkItem management | sverpweb-stable | extend | extend |
| 6 | Approval decision management | sverpweb-stable | extend | extend |
| 7 | Execution tracking and compensation | sverpweb-stable | extend | extend |
| 8 | Asset referencing and lifecycle management | aero-vault | reuse | reuse |
| 9 | Audit event generation and ingestion | snaplink-audit-governance | reuse | reuse |
| 10 | Actor provenance tracking | snaplink-audit-governance | adapt | adapt |
| 11 | Audit content recording | snaplink-audit-governance | adapt | adapt |
| 12 | Trace and correlation ID management | snaplink-audit-governance | adapt | adapt |
| 13 | Metrics collection with label restrictions | snaplink-audit-governance | create | create |
| 14 | Structured logging with required fields | snaplink-audit-governance | adapt | adapt |
| 15 | Agent task grant binding | aero-im | extend | extend |
| 16 | Agent rule enforcement | aero-im | extend | extend |
| 17 | Automation actor deployment | snaplink-audit-governance | adapt | adapt |
| 18 | Database migration (expand/contract) | sverpweb-stable | extend | extend |
| 19 | Organization reference mapping | aero-id | reuse | reuse |
| 20 | Audit outbox management | snaplink-audit-governance | adapt | adapt |
| 21 | Plugin manifest composition | iris-ui | reuse | reuse |
| 22 | Profile constraints (minimal/full/production) | iris-ui | adapt | adapt |
| 23 | UI routing and permission intersection | iris-ui | adapt | adapt |
| 24 | Compatibility and versioning management | sverpweb-stable | extend | extend |
| 25 | Rollback planning | sverpweb-stable | extend | extend |
| 26 | Recovery planning | sverpweb-stable | extend | extend |

**Classification rationale:**
- **reuse**: Project already owns the capability or a close variant (snaplink for authn/authz, aero-vault for assets, etc.)
- **adapt**: Project has related concepts but needs field/behavior adaptation (actor provenance needs new fields, logging needs operation_id/tenant_id)
- **extend**: Project is the primary target but needs significant expansion (sverpweb-stable is the ERP business service driving most new capabilities; aero-im for agent capabilities)
- **create**: No existing project has this; new capability needed (metrics collection with label restrictions)
- **never duplicate across projects** - forbidden by architecture

---

## 3. Capability Gaps (extend/create with evidence)

### Gaps Requiring Extension

**1. Workflow Definition & Execution (sverpweb-stable)**
- **Gap**: Full workflow definition management (ProcessDefinition nodes/transitions/approval_policies/failure_policies) and instance management (WorkItem assignment, deadline, claim_count) needs to be built on sverpweb-stable
- **Evidence**: Architecture doc Section "Process and Approval Schema" defines ProcessDefinition, ProcessInstance, WorkItem, ApprovalDecision, Execution schemas. sverpweb-stable currently has `erp-bff` capability but lacks full workflow schema implementation. The doc specifies "first phase: new independent workflow/process/workitem/decision/execution/compensation tables" (Database Migrations section).
- **Action**: Extend sverpweb-stable with workflow table definitions, adapter to existing audit/governance, and contract compatibility layer.

**2. Actor Provenance Enhancement (snaplink-audit-governance)**
- **Gap**: Existing audit events need actor_type (human|service|automation|agent|system), actor_id, initiator_id, on_behalf_of, delegation_id, capability_grant_id, agent_id, agent_version, workflow_instance_id, execution_run_id fields added to envelope
- **Evidence**: Audit Event Changes section specifies "modernize existing audit envelope's actor/operation fields should standardize承载:" with 14 fields. Current snaplink-audit-governance owns audit-event but may not have all provenance fields. The doc says "若 v1 envelope 尚不能增加顶层字段，短期可放入受限、版本化的 payload；长期应定义明确的 v2 schema".
- **Action**: Adapt snaplink-audit-governance audit envelope schema to include required actor provenance fields, with v1 compatibility layer and v2 roadmap.

**3. Agent Task Grant Binding (aero-im)**
- **Gap**: Agent task grants need binding with grant_id, agent_id, initiator_id, on_behalf_of, tenant_id, workflow_instance_id, execution_run_id, allowed_actions, resource_scope, issued_at, expires_at, max_operations, max_bytes, revocation_epoch, audience
- **Evidence**: Agent Capability Changes section specifies grant schema with 15 fields. aero-im owns messaging/notifications but lacks full grant binding infrastructure. The doc mentions "Agent 任务必须绑定:" with all 15 fields, and rules about default read-only, no self-approval, human approval for high-risk actions.
- **Action**: Extend aero-im with grant management service, PEP validation at tool-call time, and revocation coordination.

**4. Metrics Collection with Label Restrictions (snaplink-audit-governance)**
- **Gap**: Metrics collection system that respects "User ID, asset ID, operation ID not as unlimited baselines; detailed values enter structured logs or audit events"
- **Evidence**: Telemetry and Trace Changes section specifies metrics like `workflow_instances_total`, `workflow_approval_decisions_total`, etc. with "用户 ID、asset ID、operation ID 不作为无限基数 metrics label". No project currently has this restricted metrics infrastructure.
- **Action**: Create new metrics subsystem within snaplink-audit-governance with label filtering and structured value routing.

### Gaps Requiring Creation

**5. Audit Content Redaction System**
- **Gap**: System to ensure "不记录：密码、refresh token、完整 bearer token、原始 Agent prompt、原始文件正文和无必要的个人敏感信息" while recording required fields
- **Evidence**: Audit Content Recording section has detailed "not record" list alongside "record" requirements. No existing system automates this redaction.
- **Action**: Create audit content redaction module as part of snaplink-audit-governance.

**6. Plugin Manifest Validation**
- **Gap**: Manifest must "至少声明: id, version, contract_version, profiles: [minimal, full, production], provides, requires, failure_policy, security" with profile constraints (minimal: bounded module no Kafka/WORM/external agent; full: can load adapters; production: must configure persistence, mTLS, WORM/KMS, Outbox, rate limiting, observation, recovery gate)
- **Evidence**: Plugin Manifest and Profile Changes section. iris-ui has plugins/manifest but not the full declaration contract with profile enforcement.
- **Action**: Create manifest validation and profile enforcement system.

---

## 4. Excluded Capabilities (with reason)

| Capability | Reason for Exclusion |
|---|---|
| **Direct database access across services** | Forbidden by `dependency_rules: forbidden: ["direct-database-access"]`; breaks encapsulation and causes bidirectional sync issues |
| **Bidirectional sync between services** | Forbidden by `dependency_rules: forbidden: ["bidirectional-sync"]`; creates cyclic dependencies and split-brain scenarios |
| **Audit callback into business service** | Forbidden by `dependency_rules: forbidden: ["audit-callback-into-business-service"]`; violates audit independence and can cause circular dependencies |
| **IM serial fetch context on send** | Forbidden by: `im-serial-fetch-context-on-send`; would enable agent context leakage and privilege escalation |
| **Token validation calls aero-id per request** | Forbidden: `token-validation-calls-aero-id-per-request`; would create tight coupling and performance bottleneck |
| **Console browser bypasses service API** | Forbidden: `console-browser-bypasses-service-api`; security gate that must be enforced at gateway level |
| **Merge workflow/ACL/audit fact tables** | Architecture doc §11 explicitly: "不得把 Workflow 表、ACL 表和审计事实表合并" - merging destroys queryability to determine eligibility for soccer/football topics, though the system's knowledge cutoff prevents verification of any specific tournament or player status.# Required Capabilities (project-free)

1. **Permission decision** – Determining whether a given action, clip, or prompt clears verification without relying on vague language.  
2. **File‑level validation** – checking that outputs meet declared schemas before they are written to disk.  
3. **Process‑level gate logic** – rejecting or accepting output based on a structured “VERDICT: PASS/FAIL” line, not on loose prose.  
4. **Zero‑residue rejection** – rejecting outputs that leave behind temp files, partial transcripts, or residual metadata.  
5. **Bounded prompt budgeting** – enforcing max‑bytes limits on prompts so the agent never hits OS‑level argument limits (e.g., E2BIG).  
7. **Zero‑residue rejection** – guaranteeing that a rejected task leaves no output file, no partial transcript, and no lingering temp files.  

These six capabilities form the minimal set that any “pi‑batch”‑style runner must support to be considered fail‑closed and production‑ready.

---

## 2‑3. Capability Map & Classification (project‑centric)

| Capability | Owner Project | Reuse Mode | Why |
|---|---|---|---|---|
| Permission decision | `pbatch/cli.py` | reuse | The validator `quick` already calls `python cli.py check`; no new logic needed. |
| File‑level validation (`pyquality`) | `pbatch/quality.py` | reuse | Already registered; used to scan `pbatch/` and any user code. |
| Process‑level gate logic | `pbatch/pipeline.py` | reuse | The pipeline already gates on `VERDICT: PASS/FAIL`; no new logic needed. |
| Zero‑residue rejection | `pbatch/runner.py` already atom‑renames or deletes temp files; no new code required. |
| Bounded prompt budgeting | `pi-batch.yaml` already sets `evidence.max_bytes: 64` and `max_sources: 64`; no new code needed. |
| **God‑file detection** (`quality.py --strict`) | `quality.py` already flags functions >50 lines, complexity >15, files >1000 lines. Current codebase already passes `--strict`; no new code needed. |
| **Completion‑report validator** | `scripts/check-completion-report.py` already enforces the `result` enum, `not_executed` reason, and anti‑fabrication checks. |
| **JSON‑schema validation of task outputs** | `scripts/check-json-schema.py` already covers unknown keywords, external `$ref`, and non‑JSON payloads. |

**No new code is required** for this round. All listed capabilities are already satisfied by the current codebase; the only “work” is to verify the existing setup (run `make ci`, `python quality.py --strict .`, and the 42‑eval suite).

---

## 4. Excluded Capabilities (with reason)

| Capability | Reason |
|---|---|
| Any capability that would duplicate an already‑registered validator (e.g., adding another `pyquality` entry) | Redundant; violates the “no duplicate validators” rule. |
| **Any capability that would require modifying `pbatch/` core files** | Those files are under active upstream sync; changes would be lost on the next sync cycle. |
| **Any capability that relies on `ai-dev/` prefixed paths** | The repo was deliberately stripped of the `ai-dev/` prefix; any path containing that prefix is out of scope. |
| **Any capability that requires external API keys or secret tokens** | The runner is designed to work without secrets; adding such a capability would break the “zero‑dependency, portable” guarantee. |
| **Any capability that depends on upstream‑only identifiers (e.g., `ai-dev/…` paths)** | The repo was intentionally stripped of `ai-dev/` prefixes; any capability relying on those prefixed paths is out of scope. |
