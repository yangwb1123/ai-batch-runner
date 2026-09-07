# T8：Codex App Server 协议层

## 目标

为 Codex App Server 建立 provider-neutral 的双向运行时适配层：以结构化 RPC 表达
session、turn、steer、interrupt 和审批生命周期，并在服务不可用、断线或能力未声明
时 fail closed。

## 已交付

- `pbatch/tui_adapter_appserver.py` 实现 App Server JSON-RPC adapter，包括 initialize、
  thread start/read/resume/fork/list、turn start/steer/interrupt 和事件流。
- `pbatch/tui_appserver_support.py` 提供可替换 Transport、默认子进程 transport、有界
  EventQueue、item projector、session summary 和审批响应转换。
- 默认 transport 按当前官方协议直接启动 `codex app-server` 的 stdio JSONL 服务；
  只有显式传入 daemon socket 时才使用 `codex app-server proxy --sock ...`。
- 初始化严格执行 `initialize` request → `initialized` notification 两阶段握手，并以
  当前 `ai-batch-runner` 版本标识 client；不再发送当前 schema 未声明的
  `protocolVersion` 参数。
- initialize 前所有富能力均关闭；握手成功后，基础双向能力和 server 明确声明的扩展
  能力才会开启。
- provider notification 被映射为统一 `RuntimeEvent`；未知通知降级为有界
  `runtime.unknown_item`，不会把正文或未知 payload 提升为控制事件。
- exec command、file change 和 permissions 审批使用 typed request/decision；request id
  保留类型边界，重复、未知或类型不匹配的决策被拒绝。
- 审批具有 TTL；过期或连接丢失会产生失效事件，`close()` 清空未决请求并关闭传输；
  三种路径都不会发送迟到授权。

## 关键契约与数据流

```text
Codex App Server
    ⇅ JSON-RPC / Transport
CodexAppServerAdapter
    ├─ request: initialize, thread/*, turn/*, approval decision
    └─ event: session.*, turn.*, command.*, file.changed,
              approval.*, usage.updated, runtime.*
                         │
                         ▼
                  AgentRuntime / EventBus
```

- 每个本地 session 同时保存 pi-batch `session_id` 与 provider thread id；resume/fork
  请求使用 provider id，fork 创建新的本地 session identity。
- turn steer/interrupt 必须绑定当前活动 handle；活动 turn 未结束时不能替换 session。
- thread resume 先用不加载、不订阅的 `thread/read` 对账；只接受
  `notLoaded`/`idle`，其他或不可验证状态保持本地历史只读。
- `thread/list` 返回 typed `SessionPage`，固定 adapter cwd、`updated_at desc` 与顶层
  sourceKinds，严格校验字段并保留 `nextCursor`；T28 只把 exact provider handle 匹配项
  作为 picker advisory，不能替代上述 `thread/read`。
- 同一 turn 只接受一个 terminal event；provider `turn/completed` 的内部 status 会按
  `completed` / `failed` / `interrupted` 分别投影为成功、失败和中断，缺失或未知 status
  以 `UNKNOWN` 失败关闭。先到达的 `error` notification 只记诊断，不提前冒充终态。
- transport EOF 或协议不可用会结束事件流、撤销能力并使未决审批过期。
- payload、未知 item、文本和队列均有界；协议错误不会退化成非结构化 stdin 确认。
- `review/start` 已按当前 App Server `ReviewStartParams` 接通 inline review：严格绑定
  active provider thread，按 uncommitted/base/commit/custom 转换 typed target，并校验
  `reviewThreadId` 与 provider turn id；detached review 仍 fail closed。App Server 的
  `thread/archive` / `thread/unarchive` 也已接通，但仅接受 exact provider thread id、
  active workspace 与 `session_archive` capability；本地 `/archive` 仍只是 journal
  标记，远端副作用必须显式使用 `/provider-archive` 或 `/provider-unarchive`。

## 验证入口

- `tests/test_tui_adapter_appserver.py` 使用 FakeDaemon/Transport 固定 initialize、
  session/turn/archive 请求形状、resume/fork/list、通知映射、终态单一性、审批往返、TTL、
  断线、未知通知、两阶段握手、默认/daemon transport argv 和三态 terminal 合同。
- `tests/test_tui_protocol.py`、`tests/test_tui_runtime.py`、`tests/test_tui_events.py`：
  provider-neutral 协议、能力和投影回归。
- `tests/test_tui_adapter_codex.py`：Structured Exec fallback 不受 App Server 层影响。
- 仓库门禁：`python quality.py --strict .`、`python pi-batch.py eval`、`make ci`。

## 结果与当前边界

T8 当时只交付 App Server 协议层；后续 T26 已通过显式
`--runtime app-server` 把该 adapter、live steer 与 typed approval 接入产品 TUI。
最新运行合同见 [`t26-appserver-approvals.md`](t26-appserver-approvals.md)、
[`t27-provider-reconcile.md`](t27-provider-reconcile.md) 与
[`t28-session-summary.md`](t28-session-summary.md)。当前 review 使用 inline provider
turn；TUI 会回收有界 message/typed finding evidence，写入与 turn/session/run 绑定的
review 报告，`f` 修复仍只启动普通 turn。sandbox/network/approval policy 有效值已可常驻
显示。2026-08-26 已使用本机
`codex-cli 0.149.0` 验证默认 stdio 进程的真实 initialize/initialized 握手；真实模型
turn 和自动重连/断线对账仍保留为可选集成测试与后续切片。默认 `auto` 继续使用
Structured Exec 或 Legacy；只有显式选择且握手成功才开放 App Server 富能力。

协议事实以 OpenAI 官方
[Codex App Server 文档](https://learn.chatgpt.com/docs/app-server)为准；其中明确要求
客户端在 `initialize` 成功后发送 `initialized`，并规定 `turn/completed` 的 status
为 `completed`、`interrupted` 或 `failed`。
