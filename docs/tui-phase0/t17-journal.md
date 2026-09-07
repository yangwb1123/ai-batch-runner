# T17：RuntimeEvent 持久日志与投影重建

状态：已交付核心恢复合同、命令式 session index 与 capability-driven resume；
图形 picker、自动恢复与 provider 对账未交付。

## 已交付

- EventBus 先持久化 canonical RuntimeEvent，再让队列、订阅者和 projection 可见；
- 数据写入 `config.data_dir(cwd)` 下的 scope/session 摘要目录，metadata 绑定
  tenant、project、sensitivity、resolved cwd 和逻辑 session；
- session 内跨 run 保持顺序，event id 幂等，run seq 单调，多进程写者经文件锁串行；
- payload/extensions 落盘前做 best-effort 模式脱敏；所有 item/message/request id
  （含数字 request id）使用统一固定宽度摘要域，疑似含 secret 的 envelope identity
  或递归映射键直接拒绝；
- Controller 可从可信事件前缀重建 core projection、message、timeline 和 workspace
  snapshot；重放不发布订阅消息或追加 RuntimeEvent，但可修复 crash tail/pending append
  并持久化只读 marker；
- `tui.journal_enabled` 控制接线，`tui.journal_max_bytes` 控制单 session 文件上限。

## 会话发现与执行恢复

- `/sessions` 只扫描当前 resolved cwd + tenant/project/sensitivity scope 的摘要目录；
  扫描数与返回数均有硬上限，metadata、目录摘要和 session id 必须完全一致，符号
  链接及无效条目不会成为候选；
- `/resume <session-id>` 只接受精确、已存在的 session，不做模糊前缀匹配，也不会因
  查询不存在 id 而创建空 journal；
- 选择后先从可信前缀重建 Controller 与当前可见投影。只有 journal 可写、最后
  session event 含未脱敏 provider handle、历史 runtime 与当前 runtime 相同且
  capability 明确声明 `session_resume` 时，才允许执行续跑；
- Codex Structured Exec 将恢复标为 required，下一 turn 若 provider 拒绝 resume
  会 fail closed；普通进程内自动续轮仍可沿用既有兼容降级语义；
- UNKNOWN/损坏 journal、Legacy、不匹配 runtime 或缺失 handle 只读展示，不能把
  UI 投影恢复冒充 provider 上下文恢复。

## 损坏与重启语义

- 最后一条记录若因崩溃缺少换行，只截断该 tail，fsync 后允许继续；
- 完整坏行、重复/乱序或边界不匹配均 fail closed：只采用此前可信前缀，坏行之后
  即使看似合法也不应用；投影语义错误会创建共享 `read-only.json`，阻止其他
  writer 继续延长该 session；
- 恢复到非终态时，对应 turn 与 session 核心投影标记 UNKNOWN；只有当前可见 turn
  本身未决时 presentation 才显示 UNKNOWN。隐藏未决 turn 仍会令 Controller 只读，
  不会因最后可见 turn 已成功而漏掉对账要求；
- 每次 append 先持久化 pending `append-intent.json`，事件行 fsync 后再持久化
  committed `append-commit.json`，最后持久清除 intent 完成 finalize。事件只在
  finalize 成功后进入可见队列；此后 commit marker 清理是 best effort，清理失败
  不会把成功改报为失败。
- finalize 之前的 fsync/短写或 intent 持久清除失败会尝试回滚本次 append；
  回滚确认成功时事件不进入可见队列且可安全重试同一 id。回滚无法确认时，
  当前实例锁存只读，重写 pending intent，并 best-effort 写入共享
  `read-only.json`。
  新实例恢复始终 intent-first：只要 pending intent 存在，即使同时有 commit marker
  也回滚尾部；只有 intent 已持久清除后的 commit-only marker 与尾部
  start/length/digest 精确匹配时才确认已提交。`read-only.json` 只作保守
  共享锁存，不依赖 event id 选定截止点；选中的恢复 metadata 无效或与尾部
  不匹配时 fail closed。
- 终态后的合法 late audit 依然进入 journal；Projection 忽略 turn-scoped 可变事件
  且不回退终态，`workspace.snapshot` 作为显式例外仍可更新权威快照。

## 当前限制

- `/sessions` overlay 已支持精确 resume/fork 和本地 archive/restore；默认启动仍创建
  新 session，不自动选择旧 session；可用 `tui --native-session ID` 显式启动
  provider-free 只读恢复 viewer；
- 已有有界 `checkpoint.json` 投影完整性清单：绑定 scope、session、精确 journal
  byte size、最后 event id/seq 和 projection digest；它是可丢的缓存/一致性见证，不能
  替代 journal replay、授权或 provider resume。缺失、过期或 digest 不匹配时仍按
  可信 journal 从头重放，并保持只读安全边界；尚未实现跳过重放的状态快照加速。
  `TUIViewSnapshot.recovery` 只提供有界恢复 metadata，且不参与 projection equality
  或 checkpoint digest，因此 live/recovered projection 仍保持 replay-equivalent；
- Codex 可按历史 handle 在下一 turn resume；`thread/list` 瞬时 advisory 与动作前
  `thread/read` 预对账已完成，但没有 provider 持续订阅、自动恢复或 App Server 重连；
- 没有分段/压缩；达到容量或 run 上限后需显式开启新 session；
- journal 是必要的 UI 投影审计信息，不是 provider 原始 transcript 或完整聊天归档。

## 验收

以下确定性测试共同固定该合同：

- `tests/test_tui_journal.py`：持久顺序、边界、脱敏、可信前缀和 UNKNOWN/只读恢复；
- `tests/test_tui_journal_safety_regressions.py`：append/finalize 歧义、投影等价与 identity 安全；
- `tests/test_tui_journal_rotation_regressions.py`：容量耗尽与 session switch 原子性；
- `tests/test_tui_sessions.py`：scope 索引、精确打开、只读降级与 provider 续跑；
- `tests/test_tui_controller.py`、`tests/test_tui_events.py`、
  `tests/test_tui_wiring_regressions.py`：Controller、EventBus 与 TUI 接线一致性。

最终交付以 `make ci`、`git diff --check` 和上述回归全部通过为准。
