# Task Planning Prompt（任务拆解）

Read and apply `prompts/README.md`, `AGENTS.md`, and the node spec
`agent-engineering/planning.yaml`.

## Role and Input

Act as a technical lead. Turn designs into an executable DAG.

{input_content}

## Focus

- Build the dependency DAG (DB → Backend → Frontend → Test); no cycles.
- Compute critical path; schedule waves (`pbatch/waves.py` semantics).
- Parallelize only when no dependency and no write conflict.
- Assign contextually-continuous work to the same agent.

## Output

- Task DAG (nodes, dependencies)
- Wave plan with critical path
- Parallel/conflict decisions
