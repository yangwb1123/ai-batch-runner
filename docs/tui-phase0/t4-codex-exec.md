# T4 — Codex Structured Exec Adapter

## 目标

把 Codex 非交互 JSONL 生命周期转换为 provider-neutral `RuntimeEvent`，为 TUI 提供
结构化 message、command、file、plan、usage 和 Turn 状态。

## 已交付

- `pbatch/tui_adapter_codex.py` 实现 `CodexExecAdapter`、有界事件队列、会话/Turn
  状态和 provider JSONL 分派；
- `pbatch/tui_codex_support.py` 提供 argv 定位、进程组生命周期、有界字段/标识处理和
  Runtime capability 构造；
- Adapter 使用 argv 直接启动 `codex exec --json`，stdin 关闭，stdout/stderr 由独立
  worker 持续排空；
- provider thread、Turn、agent message、command execution、file change、todo list
  和 usage 均映射到统一事件族；
- `tests/test_tui_adapter_codex.py` 固定映射、容量、终态、中断与会话行为。

## 关键契约

- pi-batch 逻辑 `session_id` 与 provider `thread_id` 分离；provider id 只作为会话映射
  和后续 resume/fork 的依据；
- 每个 run 的事件 `seq` 严格递增，终态只提交一次；provider 进程未给出合法完成事件
  时显式形成 FAILED 或 UNKNOWN，而不是推断成功；
- 非 JSON 行形成有界 `runtime.warning`；未知 provider item 形成
  `runtime.unknown_item`，后续合法事件继续处理；
- Assistant 正文只映射为 message，不能生成 approval、gate、review 或 command 控制
  事件；
- 文本、命令输出、路径、计划和原始未知项均按 UTF-8 字节限制；超长 identity 使用
  带摘要的稳定命名，避免相同前缀碰撞；
- 队列压力只允许淘汰最旧的 runtime warning，并产生一次显式 overflow 诊断；正文和
  终态事件不得静默丢失；
- 当前真实能力包括 structured events、message stream、usage、interrupt、session
  resume 与 session fork；steer、message queue、typed approval、review、subagent、
  image 和 instruction source 不被宣称支持；
- interrupt 回收该 Adapter 的进程组并提交不可回退的 INTERRUPTED 终态，`close()`
  幂等回收进程和 worker。

## 验证入口

- `tests/test_tui_adapter_codex.py`：JSONL 映射、未知项、限界、背压、中断、resume/fork；
- `tests/test_tui_protocol.py`：Adapter 产出事件的 schema 与状态机；
- `tests/test_tui_integration_codex.py`：安装 Codex 时的真实双 Turn/TUI 集成；
- 仓库级验证入口：`make ci`。

## 结果/当前边界

Codex Exec 已作为结构化、可中断且可延续会话的 Runtime 接入统一事件模型。该接口是
非交互执行桥接，不提供 App Server 的双向审批、steer、实时 request/decision 或
富客户端控制面；这些能力不能从 stdout 内容推导。
