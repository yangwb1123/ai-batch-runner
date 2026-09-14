# P2 会话计算设计与持久化契约

Hub 复用 Fabric 执行平面。每个租户绑定独立 Fabric 控制端点；管理员配置
device → instance → project 工作目录映射，设备自报 tenant 不作为授权依据。
用户会话权限与计算目标权限独立，Gateway 仍只能管理自己实例的会话。

新增 agent.devices:read、agent.tasks:read/write/cancel；compute_instances 明确
计算目标范围。用户提交 prompt 时在同一事务内冻结本轮计算权限，Gateway
计算请求必须携带当前 generation、session、running turn，权限取 Gateway 与
发起用户交集。轮次结束即不可继续调度。

## 四类数据

- 领域模型：计算任务及状态转换、设备资源快照、不可变执行请求。
- 持久化模型：hub_compute_tasks（task_id 主键；tenant/actor/idempotency_key
  唯一；状态和事件同事务写入）、hub_compute_authority（turn_id 主键）。
  请求 JSON 保存有界结构化 argv/资源约束；私有调度字段与公开 DTO 分离。
- API DTO：不含 Fabric 凭据、租约或远端绝对工作目录；只返回有界脱敏结果。
- 归档模型：可选 Aero Vault 固定版本执行证据摘要，归档状态与执行状态独立。

任务入队先落盘，选机后固定目标，RPC 使用持久化 task_id 作为 request_id。
ACK 丢失、Hub 重启重复提交由 Fabric 持久化幂等保护；不自动换机重试。
状态 queued → dispatching → running → completed/failed/cancelled/lost，
取消经过 cancel_requested，确认进程停止或结果到达才释放资源。
Hub 使用 pinned mobility；Fabric 在锁内按 CPU/内存进行准入，执行时持续心跳。

SQLite 沿用 Hub WAL、FULL 同步及单写入所有者锁。新表用 IF NOT EXISTS
增量创建，不重写 P1 会话数据；索引覆盖租户分页、活跃任务查询和幂等键。
任务和事件不物理删除，权限快照不由后续重试覆盖。队列有总量上限，所有
网络请求、结果和列表有界。数据库/网络失败显式返回或保留待协调状态。

本轮要求设备已安装任务运行时并配置工作目录，不承诺文件系统沙箱、GPU
调度或不可变工作区传输。Vault 仅归档执行证据，版本锁定下载能力不会被
普通 presign URL 冒充。生产部署、真实账号联调及原生安装包另行验证。
