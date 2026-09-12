I've thoroughly read and analyzed all the provided backend specification documents. Let me summarize my understanding and ask for direction on the specific implementation task.

## Key Principles Applied from the Documents

### 1. **Architecture & Module Boundaries** (`architecture.md`)
- **Business-capability-first module划分**: No scattered `controllers/services/repositories/models/utils` directories
- **Strict dependency direction**: `Transport → Application → Domain ← Infrastructure`
- **Domain never imports framework/ORM/SDK** - core business rules must test without Web Server or DB
- **Composition over inheritance**: Default to composition; inheritance only when stable "is-a" relationship exists, max 2-3 layers

### 2. **Design Patterns** (`design-patterns.md`)
- **Pattern selection is problem-driven**: Never use a pattern to show off
- **Rule of Three**: Abstract only after third real use case
- **Hard red lines**: No interface-per-class, no BaseService/BaseManager/BaseController, no speculative extension points
- **Each pattern invocation must state**: problem / change point / why simple code insufficient / added complexity / how to test

### 3. **Domain Modeling** (`domain-modeling.md`)
- **Domain objects protect their own state**: `order.pay(payment)` never `order.status = "paid"`
- **State machines centralized**: When >3 states or transitions have permission/condition/side-effect
- **Policy centralization**: Button/action visibility = business state × permission × data condition × system state
- **Aggregation boundaries**: One use case = one transaction boundary (Application Use Case level)

### 4. **Persistence Modeling** (`persistence-modeling.md`) - **MANDATORY GATE**
Before any table/ORM entity, must complete Persistence Design Report covering:
- **Four models separation**: API DTO / Domain Model / Persistence Model / Read Model (never merge)
- **Identity decisions**: Internal PK + Business key + Idempotency key (distinct concepts)
- **Money**: `amount_minor BIGINT` + `currency_code CHAR(3)` — **NO FLOATS**
- **Time**: All timestamp fields must specify UTC/local, precision, source, nullable
- **Status**: Defined state set + mapping, never free strings, publish-time non-reuse
- **NULL semantics**: Must distinguish unknown/not-exists/unset/not-applicable/cleared
- **Indexes**: Drive by query patterns, never mechanical addition
- **Uniqueness**: `tenant_id+order_no`, `tenant_id+idempotency_key` etc. are business rules
- **Migration**: Expand→Migrate→Contract sequence, never drop+change simultaneously

### 5. **Production Readiness** (`production-readiness.md`)
- **11-item gate**: Contracts, error model, idempotency, capacity, observability, verification
- **Error model**: `{code, message, trace_id, details}` with categorization
- **Reliability**: Timeouts, retries, circuit breakers, degradation for every critical dependency
- **Capacity**: All pools bounded (DB/HTTP/Worker), no unlimited resources
- **Multi-tenant**: `tenant_id` must enter main query, unique indexes, cache keys, events, audit
- **Security**: Input untrusted, sensitive data classification, keys never in code/Git
- **Observability**: Logs (what happened), metrics (how many), trace (through which services)
- **Testing**: 5 layers + property-based + fuzz + race + chaos; performance needs baseline

### 6. **Agent Guardrails** (`agent-guardrails.md`)
- **No hallucinations**: Must search existing code, confirm versions, reuse patterns, run actual commands
- **Change scope control**: Direct/indirect file modifications must be documented
- **DRY sacrifice**: Business repetition > wrong abstraction; abstraction needs ≥2 cross-business users + stable semantics
- **Performance discipline**: Measure first, optimize second; no data-free "optimizations"
- **Completion definition**: Must run verification commands, no fabricating results

### 7. **Evolution** (`evolution.md`)
- **Refactor triggers**: File >400 lines, ≥4 unrelated areas for simple change, ≥10 dependencies, rule duplication
- **ADR requirement**: Important decisions must be in `docs/adr/NNNN-title.md`
- **Technical debt**: Every TODO needs cause/Owner/date/follow-up condition
- **Progressive migration**: Feature Flag / Dual write / Dual read / Gradual cutover

### 8. **Experience-Driven Engineering** (your mandatory principles)
1. **Endpoint from scenario card**: User operation → page response → latency → data source; search gets contextual aggregation
2. **Lifecycle > CRUD**: Status machines centralized, history append-only (never overwrite), event-driven cross-service
3. **Data model from page needs**: E.g., health needs safety stock/daily consumption/supply cycle; derive strategy (compute/cached/ materialized)
4. **Experience budget → architecture**: 500ms detail → Read Model; dashboard → aggregate; long task → async (202 + task id + progress + idempotent retry)
5. **3-year horizon**: Tenant/permission/audit/idempotency boundaries correct first; abstract conservatively (rule of three)

## What I Need From You

Since you've established the role as "senior backend engineer with 10+ years production experience," I'm ready to apply these principles. However, I need to know:

**What specific backend engineering task or project do you want me to work on?**

Examples of what I can help with:
- Designing a new module from scratch following these specs
- Refactoring an existing module applying the evolution patterns
- Designing a persistence model for a specific entity
- Implementing a state machine for an order/approval workflow
- Setting up production-ready error handling and idempotency
- Designing an API contract with proper versioning and compatibility
- architecting a multi-tenant system with proper data isolation
- Pattern selection for a identified problem
- Code review against these specifications

Please provide the specific prompt, project description, or task you want me to tackle, and I'll apply all the relevant principles from the documents I've read.
