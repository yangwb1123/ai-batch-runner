# Vault 工作区分发设计

本轮把任务输入从设备上预先存在的目录扩展为固定版本工作区快照。上传、
调度、执行和产物归档分属不同状态；归档重试不重新执行命令。

## 数据与持久化

- API 模型：`pbatch.workspace.v1` 包含按相对路径排序的 `files`，每项仅有
  `path` 和规范 Base64 `content_b64`。最多 128 文件，单文件 64 KiB，总原始
  256 KiB，规范 JSON 512 KiB。无压缩、链接、权限位或设备文件。
- 领域模型：Snapshot 是不可变输入；Task 引用同会话 Snapshot 和显式输出文件
  清单；ExecutionEvidence 绑定输入及输出 SHA-256。SHA-256 覆盖 UTF-8 JSON：
  key 排序、无空白、非 ASCII 保留、禁止 NaN，文件按路径排序。
- 存储模型：Hub SQLite 新增 `hub_workspace_snapshots`，主键为随机 snapshot_id，
  保存 tenant、actor、session、instance、project、幂等键、摘要、大小、文件数、
  时间、状态、Vault 固定版本引用。唯一键 `(tenant,actor,idempotency_key)`；
  `(tenant,session_id,snapshot_id)` 用于授权后分页。不保存输入正文。
- 表通过 `CREATE TABLE/INDEX IF NOT EXISTS` 加法迁移；旧任务没有 workspace 字段，
  原 CPU/GPU 请求的规范化、幂等摘要与执行方式保持兼容。
- 任务私有 JSON 保存固定输入引用及已校验的待归档输出包；公开任务只投影元数据。
  Fabric 私有任务轮询传入输入包，执行证据传出输出包；各次 RPC 仍受 1 MiB
  硬限制，任务快照受 2 MiB 限制；任务日志压缩后仍检查 64 MiB 总容量，超限
  保留旧文件。含工作区任务的设备轮询一次最多领取一个任务，避免领取后响应
  超限而丢失任务。普通 Hub 请求仍为 64 KiB，仅快照上传和
  活跃 turn 的 compute broker 入口允许 576 KiB（内部普通操作仍为 64 KiB）。

## 授权、状态与恢复

上传和任务绑定使用 `agent.tasks:write`，列出与下载使用 `agent.tasks:read`，
都检查当前 source session 的 instance grant。快照仅绑定原会话。Gateway
代理继续取用户/Gateway 权限交集、活跃 turn 和 generation；不向 agent 或
Runner 传递 Hub、Fabric 服务主体或 Vault token。Fabric 以设备认证、任务 grant、
lease 与 fencing 授权已选执行端，旧 Runner 不接受 workspace 任务。

快照先以幂等键和摘要登记 pending，网络传输在 SQLite 事务外执行；固定版本
读回一致才成为 ready。上传失败/丢 ACK 后同键同内容可续传，不同内容冲突。
每 actor 最多 200、每 tenant 最多 1000、全局最多 10000 快照，含 pending。
快照 ready 事件由 Hub 会话事件账本记录。现有远端审计/IM 集成继续投影
compute 生命周期事件；首版不向远端投影 workspace.ready 或文件正文。

任务先校验并保存固定版本引用，再按授权设备能力放置。Hub 下载并验证输入，
通过签名 Fabric 请求送到指定任务。Runner 在已授权 cwd 下创建新目录，校验
输入摘要后恢复；执行前再次确认 fencing。快照任务 workdir 为 `.`，不覆盖
目标现有目录；仅支持 pinned、非 checkpoint 任务。

输出只读取显式文件路径，拒绝链接/路径穿越/特种文件，沿用输入容量限制。
Hub 检查 input_sha256 与固定输入相同、输出包摘要与证据相同，然后将输出写入
Vault。`workspace_result.state` 为 pending/ready/failed/unavailable；执行结果
与归档结果独立。下载经 Hub 当前授权检查和 Vault 固定版本复验，返回 JSON 包，
CLI 仅恢复到新目录。首版不自动把远端文件覆盖或合并到源工作区。

## 客户端与验证边界

CLI 支持显式选文件打包、上传、选择快照提交、下载恢复；TUI 允许选快照和输出。
Console 各端支持 JSON 包上传、选择与查看；Web 可选文件/下载，原生端通过宿主
能力握手提供系统文件选择/保存，缺少宿主时保留 JSON 文本操作。平台构建与实际
设备验证范围由 Console 的原生工作区文档单独记录。既有 token/正文脱敏不用于文件
内容，以免损坏用户明确选择的输入输出；日志和任务列表不包含文件正文。

验收覆盖真实本地 Hub/Fabric/Runner 和镜像 Vault REST 契约的服务替身：固定版本、
幂等重试、越权、错误摘要、路径/链接、超限、取消/fencing、重启与输出归档失败。
未实际连接部署的生态服务、真实跨机器网络或原生宿主文件系统时，不宣称对应验证。
