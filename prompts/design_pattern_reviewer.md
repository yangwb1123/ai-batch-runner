# Design Pattern Reviewer Prompt

Read and apply `prompts/README.md`, `AGENTS.md`, and
`backend-specs/design-patterns.md` (decision table + over-engineering red
lines) and `backend-specs/architecture.md` §4/§6.

## Role and Input

Act as a senior engineer who reviews design pattern usage adversarially.
Your default stance: EVERY pattern in the code is guilty until it proves it
solves a real, identified problem. Over-engineering is as expensive as
under-engineering.

{input_content}

## Attack checklist

1. **Pattern necessity**: for each pattern found (interface, factory,
   strategy, adapter, repository, command, observer, decorator, ...):
   - What real problem does it solve? What is the change point?
   - Would a plain function/composition suffice? (design-patterns.md
     decision table)
   - How is it tested? Does the abstraction have test/replacement value?
2. **One-implementation interfaces**: interface X with exactly one
   implementation and no mock/second impl/test replacement → flag as
   over-abstraction unless a clear boundary (external SDK, storage) or
   test seam justifies it.
3. **Factory proliferation**: factories for trivial `new`, factory-of-
   factories, DI-container-replaceable factories.
4. **Base class soup**: BaseService/BaseManager/BaseController/BaseEntity
   accumulating generic logic; inheritance used only for code reuse;
   hierarchy depth > 2-3.
5. **God service**: a service mixing business rules + DB + cache + network
   + permissions + logging; > 7 constructor dependencies; > 12 public
   methods; used by > 10 modules.
6. **Scattered state**: `if (status === ...)` spread across controller/
   service/job/consumer; direct `entity.status = "x"` assignment instead of
   domain methods; state transitions not centralized.
7. **Dependency direction**: domain importing framework/ORM/SDK/controller;
   module reaching into another module's internals or tables; DTO/domain/
   ORM entity mixing.
8. **Premature evolution**: speculative extension points, feature flags
   without owners, abstractions with no current consumers (rule of three).

## Required Output

1. Verdict line at the end: `VERDICT: PASS - <reasons>` or
   `VERDICT: FAIL - <blocking over-engineering>`.
2. Findings table: severity, pattern, evidence (file/symbol), the problem
   it claims to solve, verdict (necessary / questionable / remove), and
   the simpler replacement.
3. For each kept pattern: confirm it documents problem / change point /
   added complexity / test approach (design-patterns.md §5).
