# T1 — Runtime 协议地基

## 目标

建立 provider-neutral 的版本化事件、能力声明和 Turn 状态机，使 TUI、Adapter 与
自动化执行共享同一组可机械校验的控制语义。

## 已交付

- `pbatch/tui_protocol.py` 定义 `RuntimeEvent`、`RuntimeCapabilities`、`TurnState`
  与 `EventSequenceTracker`；
- `RuntimeEvent` 支持有界 JSONL 编解码，并保留未知顶层字段供兼容演进；
- 固定事件类型覆盖 session、turn、message、plan、command、tool、file、validation、
  gate、approval、actor、review、artifact、usage 与 runtime 诊断；
- `runtime.unknown_item` 为 Adapter 保存未知 provider item 的显式事件类型；
- `tests/test_tui_protocol.py` 固定协议 schema、能力默认值、状态迁移与大小边界。

## 关键契约

- 事件 envelope 绑定 workspace、session、run、turn 和 actor；`event_id` 必须使用
  `evt_` 命名域，`seq` 是非负整数；
- 未知事件类型、坏 JSON、非对象、多物理行、NaN/Infinity 和不支持的协议版本均
  fail closed；未知顶层字段原位 round-trip，不混入 `extensions`；
- payload、extensions 和整条 JSONL 分别受 64 KiB、16 KiB 和 128 KiB 的 UTF-8
  字节上限约束；
- Legacy runtime 只默认声明 `message_stream` 与 `interrupt`；富能力默认关闭，未知
  capability 按不支持处理；
- Turn 只能沿 `TURN_TRANSITIONS` 迁移，SUCCEEDED、FAILED、INTERRUPTED 与 UNKNOWN
  为不可回退终态；
- 同一 `run_id` 内新事件的 `seq` 必须严格递增；重复 `event_id` 幂等跳过，并先于
  顺序检查处理。

## 验证入口

- `tests/test_tui_protocol.py`：schema round-trip、事件枚举、能力与状态机；
- `tests/test_tui_adapter_codex.py`、`tests/test_tui_adapter_appserver.py`：下游 Adapter
  对事件枚举和协议版本的消费契约；
- 仓库级验证入口：`make ci`。

## 结果/当前边界

协议层只包含纯数据和确定性校验，不执行 IO、不启动 provider，也不解释 Assistant
正文。未知 provider item 只能由 Adapter 显式映射为 `runtime.unknown_item`；它不会
绕过固定事件白名单生成控制事件。
