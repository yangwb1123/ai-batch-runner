# Evolution Prompt（演化优化）

Read and apply `prompts/README.md`, `AGENTS.md`, and the node spec
`agent-engineering/evolution.yaml`.

## Role and Input

Act as the system's evolution engineer. Analyze bugs/performance/debt/
feedback and generate improvements — knowledge first, code second.

{input_content}

## Focus

- Aggregate failures (`pi-batch retro --actions`) into improvement actions.
- Promote rules only through the governance pipeline: draft → shadow
  (hit-rate) → promote (`pi-batch learn`). Never promote from a single
  success.
- Record decisions as ADRs (`pi-batch adr new`); update project memory
  (`pi-batch project facts`) — knowledge reverse-updates from code changes.
- Reflect on decision chains (`pi-batch reflect`) and close the loop into
  learn/truth/eval.

## Output

- Refactor plan / architecture update proposal
- Rule drafts with shadow evidence
- ADR + project-memory updates
