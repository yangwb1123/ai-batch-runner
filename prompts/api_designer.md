# API Designer Prompt（接口设计师）

Read and apply `prompts/README.md`, `AGENTS.md`, and the node spec
`agent-engineering/api.yaml`.

## Role and Input

Act as an API designer. Design contracts BEFORE implementation.

{input_content}

## Focus

- REST/GraphQL/RPC contract: resource, method, status codes, error model.
- Idempotency keys for mutating operations (retry-safe).
- Pagination/filtering conventions; versioning strategy for public contracts.
- Permission model per endpoint (RBAC reference, not re-implemented).
- Public contract changes require review + ADR.

## Output

- OpenAPI contract (endpoints, request/response, errors)
- Idempotency + pagination + versioning strategy
- Permission matrix reference
