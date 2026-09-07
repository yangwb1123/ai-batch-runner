# T27 — Codex provider 恢复预对账

## 目标

在本地 journal 已经满足恢复条件时，仍先确认 Codex App Server 保存的 thread 状态，
避免把 provider 仍在运行、等待审批或系统错误的 thread 错误采用为当前执行上下文。

实现依据是 OpenAI 官方
[Codex App Server 文档](https://learn.chatgpt.com/docs/app-server#read-a-stored-thread-without-resuming)：
`thread/read` 读取持久 thread 而不 resume、不加载、不订阅，也不会发出
`thread/started`；返回的结构化 status 为 `notLoaded`、`idle`、`systemError` 或带
`activeFlags` 的 `active`。

## 已交付合同

- App Server runtime 新增真实 `session_read` capability 和只读 `read_session()`；请求
  固定使用 `thread/read {threadId, includeTurns:false}`。
- 响应必须包含对象 `thread`、与请求完全相同的 thread id、对象 status 和已知
  `status.type`。active flags 有类型、数量和字段长度上限。
- `read_session()` 不替换当前 `RuntimeSession`、不订阅目标 thread、不发布 session 或
  turn event。
- `resume_session()` 在任何本地 session 变更前完成预对账。只有 `notLoaded` 与 `idle`
  可以继续调用 `thread/resume`。
- `active`、`systemError`、未知/畸形响应和读取错误全部 fail closed。产品 TUI 仍重建
  本地可信 journal，但设置为只读，要求 `/new` 后才能执行；provider session 保持原值。
- `turn/steer` 仅在服务端响应 `turnId` 与请求的 `expectedTurnId` 完全一致后发布
  `turn.steered`，错误或缺失确认不会进入可信 Timeline。

## 证据与边界

FakeDaemon 测试覆盖 read→resume 的线序、只读查询无状态污染、四类 provider status、
畸形响应、active 历史降级和 steer 响应错配。当前对账只用于显式 App Server resume；
Structured Exec 没有等价的非订阅读取接口，因此继续按已有 required-resume 合同失败
关闭。协议没有条件式 resume，read 与 resume 之间仍存在服务端状态变化窗口；后续可
结合 `thread/status/changed` 做持续对账，但不能据此改写已经持久化的本地终态。
