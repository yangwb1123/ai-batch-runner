# 计算任务调度诊断

任务保持 `queued` 时，可以查询各设备没有被选中的原因。CLI、TUI、Console
和当前运行回合的 Agent 工具使用同一接口；诊断复用 Hub 实际调度筛选函数。

## 使用

```sh
pi-batch fleet compute placement TASK_ID --json
pi-batch fleet compute placement TASK_ID --limit 20 --after DEVICE_ID --json
```

第一条读取当前第一页。仅当 `next_cursor` 非空时，将其原样传给 `--after`
继续读取；每次只读一页，最多 20 台设备。TUI 计算任务详情和 Console 任务详情
提供调度诊断入口，可刷新和查看下一页。窗口保留当前页，不自动轮询或提交任务。

启用 Gateway `--compute-tools` 后，Agent 可以通过当前回合的私有工具入口运行
`placement TASK_ID`，与已有 `devices`、`submit`、`status` 配合使用。该入口仅能
诊断当前回合自己提交的任务；回合结束、取消或 Gateway 换代后委托失效。

## 理解结果

`eligible=true` 表示设备在本次观察中满足 Hub 筛选条件。它不预留设备，也不保证
随后的执行准入成功；Fabric 仍在锁内检查并发占用、信任策略和资源配额。
`evaluated_at` 是 Hub 本次观察时间，多设备数据不是同时采集的冻结快照。

| 原因码 | 含义 |
|---|---|
| `device_offline` | 设备不在线 |
| `device_not_ready` | 设备生命周期尚不可调度 |
| `device_busy` | 已观察到执行或 CPU 预留占用 |
| `device_unavailable` | 设备未通过可调度状态检查，现有投影不足以进一步区分停收、执行槽或能力过期 |
| `project_unavailable` | 当前设备没有任务的项目映射 |
| `target_not_authorized` | 不属于任务的原允许目标集合 |
| `target_mismatch` | 任务指定了其他目标设备 |
| `workspace_unsupported` | 缺少快照工作区执行能力 |
| `cpu_insufficient` / `memory_insufficient` | 当前可用 CPU / 内存不满足请求 |
| `os_mismatch` / `architecture_mismatch` | 操作系统 / 架构不匹配 |
| `runtime_missing` | 缺少请求的运行时 |
| `gpu_unavailable` | 没有可用的 GPU 探测结果 |
| `gpu_insufficient` | 当前可调度物理卡数或每卡显存不足 |
| `inventory_missing` | 已配置的设备未出现在本次 Fabric 返回中 |

部分原因只用于共用筛选函数：公开诊断在读取 Fabric 前就过滤项目与授权范围，
不会为了展示 `project_unavailable` 或 `target_not_authorized` 泄漏范围外设备。
同一设备可以有多个阻塞原因。CPU 任务不受 GPU 状态影响。
CPU、内存和占用量必须是有限非负数；异常探测会使设备不可调度，不能以
NaN/Infinity 绕过资源比较。非法库存结构会返回明确的不可用错误。

任务已进入 dispatching/running、取消流程或终态时，返回当前状态和空设备列表，
界面说明排队诊断已不适用。这样不会把任务自身占用误报为等待原因。若查询期间
任务离开队列，服务端丢弃刚读取的设备观察。

## 授权与恢复边界

查询需要源实例的 `agent.tasks:read` 和 `agent.devices:read`；启用 Aero ID
成员检查时，仍必须通过成员门禁。可见设备集合是任务提交时的目标集合、当前
compute 实例授权和当前项目绑定的交集。权限增加不会把新设备自动加入旧任务。
没有可见设备不代表其他权限范围内没有容量，接口不返回隐藏设备或隐藏总数。

原会话离线或已冻结时，仍能读取已提交计算任务的诊断。读取不改变任务、事件、
幂等 key、重试时间或资源预留，也不会更换目标、重新提交或迁移任务。
修改运行条件后可以刷新观察；调整已提交任务的资源要求仍需遵循现有显式任务操作。

刷新或翻页检测到登录身份变化、读权限被拒绝时，客户端清空当前诊断页和游标。迟到响应不能
恢复旧身份或旧任务的设备信息。旧 Hub 没有该入口时显示不支持，其他计算功能
保持可用。

接口为 `GET /api/v1/agent/tasks/{task_id}/placement`，精确 DTO、固定原因集合和
分页限制见 [openapi.yaml](openapi.yaml)。查询不接受请求正文；响应最多 64 KiB。
