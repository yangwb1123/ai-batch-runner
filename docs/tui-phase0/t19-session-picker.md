# T19 · Session Picker、Fork 与可逆 Archive

## 交付范围

`/sessions` 现在打开独立的键盘 overlay，不再向 conversation transcript 注入索引文本。
Picker 只消费当前 resolved cwd、data_dir 与 memory scope 中通过 metadata 边界校验的
最近 50 个 journal，并显示更新时间、事件大小和 current/candidate/read-only/archived
状态。T28 进一步加入 byte-bound 本地摘要与 App Server `thread/list` advisory；详细
权威边界见 [`t28-session-summary.md`](t28-session-summary.md)。

| 键 | 行为 |
|---|---|
| Enter | 恢复 available/read-only 历史；若已 archived，则先恢复 archive 标记 |
| `f` | 按 runtime `session_fork` capability 分叉所选 provider context |
| `v` | 以精确 session id 打开 provider-free Native Pipeline 只读 viewer |
| `a` | archive 或 restore 所选本地 journal；不触发 provider 远端副作用 |
| `n` | 新建 session |
| `r` | 重新扫描当前 scope |
| Esc / `q` | 关闭，不改变会话 |

命令式等价入口保留为 `/resume <id>`、`/fork [id]`、`/archive <id>` 和
`/unarchive <id>` 和 `/native-recover <id>`，便于小终端、测试与可重复操作。

## Fork 合同

Fork 不是复制 transcript，也不从历史正文推断 provider id。控制器只接受可信
`session.created`、`session.resumed` 或 `session.forked` 事件中的
`provider_session_id`，并同时要求：

1. 来源 journal 没有损坏或未决 turn；
2. 历史 runtime 与当前 runtime 身份相同；
3. 当前 runtime 明确声明 `session_fork`；
4. backend/adapter 实现精确 `fork_session`。

Codex Structured Exec 的 CLI fork 必须与下一条 prompt 一起执行。因此 Picker 先创建
一个空的新本地 session，并把 provider fork 标记为 required；下一 turn 使用
`codex exec fork <parent-thread> <prompt>`。若 fork 被拒或 adapter 不支持，执行失败关闭，
不会回退成 resume 或 fresh session。观察到新的 child provider handle 后，后续 turn
改为正常 resume。

## Archive 合同

Picker 的 `a`、`/archive` 和 `/unarchive` 仍明确是本地 journal 生命周期标记，不触发
provider 远端副作用。显式 `/provider-archive <session-id>` 和
`/provider-unarchive <session-id>` 才调用 provider 的 archive API；这两类状态不会互相
伪装或自动回退：

- provider 操作只接受当前 runtime 声明的 `session_archive` capability，以及本地
  journal 中可信的 provider session handle；runtime 不匹配、handle 缺失、journal 只读或
  有未决 turn 时 fail closed；
- App Server 发送严格的 `thread/archive` / `thread/unarchive`，unarchive 必须由返回
  的相同 thread id 确认；活动 provider thread 不允许 archive；
- provider archive 成功不会写本地 `archived.json`，本地 archive 也不会宣称远端已归档；
  `/sessions` 的 provider `thread/list` 仍只是带时间戳的 advisory 快照；

本地 journal 生命周期继续遵守：

- `archived.json` 与 event append 共用 session journal 文件锁；
- archive 前以恢复时的 event byte size 做 compare-and-mark，检查后若日志变化则拒绝；
- 标记存在时任何 append 都 fail closed，历史仍可完整恢复；
- restore 只移除已验证的标记，不改写 `events.jsonl`；
- 活动 session、含未决 turn 的 session、跨 scope/无效 metadata 和符号链接均拒绝；
- archived session 仍出现在 Picker，但恢复标记前不能 resume 或 fork。

这一设计让操作可逆、可审计，也避免把本地整理动作伪报成 provider 已归档。

## 摘要与瞬时 Provider 状态

`summary.json` 是可丢的索引缓存，不是 projection checkpoint：只有 event durable 且
projection commit 后才写入，并与精确 journal byte size 绑定。缺失/陈旧/损坏缓存仍
保留 session，显示 unavailable。App Server `thread/list` 的 name/preview/status 也只
用于明文、无 markup 的 advisory 展示；远端-only thread 不进入 picker，Enter 后仍
完整恢复 journal 并通过 `thread/read` 动作门。因此未归档条目统一标为 candidate，
不根据 mtime 或列表快照宣称可执行。

## 验证

- `tests/test_tui_sessions.py`：overlay/transcript 隔离、键盘选择、精确 resume、
  capability fork、archive/restore 和 scope 边界。
- `tests/test_tui_journal.py`：archive append fencing、compare-and-mark、无损恢复、
  marker schema 与符号链接拒绝。
- `tests/test_tui_backend.py` 与 adapter 契约测试：pending fork、child handle 捕获及
  required 操作失败关闭。
