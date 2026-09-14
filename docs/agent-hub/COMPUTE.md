# 跨实例计算

CLI、TUI 与 Console 共享设备资源目录和计算任务 API。任务关联原会话，但有
独立状态；聊天结束不会删除计算任务。设备通过现有 Fabric Runner 执行结构化
argv，Hub 不接受 shell 字符串或客户端提供的远端绝对工作目录。

## 配置

先按 Fabric 文档注册并审批计算设备，以及一个 Hub 服务身份。在每个租户的
专用 Fabric 控制面配置运行目录和信任策略。Hub 不替管理员审批设备。
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
```

每个 tenant 的 Fabric URL 必须独立，不能用设备自报 tenant 代替身份隔离。
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
python pi-batch.py fleet compute cancel TASK
python pi-batch.py fleet compute tui
```

未完成任务有每用户 32、每租户 100、全局 1000 的入队上限。缺少
agent.tasks:read 时，提交/取消只返回不含命令与结果正文的回执。

不指定 `--target-device-id` 时，Hub 按项目、授权、在线状态、生命周期、CPU、
内存、运行时及可选 GPU 需求筛选设备；Fabric 在锁内再次做资源准入，防并发超卖。
每个 Runner 同时执行一个计算任务；单个任务可请求多个 CPU 核心，
多台 Runner 可以并行。任务在 Hub 等待空闲执行槽后才交给 Fabric，避免
尚未执行的任务占用会过期的租约。资源不足保持 queued，选定设备后不自动迁移。命令 timeout 为 1–3600 秒；
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
