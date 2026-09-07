# T26 — Codex App Server 主 TUI 与 typed approval

## 交付范围

`pi-batch tui --runtime app-server` 显式选择 Codex App Server。默认 `auto`
保持原有 Structured Exec/Legacy 路由，App Server 不可用时显式模式 fail closed，
不会静默伪装为富运行时。

该路径按官方 [Codex App Server](https://learn.chatgpt.com/docs/app-server)
JSON-RPC 合同完成：

- 本地默认拥有 `codex app-server` stdio 子进程；只有显式 daemon socket 才使用
  `app-server proxy`；
- `initialize`/`initialized` 后建立 thread，`/model` 值传入 `thread/start` 和下一次
  `turn/start`；
- 主 TUI 消费同一有界 RuntimeEvent stream，因此 App Server 的 steer、interrupt、
  resume/fork 和 typed approval capability 都来自已初始化连接；
- server-initiated approval request 只映射为 `approval.requested`，Assistant 正文和
  stderr 永不生成审批；
- 决策使用 JSON-RPC response 回到同一 request id；现代 request 映射
  `accept`、`acceptForSession`、`decline`、`cancel`，旧方法保持兼容映射。

## 界面合同

收到可信 `approval.requested` 后，Controller 把当前只读快照置为
`WAITING_APPROVAL`，TUI 自动打开 modal：

```text
a         allow once
s         allow for session（仅 runtime 提供时）
d/Enter   deny（安全默认）
i         interrupt/cancel
Esc       关闭卡片但保持 pending；/approvals 可重新打开
```

卡片显示 approval type、method、带 JSON 类型的 request id、
session/turn/actor、目标、接收时间和失效倒计时。网络请求优先显示
`networkApprovalContext`，不会用普通 command 文本掩盖网络目标。

## 安全与恢复不变量

- pending request 与 decision 精确绑定
  `workspace/session/run/turn/actor/request-id-type/request-id`；字符串 `"1"` 与整数
  `1` 不可互换。
- 原始 `result` 注入、类型错配、跨 turn、重复、迟到和已失效 decision 全部拒绝。
- pending 集合默认最多 32 个；超限 request 立即返回 decline，不进入 UI。
- 本地 TTL 到期先从可决策集合移除，再向服务端发送 decline，并追加
  `approval.expired`；断线清空 pending；正常退出在关闭 transport 前尽力 decline
  所有 outstanding request。
- terminal turn 清除遗留卡片。未终结 journal 恢复为 `UNKNOWN` 且只读，历史卡片
  可检查但不能发送 decision。
- request/resolution/expiry 都进入 append-only runtime journal；modal 操作不写入
  conversation transcript。

## 验证

离线测试覆盖投影重建、request id 类型隔离、重复/错配、allow/deny、只读卡片、
TTL、断线、退出、pending 上限、网络目标和主 TUI 自动弹窗。另用本机安装的官方
Codex CLI 完成只握手探测；CI 不依赖账户或网络。

sandbox/approval policy/network 的有效配置常驻展示已在 T31 承接：只接受
App Server 明确返回的标量值，结构化值显示 `present`，缺失仍显示未知，不根据
本地猜测填充。后续 T27 已为 App Server resume 增加非订阅 `thread/read` 预对账，并校验
`turn/steer` 的响应 turnId；见
[`t27-provider-reconcile.md`](t27-provider-reconcile.md)。
