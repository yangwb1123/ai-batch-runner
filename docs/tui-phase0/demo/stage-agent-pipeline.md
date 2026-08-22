# 阶段级 Agent 流水线演示（T13，2026-08-22 实证）

> 一个 pipeline 内编排多个真实 agent：`agent:` 是每阶段的执行器选择（per-task，并行安全）。

## 演示（全部真实执行）

```yaml
stages:
  - name: analyze      # ① pi 产品架构分析
    agent: pi
    from_prompt: "离线优先同步层的冲突消解策略，5 条要点"
    output: docs/architecture.md
  - name: implement    # ② codex 消费上游产物继续实现清单
    agent: codex
    from_outputs: analyze        # 证据有界注入：<evidence source="architecture">
    tasks: [{prompt: "…{input_content}… End with: IMPL_DONE",
             output: docs/implementation.md}]
  - name: gate         # ③ codex 独立裁决（对抗视角）
    agent: codex
    from_outputs: implement
    tasks: [{prompt: "若连贯完整回复 VERDICT: PASS，否则 VERDICT: FAIL …",
             output: docs/gate.md}]
```

## 结果

```
analyze   (pi)    7.8s  PASS → 架构要点（LWW 基线/CRDT/重试幂等…）
implement (codex) 15.3s PASS → 9 条实现清单 + IMPL_DONE（产物为结构化 JSONL 转录）
gate      (codex) 14.3s PASS → VERDICT: FAIL - only a high-level design checklist
                              is provided; no implementation or verification
                              evidence is included.
```

**亮点**：
- `from_outputs` 证据链：codex 的 prompt 里真实出现
  `Upstream analysis: <evidence source="architecture">…`
- gate 独立裁决 **诚实 FAIL** 并阻断后续（fiend closed）——尽管每阶段 PASS，
  gate 认为"只有设计清单、无实现/验证证据"——AI 产出是提案的纪律原样生效
- `--reuse` 断点续跑：analyze 阶段复用上轮产物（REUSED+VALIDATED）

## 特性组合（本轮用到的全部为项目原生能力）
阶段级 agent（T13）· from_outputs 有界证据注入（evidence）· gate 裁决
（VERDICT fail closed）· --reuse 标志校验复用 · 多实例锁（--no-lock 场景）
· 账本互见（每 run 记录各自的 agent）