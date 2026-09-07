# T28 · Session 索引摘要与 App Server 历史页

## 官方协议基线

Codex App Server 的 `thread/list` 是历史界面接口：默认按 `createdAt` 倒序，支持
cursor 分页、`updated_at` 排序、cwd 与 source kind 过滤，响应包含 `data` 和
`nextCursor`；thread 摘要包含 name/preview、provider、时间、pin/ephemeral 和结构化
runtime status。省略 `sourceKinds` 时只包含 `cli` 与 `vscode`，因此本实现显式查询
`cli/vscode/exec/appServer`。[Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server)

## 两层摘要，不同权威

Picker 仍只列出当前 resolved cwd、data_dir 与 memory scope 内通过 metadata 边界校验的
本地 journal。远端-only thread 不会变成可恢复项目，也不会创建本地 session。

本地层使用每 session 一个 `summary.json`：

- 只在 RuntimeEvent 已 durable、Projection 已成功提交后更新；
- 内容来自 journal 中已经持久化和脱敏的 event，不读取 raw provider 回调；
- 与 session/scope/cwd、最后 event 和精确 journal byte size 绑定；读取时在 journal
  锁内同时复验当前 fd、EOF 与最后一条有界 RuntimeEvent；
- 保存 runtime、provider handle、workspace、可信 branch、最后 turn 状态和有界完成消息摘要；
- provider handle/runtime/model 只从同一个最新 session lifecycle event 原子更新，普通
  message/provider 正文不能改写这些字段；
- 写入使用 journal lock 下的 byte-size + tail CAS，读取最多 32 KiB；多 writer 之间若前一
  summary event 不与当前 journal 倒数第二条相邻，就不写新 cache；
- 缺失、陈旧、损坏或符号链接 sidecar 只显示 `summary unavailable`，不隐藏 journal。

恢复回填只在 journal 的每条 event 都被 Projection 接受时发生。语义坏行、终态后的 late
event 或恢复中止都会让旧 cache 自然 stale/missing，摘要不会越过执行投影的可信前缀。
以上收紧以 sidecar schema v2 标识；旧 v1 cache 会按 invalid 降级，并在用户显式打开该
journal、完成可信恢复后懒重建，不做批量迁移。

旧 journal 不会在 `/sessions` 中批量重放。用户显式打开它时，本来就要完整恢复可信
前缀，此时才懒回填摘要。列表因此最多读取已截断为最近 50 条后的 50 个小 sidecar，
不会退化成 50 × 16 MiB 的同步扫描。

Provider 层由一次有界 `thread/list` 提供。Adapter 固定自身 canonical cwd、最多 100 条、
`updated_at desc`、四种顶层 source kind 和 `useStateDbOnly=true`；严格校验 id、status、
activeFlags tagged union、时间、布尔值和展示字段；响应必须显式携带可为 null 的
`nextCursor`。单条畸形数据被跳过并显示 warning；整个 provider 请求失败时，本地 picker
仍可使用。

两层仅以“本地摘要中的 App Server provider handle 完全相等”为 join 条件。
name/preview/modelProvider/status 都是带观测时间的 advisory display，关闭 markup、清理
控制字符并有界显示；它们不进入 prompt、transcript 或动作参数。

## 执行边界

`thread/list` 不加载、不订阅、不采用 thread，也不改变当前 RuntimeSession。列表中的
`idle` 不授权 resume，`active` 也不提前禁止 resume，因为状态可能立即变化。用户按
Enter 后仍执行原有动作门：

1. 精确重新打开本地 journal，恢复完整可信前缀；
2. 从可信 `session.created/resumed/forked` event 重新取得 provider handle；
3. 检查 runtime 与 capability；
4. App Server 再调用 `thread/read`；只有此刻的 `notLoaded`/`idle` 才允许 resume。

因此“list=idle、read=active”和“list=active、read=idle”两种竞态都有回归测试，均以
动作时 `thread/read` 为准。Picker 的非归档、非已知只读项标为 `candidate`，不再根据
mtime/marker 宣称 `available`。

## UI 与完成标准

本地发现和 provider RPC 都运行在独立 Textual thread worker。worker token 固定 canonical
cwd、resolved data root、完整 memory scope、runtime/capability 和 adapter identity；扫描
前后与 UI callback 都复验，任一边界变化的迟到结果直接丢弃。Picker 展示：

- 本地更新时间/大小/current/candidate/read-only/archived；
- runtime、最后 turn 状态、可信 branch、真实可得的 model 与完成消息摘要；
- provider name/preview、status/activeFlags、model provider、更新时间和观测时间；
- 本地/provider 截断与 warning，不把首个 provider page 冒充完整历史。

详情固定为五条按终端 cell 宽度截断的单行；activeFlags 最多展示前三项并带 `+N more`，
38/72 列窄屏回归保证 provider 细节不会把 Assistant preview 挤出详情区。

完成证据：`tests/test_tui_session_summary.py` 覆盖 sidecar durability、redaction、CAS、
stale/invalid/symlink、双 writer、坏投影后缀与懒回填；`tests/test_tui_sessions.py` 覆盖
exact join、remote-only 忽略、六类迟到边界、窄屏、worker 线程和 transcript 隔离；
`tests/test_tui_adapter_appserver.py` 覆盖分页、查询边界、tagged union、字段校验及两种状态
竞态。CI 使用 fake transport，不依赖账号或网络。另以本机 `codex-cli 0.149.0` 完成
initialize + `thread/list(limit=5)` 只读冒烟，得到 5 条 `notLoaded`、非空 next cursor、
0 warning；该结果只验证本机协议连通性，不替代 fake transport 的确定性门禁。
