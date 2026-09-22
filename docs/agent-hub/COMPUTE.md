# 跨实例计算

CLI、TUI 与 Console 共享设备资源目录和计算任务 API。任务关联原会话，但有
独立状态；聊天结束不会删除计算任务。设备通过现有 Fabric Runner 执行结构化
argv，Hub 不接受 shell 字符串或客户端提供的远端绝对工作目录。

## 配置

先按 Fabric 文档注册并审批计算设备，以及一个 Hub 服务身份。在每个租户的
一个或多个 Fabric 控制面配置运行目录和信任策略。Hub 不替管理员审批设备。
Fabric secret 可轮换，但服务 device_id 必须保持稳定；未确认任务绑定原服务身份，
身份改变会暂停协调并报告 fabric_identity_changed，避免产生新的幂等命名空间。
Hub 无法证明任意 argv 的副作用可撤销，因此使用保守的 irreversible effect
和 internal data class；目标设备策略必须明确允许这两项。
Hub 凭据文件为 mode 0600 的单行 JSON：`{"device_id":"hub-service","secret":"…"}`。
它只留在 Hub 主机；控制面按该身份认证 HMAC 请求。远端连接必须使用 HTTPS。

在 `fleet serve --config` 的 YAML 中追加以下配置（替换所有占位值）：

```yaml
compute:
  - tenant_id: TEAM
    base_url: https://fabric.example.test
    credential_file: /private/fabric-service.json
    ca_file: /private/fabric-ca.pem
    cert_file: /private/hub-service.pem
    key_file: /private/hub-service-key.pem
    bindings:
      - device_id: DEVICE_A
        instance_id: alpha
        workspaces:
          project-one: /srv/work/project-one
      - device_id: DEVICE_B
        instance_id: beta
        workspaces:
          project-one: /home/worker/project-one
    # 可选：默认 bucket 已启用 versioning，token 有读配置、版本和写文件权限。
    vault:
      base_url: https://vault.example.test
      tenant_id: TEAM
      token_file: /private/vault-token
      prefix: agent-hub/evidence
  # 同一 tenant 的第二个 Fabric；device_id 必须与上面的连接不重复。
  - tenant_id: TEAM
    base_url: https://fabric-west.example.test
    credential_file: /private/fabric-west-service.json
    ca_file: /private/fabric-ca.pem
    cert_file: /private/hub-service.pem
    key_file: /private/hub-service-key.pem
    bindings:
      - device_id: DEVICE_WEST_A
        instance_id: west
        workspaces:
          project-one: /srv/worker/project-one
```

一个 tenant 可以声明多个 Fabric 连接；每个控制端点 URL 必须唯一，跨连接的
`device_id` 也必须唯一。Hub 会把这些连接的设备清单合并给 CLI、TUI、Web、App
和 Mobile，并按最终选中的 `device_id` 把 inventory、任务派发、状态轮询和证据
归档路由回其所属 Fabric。不能用设备自报 tenant 代替身份隔离。
聚合会话、设备和任务视图始终保留 `instance_id`；TUI 和 Flutter Console 在列表、
详情及设备卡片中显示来源实例，避免不同实例使用相同名称时误选资源。
自动调度对联邦连接采用部分失败隔离：某个 Fabric 的 inventory 暂时不可用时，
其他健康连接仍可接收自动任务；故障连接上的显式目标保持 queued 等待。面向用户
的完整 inventory 读取仍在任一连接失败时返回不可用，避免把不完整目录伪装成完整资源视图。
同一 tenant 的多个连接若使用工作区快照，建议配置同一个 Vault；不同 Vault
需要在提交时固定目标设备，否则 Hub 会拒绝存在歧义的快照操作。
计算设备不必运行聊天会话；其 instance_id 是 Hub 中管理员定义的权限分组。
Gateway 用 `--project-id project-one` 使跨设备项目映射一致。

用户 grant 增加 `agent.devices:read`、`agent.tasks:read`、`agent.tasks:write`、
`agent.tasks:cancel`；`instances` 仍决定可操作的源会话，`compute_instances`
决定可调度的目标实例，省略时继承 `instances`。Snaplink OAuth resource/client
也须允许相应 scope，JWT scope 与 Hub grant 取交集。
若 Snaplink 启用了全局 scope registry，须在 registry 中额外注册上述四个
计算 scope；只更新 resource/client 的 allowed_scopes 不够。

Gateway 要让会话 Agent 调度其他设备，需显式授予上述计算 scope 和
`compute_instances`，保留 `agent.gateway:connect`，机器令牌也需申请计算
scope，并以 `fleet agent ... --compute-tools` 启动。每轮 prompt
自动注入 `fleet compute --via <private capability>` 工具说明。该能力经本地
Unix socket 转发，只在原会话的 running turn 有效，同时受发起用户与 Gateway
权限交集约束。工具代理不会向 Provider 传递 Hub/Fabric/Vault 凭据。

## 使用

```bash
python pi-batch.py fleet compute devices --project-id project-one
python pi-batch.py fleet compute submit --session-id SESSION \
  --argv-json '["python","job.py"]' --cpu-cores 2 --memory-mib 512 \
  --timeout 300 --idempotency-key MY_STABLE_REQUEST_KEY
python pi-batch.py fleet compute tasks --session-id SESSION
python pi-batch.py fleet compute status TASK
python pi-batch.py fleet compute placement TASK --json
python pi-batch.py fleet compute cancel TASK
python pi-batch.py fleet compute reschedule TASK --target-device-id DEVICE_B
# 可能重复副作用；必须显式确认，省略目标时保留原显式目标
python pi-batch.py fleet compute retry TASK --confirm-duplicate
python pi-batch.py fleet compute tui
```

未完成任务有每用户 32、每租户 100、全局 1000 的入队上限。缺少
agent.tasks:read 时，提交/取消只返回不含命令与结果正文的回执。

不指定 `--target-device-id` 时，Hub 在该 tenant 的全部已授权 Fabric 连接上按项目、在线状态、生命周期、CPU、
内存、运行时及可选 GPU 需求筛选设备；Fabric 在锁内再次做资源准入，防并发超卖。
每个 Runner 同时执行一个计算任务；单个任务可请求多个 CPU 核心，
多台 Runner 可以并行。任务在 Hub 等待空闲执行槽后才交给 Fabric，避免
尚未执行的任务占用会过期的租约。资源不足保持 queued，选定设备后不自动迁移；
排队期间可用 reschedule 改变目标，但只能使用原任务授权集合内的设备，且任务创建者才可操作。
如果已派发的 Fabric 租约在 Runner 启动前因设备失联而变为 lost（Fabric 明确返回
`started_at: null`），Hub 会清理旧句柄，
为下一次派发生成新的请求标识并回到 queued；显式目标仍然受限于该设备，不会悄悄改投。
一旦 Fabric 返回 `started_at`，Hub 保持 lost 终态，避免对可能产生副作用的命令隐式重试。
如果需要恢复这种终态，用户可以执行 `retry TASK --confirm-duplicate`。这是一个
有意的重复风险确认：只接受 `lost` 任务，任务创建者必须仍有写权限，旧 Fabric
句柄会被清空并生成新的请求标识。省略 `--target-device-id` 会保留原来的显式
目标；原本自动放置的任务仍按当前授权设备重新选择。启用了证据归档的任务不允许
重试，以免和不可变归档记录竞争。CLI、TUI、活动回合工具和 Console 使用同一
接口，均不会自动执行此动作。
排队重调度验收见 [RESCHEDULE_VERIFICATION.md](RESCHEDULE_VERIFICATION.md)，派发前失联恢复见
[PRESTART_RECOVERY_VERIFICATION.md](PRESTART_RECOVERY_VERIFICATION.md)。
命令 timeout 为 1–3600 秒；
内存是调度预算，不是 OS 内存限额，CPU 预算也不限制子进程实际占用。
工作目录映射不是文件系统沙箱；argv 中启动的程序具有 Runner 操作系统账户的权限。

提交前持久化幂等键，断线重试复用同一个键；相同键的不同请求返回 409。
运行中取消先标记 cancel_requested，Runner 停止进程组并回传证据后终结。
stdout/stderr 各最多显示 8 KiB，并带 output_truncated。Vault 归档失败通过
archive_state=failed 独立呈现，每 60 秒重试，不改写已经完成的计算结果。

支持预先配置的 POSIX 工作目录、CPU/内存计算，以及 NVIDIA CUDA 物理 GPU
探测和调度，GPU 配置与边界见 [GPU.md](GPU.md)。固定版本工作区分发见
[WORKSPACE.md](WORKSPACE.md)。真实 CUDA 工作负载、跨主机真实部署、原生客户端安装包尚未验收。
源会话的受控执行和设备侧受信任进程是权限边界，不承诺对恶意本机同账户进程隔离。

任务排队时，使用[调度诊断](PLACEMENT.md)查看当前授权设备的阻塞原因；
这是即时只读观察，实际资源准入仍由 Fabric 决定。
