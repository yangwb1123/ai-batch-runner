# 固定版本工作区与远端产物

工作区快照让计算设备无需预先准备同一份输入文件。沿用已配置的 Hub/Fabric
计算连接和 `compute[].vault`；Vault default bucket 必须开启版本管理。只有同时
声明 `hub.compute.workspace.v1` 的控制面和 `task.workspace.v1` 的 Runner 能接收
快照任务。CLI、TUI、Console 使用相同 Hub API 与会话权限。

## 使用

```sh
# Hub URL / 私有 token 文件沿用 fleet 配置。
# 只包含明确选择的文件，避免无意上传目录中的凭据或运行数据。
python pi-batch.py fleet compute snapshot-upload --session-id SESSION \
  --directory ./job --file job.py --file input.json --idempotency-key my-input-v1
python pi-batch.py fleet compute snapshots --session-id SESSION

# 用上传回执的 snapshot_id；outputs 为执行后必须存在的文件，均相对快照根。
python pi-batch.py fleet compute submit --session-id SESSION \
  --snapshot-id SNAPSHOT_ID --output result.json \
  --argv-json '["python3","job.py"]' --timeout 120 --idempotency-key my-job-v1
python pi-batch.py fleet compute status TASK_ID

# 输出固定版本归档 ready 后，下载并恢复到全新目录，已存在目录会拒绝。
python pi-batch.py fleet compute workspace-output TASK_ID --directory ./remote-result \
  --input-sha256 INPUT_SNAPSHOT_SHA256
```

`--bundle-file file.json` 可替代上传时的 `--directory/--file`。CLI 自动生成的上传键
由会话和输入摘要决定；网络失败时以相同参数重试。计算提交仍需保留回执中的
Idempotency-Key。Gateway 活跃 turn 的 `--via` 工具支持同样的上传、列出、提交、
状态、取消和产物恢复，不把 Vault 凭据交给 agent。

TUI 的 Submit 表单可以上传本地 JSON 包、选择快照、填写输出路径 JSON；Tasks
面板可把归档输出恢复到新目录。Console 可粘贴 JSON、选择已上传快照、查看摘要与
产物状态；Web 支持选择 JSON 文件与下载。原生 App/Mobile 的宿主文件通道支持
通过系统对话框选择 JSON 文件、保存已校验的输出包。仅在宿主声明对应能力后
显示文件操作；JSON 查看、复制和粘贴仍可使用。CLI 的 `workspace-output` 命令
仍可按任务 ID 把远端输出恢复到新目录。平台源码、构建与设备验证范围见 Console 的
`docs/AGENT_NATIVE_WORKSPACE_FILES.md`。

## 边界与恢复

- 快照最多 128 个文件，单文件 64 KiB，全部原始内容 256 KiB，规范 JSON 512 KiB。
  内容以 Base64 保存，不压缩。快照任务的进程输出上限为 32 KiB，较大结果请写入
  指定文件；输出文件使用相同容量限制。当前不传目录、符号链接、权限位、
  大型依赖树或模型权重。可使用设备已有运行时，但应把任务所需小文件显式放入快照。
- 文件名为规范相对 POSIX 路径；拒绝绝对路径、父路径、Windows 保留字符/设备名、
  控制字符、大小写折叠碰撞和文件/父目录冲突。Unicode 必须是 NFC，路径最多
  512 UTF-8 字节、单段 255 字节。输出路径是文件名，不展开 glob。
- 快照仅能被原会话任务引用。读/写沿用 `agent.tasks:read/write` 及 source instance
  授权；调度仍取用户授权设备与项目映射交集。文件正文不进入任务列表、运行日志、
  IM 通知或审计事件。
- 快照任务以 `workdir=.` 在目标授权目录下的新目录运行，不覆盖设备现有工作区；
  这不是 OS 沙箱，命令仍须在受信任且已授权的设备身份下执行。
  恢复失败时可能保留空目录或不完整目录，请改用新 destination；失败清理不按
  可被并发替换的目录名执行删除。文件打包/恢复依赖 POSIX dir_fd 和 no-follow 支持。
- 输入按 Vault version_id/ETag/大小/SHA-256 校验；执行证据绑定输入和输出摘要。
  输出缺失、链接或摘要错误不会成为成功结果。完整 bundle 原样归档，不做会破坏
  文件字节的正文脱敏。
- `state` 是执行状态；`archive_state` 是证据归档状态；`workspace_result.state`
  是工作区产物状态。Vault 暂时不可用时已完成任务保持原执行结果，独立重试归档。
  控制面丢失活跃任务沿用 LOST 行为，不自动重跑可能有副作用的命令。
- 此版本仅支持 pinned 工作区任务。检查点迁移、大文件分块、自动源目录合并及
  多节点分布式训练仍需后续实现。原生保存由用户选择目的地，并由系统确认覆盖；
  保存的是 JSON 包，不自动把包内文件应用到当前项目。

API 与持久化说明见 [openapi.yaml](openapi.yaml)、[WORKSPACE_DESIGN.md](WORKSPACE_DESIGN.md)
和 [VAULT_WORKSPACE.md](VAULT_WORKSPACE.md)。实际验证记录见 [WORKSPACE_VERIFICATION.md](WORKSPACE_VERIFICATION.md)。
