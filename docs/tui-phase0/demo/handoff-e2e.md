# 多 Agent 产物接力示例

该示例展示 Agent A 产出文件、共享账本记录路径、Agent B 通过 handoff 使用有界证据的完整路径。

```bash
pi-batch -p '生成一份产品能力摘要' \
  --agent-bin pi -o docs/demo/producer.md

pi-batch handoff --from-agent pi --agent codex \
  --output docs/demo/consumer.md \
  "核对上游摘要，并列出你实际采用的事实"
```

## 正向流程

```text
Agent A run
  → ledger 记录 succeeded + outputs
  → handoff 选择唯一来源
  → source manifest + 有界摘录
  → Agent B run
  → ledger 记录新结果
```

## 验收点

- `pi-batch agents sessions` 能看到两次 run 的 agent、session、状态与产物路径。
- Agent B 的 prompt 包含来源身份、路径 manifest、截断标记和公平有界摘录。
- Agent B 的报告引用实际使用的上游产物；coordinator 只读取并摘录源文件。
- 不存在成功来源、run 前缀不唯一或全部产物不可读时，在启动 Agent B 前拒绝。
- 目标执行继续遵守 workspace lock、timeout、validator 和 prompt 大小门禁；若账本
  指向 cwd 外绝对路径，调用方负责确认数据边界及目标 Agent 的写权限。

该文档是可复现操作示例；具体运行证据以当前工作区的账本记录和产物摘要为准。
