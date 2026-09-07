# T18 · Plan / Changes Inspector

## 交付边界

本批次把 Plan 与 Changes 从 conversation transcript 中分离为只读 Textual overlay：

- `/plan` 和命令面板打开当前 turn 的 Plan Inspector；
- `/changes [scope] [path]`、Ctrl+G 和命令面板打开 Changes Inspector；
- Esc 或 q 关闭 overlay，主界面与 composer 状态不被重置；
- `/files`、`/diff [path]` 继续提供 transcript 兼容输出。

## 单一状态源

Plan Inspector 不解析 Assistant 正文，也不维护第二份 plan store。`TimelineLog`
消费受信 `plan.updated` 后，在原 Timeline 条目中同时保留：

- 2 KiB 的紧凑 Timeline 文本预览；
- 同一个有界 `PlanSnapshot`（最多 64 步，单步 256 bytes）；
- provider-neutral step state 与 runtime status；provider `item_id` 仅用于内部原位合并，
  不作为用户界面噪声展示；显式 plan 的 goal、scope、commands、validators、gates、budget
  和风险边界以额外有界字段保留。

Controller 的 immutable snapshot 只做防御性复制；Inspector 读取当前 active Timeline
条目。相同 `item_id` 继续原位替换，overlay 打开时随 runtime snapshot 刷新。

Changes Inspector 同样不复制 workspace 状态：当前文件清单和 diff 来自只读
`workspace_status()` / `workspace_diff_scope()`，last-turn change set 优先使用
Controller 的权威 `workspace.snapshot` 投影，Legacy 路径才读取 backend snapshot。
all/unstaged/staged/untracked、base/commit ref、文件选择和 hunk 导航合同见
[`t21-changes-navigation.md`](t21-changes-navigation.md)。

每轮终态的跨域收口不塞入 Plan/Changes，而由独立 Completion Inspector 承担；其
typed event、权威 Git delta 与缺证据不声称通过的合同见
[`t23-completion-inspector.md`](t23-completion-inspector.md)。

## 诚实限制

- Changes 在 active turn 期间仍 fail closed，尚未提供 live debounce 刷新；
- 当前 hunk 导航展示 unified diff 片段，尚无行级光标、增删统计、二进制专用视图或
  Review finding 跳转；
- PlanSnapshot 的边界字段仅是 provider/deterministic-preflight 的展示证据，不授予执行
  权限；尚未把这些字段绑定到统一的执行授权、validator、gate、budget 或 remote side-effect
  控制面；
- Inspector 是只读展示，不执行修复、审批、commit 或其他副作用。

## 回归证据

`tests/test_tui_inspector.py` 覆盖：

- Inspector 可看到 Timeline 2 KiB 预览之外的同源有界步骤；
- Plan 空态与 Changes 三段投影；
- slash 命令和 Ctrl+G 打开 overlay；
- Esc 关闭、scope/path 传递、文件/hunk 导航和 transcript 零污染。

Timeline/Controller 原有回归继续验证 `item_id` 原位更新、状态规范化、ANSI 清理、
数量/字节上限和 journal 恢复。
