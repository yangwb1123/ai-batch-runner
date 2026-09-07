# T21 · Scoped Changes Inspector

## 用户闭环

`/changes` 或 Ctrl+G 打开专用只读 overlay。左侧是当前 scope 的有界文件列表，
右侧显示选中文件的单个 unified diff hunk：↑/↓ 移动、Enter 打开文件，`n`/`p`
切换 hunk，`r` 重读快照，Esc 关闭。数字键直接切换：

1. all；
2. unstaged（index → worktree）；
3. staged（HEAD → index）；
4. untracked；
5. last-turn（可信 workspace delta 中的路径）。

Slash 还支持 `/changes base REF [PATH]` 与 `/changes commit REF [PATH]`。旧的
`/changes PATH` 保持为 all scope 的单路径快捷写法；`/files` 和 `/diff [path]`
继续提供 transcript 兼容输出。

## 只读与边界合同

- 所有 Git 调用使用 argv，不经过 shell，不修改 index、worktree、HEAD 或 refs。
- base/commit ref 必须非空、不以 `-` 开头、无控制字符且不超过 200 bytes；执行 diff
  前先解析为现有 commit，失败关闭。
- path 经 resolved Git root 校验；跨仓库 path 被拒绝。
- diff 最多 256 KiB、文件最多 200、hunk 最多 256；`.pi-batch/` 运行数据不进入列表。
- all/base 可纳入 untracked 文件；staged、unstaged、untracked 分别保持明确的
  HEAD→index、index→worktree 和未跟踪文件比较语义。
- last-turn 只从可信 Controller workspace delta（Legacy 使用 backend turn snapshot）
  取得路径，不解析 Assistant 正文。
- Inspector 操作不写 conversation transcript。active turn 期间仍拒绝打开稳定 scope；
  `/findings` 在 turn 结束后把已校验的相对路径/行号定位到最近 diff hunk。当前视图
  是显式刷新获得的稳定只读快照，不宣称 live 原子性。

## 实现与验证

- `pbatch/tui_workspace.py`：scope/ref/path 校验、有界 patch 与文件枚举。
- `pbatch/tui_changes.py`：不可变 snapshot、文件列表、hunk 切分与键盘 overlay。
- `pbatch/tui_inspector.py`：backend capability fallback、可信 delta 与界面接线。
- `tests/test_tui_workspace.py`、`tests/test_tui_inspector.py`：scope 分离、ref 解析、
  键盘导航、边界与 transcript 隔离。
