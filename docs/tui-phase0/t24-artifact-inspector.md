# T24：Artifact Inspector

## 目标

让 Completion 中的明确产物引用成为可检查对象，同时保持事件真值、路径边界、容量预算
和 conversation transcript 彼此独立。

## 正向流程

1. Runtime 发出 turn-scoped canonical `artifact.created`。
2. Controller 按当前 `run_id + turn_id` 构建 immutable `ArtifactCatalogSnapshot`，仅保留
   最新 64 项，并随 journal 恢复重建同一目录。
3. 用户执行 `/artifacts [N]`、`/artifact [N]`，或在 Completion 中按 `a`。
4. Inspector 展示事件 id、actor、media type、声明大小、digest 和引用诊断；↑/↓ +
   Enter 选择，`r` 重新读取，活动 turn 中新增引用原位进入列表。
5. 本地 path/file URI 先绑定当前 backend workspace；显式 `scope=data` 则绑定当前
   进程根据同一 workspace 解析出的 data_dir。随后通过目录 fd 逐级 `O_NOFOLLOW`
   打开；只有普通 UTF-8 文件展示前 64 KiB。

## 信任与读取合同

- 不扫描 Assistant 正文、command 输出或 `file.changed` 来猜路径。
- 不联网获取 http/https/ssh 等 URI，不启动编辑器、浏览器或系统 viewer。
- 绝对路径和 `..` 归一化后必须仍位于对应授权根。事件只能选择 `workspace` 或 `data`
  scope，不能携带根路径；缺少 workspace root 时，data scope 同样停止预览。
- 最终文件及每个中间路径组件都禁止符号链接；不具备安全 dir-fd/no-follow 能力的平台
  停止预览。
- 目录、设备、缺失/不可读文件、NUL/超长路径显式显示状态；二进制和非 UTF-8 只显示
  元数据。声明的 MIME/size 不覆盖 `fstat` 结果。
- 内容用 `Static(markup=False)` 显示，ANSI 与终端控制字符被清理；检查结果不进入
  conversation transcript。

## 容量与诊断

- catalog：64 refs；catalog diagnostics：16；单引用路径/URI：4096 bytes。
- preview：64 KiB；读取最多预算加 1 byte，用于确定截断。
- recent event ring 缺少当前 turn 起点时显示 event-window truncation；引用总数超限时显示
  artifact-list truncation。

## 验证

`tests/test_tui_artifacts.py` 覆盖 typed-only/current-turn 选择、列表上限、畸形元数据、
UTF-8/截断、远程 URI、越界、最终与中间 symlink、目录、二进制、缺失文件、Completion
跳转、活动事件刷新、手工刷新、workspace/data 双 scope、transcript 零污染及 journal
等价恢复。
