# 多 Agent 接力端到端演示（2026-08-22 实证）

> 用户需求闭环证明："安排几个不同的 agent 来完成任务，要可以共享结果"。

## 演示过程（全部真实执行，非模拟）

```bash
# ① pi 生产数据（8.0s，账本自动记录产物路径）
pi-batch -p 'Create /tmp/handoff-data.json containing exactly:
  {"product": "ai-batch-runner", "version": "0.3.0",
   "supported_agents": ["pi","codex","claude","gemini","opencode"],
   "secret_color": "crimson"}' \
  --agent-bin pi -o /tmp/handoff-producer.md

# ② codex 接力消费（19.9s；产物有界摘录自动注入 prompt）
pi-batch handoff --from-agent pi --agent codex \
  "From the handed-off artifact, answer: what is the secret_color and
   how many supported agents are listed? Reply EXACTLY in the format:
   COLOR=... AGENTS=N"
```

## 结果

```
pi 产物（字节级校验）:
  {"product": "ai-batch-runner", "version": "0.3.0",
   "supported_agents": ["pi","codex","claude","gemini","opencode"],
   "secret_color": "crimson"}

codex 接力回答:
  COLOR=crimson AGENTS=5        ← 完全正确
  （codex 实际执行 sed 阅读了注入的产物摘录：事件流可见 command_execution）

handoff succeeded; target run recorded in the agent ledger.
```

## 闭环链路

```
pi run ──账本(agent_ledger.outputs)──► handoff ──有界摘录(evidence.combine_outputs)──► codex run ──► 账本
  生产                                   事实引用（禁止编造）                           消费
```

- 账本：`data_dir/agents/ledger.jsonl`（`pi-batch agents sessions` 可查双方记录）
- 摘录纪律：64KiB 公平预算、截断标记、source manifest 路径清单
- 接力结果自动回写账本 → 第三个 agent 可继续接力（链式）