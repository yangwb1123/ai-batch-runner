# 阶段级 Agent 流水线示例

一个 pipeline 可以为每个阶段选择独立执行器，并通过有界证据链把上游产物交给下游。

```yaml
stages:
  - name: analyze
    agent: pi
    from_prompt: "给出离线同步冲突消解的设计要点"
    output: docs/architecture.md

  - name: implement
    agent: codex
    from_outputs: analyze
    tasks:
      - prompt: "依据 {input_content} 实现并提供验证证据"
        output: docs/implementation.md

  - name: gate
    agent: codex
    from_outputs: implement
    gate: true
    tasks:
      - prompt: "按证据裁决，并输出 VERDICT: PASS - <reason> 或 VERDICT: FAIL - <reason>"
        output: docs/gate.md
```

## 正向流程

```text
analyze
  → 产出 architecture
  → from_outputs 有界注入 implement
  → 产出 implementation + verification evidence
  → 独立 gate 裁决
```

- `agent` 只选择该阶段的执行器，不改变 pipeline 的锁、超时、验证和失败签名合同。
- `from_outputs` 使用来源清单与公平字节预算，不复制无界上下文。
- Codex `--json` 事件流属于运行证据；阶段产物只保存最终 assistant 报告。
- gate 只接受带理由的结构化 verdict；FAIL 会阻断后续阶段。
- `--reuse` 会用当前 validator 复验已有产物，通过后才复用。
