# T25：Command Output ArtifactRef

## 目标

让命令卡片保持轻量，同时把可信 runtime 明确标识的大输出保存为可恢复、可检查且磁盘
有界的 ArtifactRef；Assistant 正文和普通 callback 不参与判定。

## 正向流程

1. Structured Exec 收到 completed `command_execution.aggregated_output`；App Server 接收
   `item/commandExecution/outputDelta`，并在 completed item 到达时以显式 aggregate 为
   权威，缺少 aggregate 时使用已接收的 delta 流。
2. event payload 继续保留约 48 KiB 预览，Timeline 保留 2 KiB。总输出未超过事件预算
   时不创建文件。
3. 超出预算后，`CommandArtifactStore` 将 UTF-8 bytes 写入当前 data_dir 私有临时文件；
   完成 fsync、retention 裁决和原子 replace 后，Adapter 才发出 `artifact.created`。
4. 事件携带 `scope=data`、相对 path、media type、实际/原始字节数、SHA-256、截断状态及
   command item id。Completion 显示友好 name，`/artifacts` 使用本地 data_dir 根预览。
5. journal 只保存引用。session 恢复后文件仍在即可重读；若已被 retention 淘汰则明确
   显示 missing，不把引用存在冒充内容存在。

## 存储合同

- 布局：`<data_dir>/tui/artifacts/<workspace-hash>/<session-hash>/`；run、turn、item 仅进入
  SHA-256 截断摘要文件名，原始身份不落路径。
- 默认单文件 2 MiB，超限保留 UTF-8 边界并附加 truncation marker。
- 默认每 workspace/session 64 MiB、256 个完成文件；跨进程 retention lock 内按 mtime
  淘汰最旧文件。目录扫描和活动 capture 数量也有上限。
- 目录 0700，artifact/temp/lock 0600；symlink、非普通 target、超量目录和不一致容量
  fail closed。临时文件只在 fsync 后原子提升，关闭/断线清理未完成 capture。
- 存储故障发 `runtime.warning(category=artifact_store)`，command exit code 与 turn terminal
  state 仍由 provider 事件决定。

## 证据

- `tests/test_tui_command_artifacts.py`：小输出零文件、aggregate/stream、UTF-8 截断、权限、
  身份哈希、retention、并发锁、active limit、symlink 与临时文件清理。
- `tests/test_tui_adapter_codex.py`：1 MiB aggregate 自动提升和存储失败不改终态。
- `tests/test_tui_adapter_appserver.py`：显式 aggregate 与 delta-only 两条提升路径。
- `tests/test_tui_artifacts.py`：显式 data scope 只能落在本地解析的 data_dir。
