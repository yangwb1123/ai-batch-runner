# AADM-D：设备感知执行织网（Device-Aware Execution Fabric）

> **定位**：把原来的 `Agent → 本机工具 → 本机执行` 升级为
> `Agent → 任务图 → 执行调度器 → 设备资源图 → 本机/远程设备/集群/模型服务`
> 的**独立、可关闭、默认本地运行的执行平面**。它是从单机 Agent Framework
> 走向**分布式 Agent Runtime** 的关键一步，与 `docs/AADM.md`（理论）、
> `docs/AADM_RUNTIME.md`（运行时）、`docs/AADM_GOVERNANCE.md`（治理）
> 并列。
>
> **核心原则**：
> - **位置透明 Location Transparent**：上层 Agent 不需要关心任务在哪台设备执行
> - **故障显式 Failure Explicit**：调度器必须明确处理网络延迟、设备离线、
>   重复执行、状态迁移、安全授权和版本差异
> - 一句话：**接口上可以像调用单机一样简单，语义上绝不能假装网络就是本机。**
>
> **状态图例**：✅ 已落地（仓库有对应实现）｜🟡 部分落地｜🔴 待实现

## 0. 结论先行

值得扩展，且**现在预留、渐进实现**。正确方式不是一开始构建复杂的设备
集群，而是：

```
现在预留 ExecutionTarget 抽象 → 先支持本机 → SSH Inventory + 只读探测
→ Runner + 受控远程执行 → 动态调度 → Checkpoint/迁移/联邦
```

## 1. 价值：为什么值得加入

| 价值 | 内容 |
|---|---|
| 异构算力统一 | AMD 大统一内存（大模型/本地推理）、NVIDIA（CUDA 推理/图像）、Apple Silicon（Metal/macOS 构建）、Raspberry Pi（常驻控制平面/探针/监控）、Linux 服务器（容器/数据库/CI/长批处理）、Windows（专用构建/兼容测试） |
| 利用空闲设备 | 策略：仅空闲运行、仅接通电源、电池 >80%、温度阈值、工作时间不跑重任务、允许 CPU 不占 GPU |
| 跨平台测试 | Linux amd64/arm64、macOS arm64、Windows amd64、CUDA/ROCm/Metal/Vulkan——**增加独立证据** |
| 数据就近执行 | 几十 GB 的模型/数据集不该传给算力，应该把任务发给数据所在设备：**计算向数据移动** |
| 故障转移 | 仅纯计算/只读/幂等/有 Checkpoint/沙箱任务可迁移；有真实副作用的不能复制重跑 |
| 模型与工具能力路由 | 调用"某种能力"而非"SSH 到某台机器执行命令"：`capabilityBroker.invoke("model.qwen3-35b", input)` |

## 2. 四个概念必须分离 🔴

```
Logical Agent（负责什么角色）
  → Task Node（要完成什么任务）
  → Execution Attempt（这一次具体执行）
  → Execution Target（在哪里执行）
```

一个逻辑 Agent 可以：AMD 上本地模型规划 → Linux 服务器编译 → Mac 上
macOS 测试。**不要设计成"一个 Agent 永久绑定一台设备"**——否则将来
无法迁移、容错和动态调度。

## 3. 任务图映射到资源图

任务图 `G_T = (V_T, E_T)`（节点 + 依赖/数据传递），资源图
`G_R = (V_R, E_R)`（设备/集群/模型服务 + 网络连接/带宽/延迟/信任关系）。
调度器寻找映射 `φ: V_T → V_R`，必须满足：

```
硬件能力 / 操作系统 / 运行时 / 权限 / 安全等级 / 数据允许移动
内存和显存足够 / 设备当前可用 / 依赖版本兼容 / 副作用类型允许
```

之后才优化：关键路径时间、跨设备通信量、模型冷启动、数据搬运、设备负载、
可靠性、可选的 Token 和金钱成本。

## 4. Flag 不能只有一个布尔值 🔴

`device_fabric: { enabled: true }` 只表示**加载模块**，不等于允许扫描
网络、登录设备、远程执行、迁移任务、分发敏感数据——这些能力**分开授权**：

```yaml
device_fabric:
  enabled: true
  inventory: { enabled: true }
  discovery:
    static_config: true
    self_registration: true
    import_ssh_config: true
    lan_active_scan: false          # 主动扫描默认关闭
  probe:
    basic: true
    runtime: true
    benchmark: false                # 性能探测单独启用
    auto_install: false             # 探测 ≠ 修复
  execution:
    enabled: true
    arbitrary_shell: false
    sandbox_required: true
  migration:
    stateless: true
    checkpointed: false
    side_effecting: false
  peer_transfer: { enabled: false }
  resources:
    observe: true
    affect_placement: false
    affect_solution: false
  security:
    approval_required_for_new_device: true
    mtls_required: true
    short_lived_credentials: true
```

**六种渐进模式**：

| 模式 | 能力 |
|---|---|
| OFF | 纯本地执行（默认） |
| INVENTORY | 只记录设备，不探测 |
| OBSERVE | 探测设备能力，不远程执行 |
| EXECUTE | 可以调度安全任务 |
| MIGRATE | 支持 Checkpoint 和故障迁移 |
| FEDERATE | 多控制域、集群或跨组织资源池 |

默认从 `INVENTORY → OBSERVE → EXECUTE` 逐步升级。

## 5. 架构：控制平面 + 设备平面 🔴

```
┌─────────────────────────────────────────────┐
│                Control Plane                │
│  Prompt / Task Graph → Placement Dry-run    │
│  Device Registry + Capability Registry      │
│  Scheduler + Policy Engine                  │
│  Lease / Secret / Artifact / Evidence       │
└───────────────────┬─────────────────────────┘
                    │ mTLS / Runner RPC / SSH Bootstrap
┌───────────────────▼─────────────────────────┐
│                Device Runner                │
│  Device Probe / Runtime Adapter             │
│  Sandbox Executor / Resource Monitor        │
│  Artifact Cache / Checkpoint Manager        │
│  Log / Evidence Collector                   │
└─────────────────────────────────────────────┘
```

控制平面：设备注册表、任务图、设备选择、短期权限、任务租约、状态、证据、
迁移与回滚协调。设备 Runner：上报能力、接收授权任务、准备环境、执行、
限制资源、采集日志、保存 Checkpoint、返回证据、清理临时资源。

## 6. SSH 的角色：Bootstrap Transport，不是常态执行通道 🟡

SSH 适合：首次设备接入、安装/升级 Runner、紧急诊断、临时执行、Runner
故障兜底。**不建议**长期设计成 `Scheduler → 拼接 Shell → SSH 执行 →
解析 stdout`——会很快遇到命令注入、环境不一致、状态无法恢复、任务取消
困难、子进程残留、日志非结构化、断线状态不明、权限粒度太粗。

更合适：设备通过 SSH 完成首次安装后，**Runner 主动建立出站连接**（对
NAT、动态 IP、内网设备友好，控制平面不需要主动扫描网络）：

```
SSH：Bootstrap Transport
Runner RPC：Normal Execution Transport
```

## 7. SSH 地址不是设备身份 🔴

IP、主机名、SSH Host Alias、局域网地址、公网端口都可能变化或被重新
分配，**都不能作为稳定设备身份**。身份应来自：设备生成的公私钥、证书
指纹、TPM/Secure Enclave 密钥、注册时生成的稳定 Device ID。

```typescript
interface DeviceIdentity {
  deviceId: string;
  publicKeyFingerprint: string;
  certificateId?: string;
  enrolledAt: string;
  enrolledBy: string;
  trustDomain: string;
  trustLevel: number;
}
```

SSH 配置只作为定位信息（`locator: { transport: ssh, host_alias: ai-node-1 }`）。

## 8. 设备能力模型：静态能力 + 动态状态 🟡

```typescript
interface DeviceNode {
  id: string;
  identity: DeviceIdentity;
  kind: "physical_device" | "virtual_machine" | "cluster" | "model_endpoint" | "cloud_batch";
  labels: Record<string, string>;
  transports: { type: "local" | "ssh" | "runner_rpc" | "kubernetes"; endpoint?: string }[];
  staticCapabilities: {
    os: string; architecture: string;
    cpuCores: number; totalMemoryBytes: number;
    accelerators: AcceleratorCapability[];
    runtimes: RuntimeCapability[];
    executionAdapters: string[];
  };
  dynamicState: {
    status: "online" | "busy" | "draining" | "offline" | "stale";
    availableCpuCores: number;
    availableMemoryBytes: number;
    availableAcceleratorMemory?: number;
    load: number; temperature?: number;
    batteryPercent?: number; pluggedIn?: boolean;
    runningTasks: number; lastSeenAt: string;
  };
  trust: { level: number; zone: string; allowedDataClasses: string[]; maxEffectClass: EffectClass };
  protocolVersion: string;
  capabilityFingerprint: string;
  capabilityValidUntil: string;    // 能力上报必须有有效期
}
```

三天前上报"CUDA 可用"，不能永远认为仍然可用——驱动升级、系统更新、
硬件异常都可能改变能力。

## 9. 探测分级 P0-P4（探测与修复分离）🔴

| 级别 | 内容 |
|---|---|
| P0 注册信息 | 设备身份、OS、CPU 架构、基础内存、Runner 版本 |
| P1 运行时 | Docker / Node.js / Python / Go / Java / CUDA / ROCm / Metal / Vulkan / 模型服务 / 可用端口 |
| P2 能力验证 | 编译最小程序、加载小模型、执行 GPU Kernel、启动临时容器、文件读写测试（避免"nvidia-smi 存在但 CUDA 实际跑不了"） |
| P3 性能探测 | CPU 基准、磁盘吞吐、GPU 推理速度、网络带宽、首 Token 延迟、模型吞吐（占用资源，单独启用） |
| P4 持续遥测 | 负载、温度、可用内存、队列、网络、电源状态 |

**探测和修复必须分开**：发现设备没有 Docker，不代表探针可以自动安装
Docker（`probe.auto_install: false`）。自动修改设备属于另一种高权限能力。

## 10. TaskNode 必须加入设备放置约束 🔴

```typescript
interface TaskPlacement {
  requiredCapabilities: string[];
  preferredCapabilities: string[];
  requiredOs?: string[];
  requiredArchitecture?: string[];
  resources: {
    cpuCores?: number; memoryBytes?: number;
    acceleratorType?: string; acceleratorMemoryBytes?: number;
    diskBytes?: number;
  };
  data: {
    inputArtifacts: ArtifactRef[];
    sensitivity: "public" | "internal" | "secret";
    preferredLocations?: string[];
    allowTransfer: boolean;
  };
  execution: {
    effectClass: EffectClass;
    mobility: "stateless" | "restartable" | "checkpointable" | "pinned";
    sandboxRequired: boolean;
    containerImage?: string;
    runtimeLock?: string;
  };
  affinity?: string[];        // 尽量共址
  antiAffinity?: string[];    // 必须隔离
  deadline?: string;
  targetHint?: string;        // 建议（不是硬约束）
  targetRequired?: string;    // 硬约束：必须在 macOS 完成 iOS 构建
}
```

## 11. 先进行设备放置 Dry-run 🟡（仓库：campaign --dry-run 为雏形）

与 Prompt Dry-run 同一思想，三者串起来：

```
Prompt Dry-run：判断怎么做
Placement Dry-run：判断在哪里做
Execution Dry-run：验证能不能做
```

```yaml
task: build-project
candidate_devices:
  - device: linux-server-1
    feasible: true
    reasons: [已安装 Node.js 22, 有足够内存, 项目缓存已存在]
  - device: mac-mini-m4
    feasible: true
    warnings: [需要传输 2.1 GB 依赖缓存]
  - device: raspberry-pi
    feasible: false
    reasons: [内存不足, 架构不兼容]
selected: { device: linux-server-1 }
estimated:
  artifact_transfer: 40 MB
  execution_time_p50: 90s
  execution_time_p90: 180s
risks: [linux-server-1 最近一次能力探测距今 18 小时]
fallback: [mac-mini-m4]
```

## 12. 设备选择：字典序过滤，不是单一总分 🟡

| 层 | 判据 |
|---|---|
| 第一层 硬过滤 | 权限、信任等级、数据安全、运行环境、CPU/内存/GPU、副作用等级、设备可用状态、任务截止时间 → 可行集合 D_f |
| 第二层 质量与可靠性 | 运行时兼容性、历史成功率、证据可信度、环境稳定性、可回滚能力 |
| 第三层 数据与模型局部性 | 输入数据是否已存在、模型权重/容器镜像/代码依赖是否已缓存（大型模型场景，冷启动与权重传输可能比执行本身更贵） |
| 第四层 调度效率 | 队列长度、关键路径、资源空闲、跨设备通信、上下文复用 |
| 第五层 可选资源优化 | 仅开关开启后：Token、金钱、能耗 |

## 13. 资源在设备调度中的位置

默认不变：`resources: { observe: true, affect_placement: false, affect_solution: false }`。
记录每台设备的运行时间/Token/模型调用成本/网络传输量，**但不能因为便宜
就把高质量任务交给不满足质量要求的设备或模型**。

- `affect_placement: true`：只在**同等质量设备之间**选（当前负载、传输
  时间、能耗、本地模型是否已加载）
- `affect_solution: true`：成本参与技术方案或模型路由，**仍须满足质量底线**

## 14. 跨设备通信成本与亲和/反亲和 🔴

两个节点可以并行，不代表放两台设备一定更快——A 产生 20GB 输出、B 立即
消费时，共址反而更快：

```
CommunicationCost(i,j) = DataSize_ij / Bandwidth + Latency × InteractionRounds
```

调度器同时决定：哪些节点并行、哪些共址、哪些必须分开、哪些设备上运行。

```yaml
affinity: [task:data-preprocess, task:model-inference]   # 大量数据交换 → 共址
anti_affinity: [implementation-device]                    # 验证与实现隔离，降低相关错误
```

## 15. 资源预约必须是原子的：Lease + Fencing Token 🔴

不能出现"调度器看到 GPU 8GB 空闲，同时把两个各需 6GB 的任务都调度过去"。
先预约：检查容量 → 创建 Reservation → 发放 Lease → 准备输入 → 启动任务。

```typescript
interface ExecutionLease {
  leaseId: string;
  taskNodeId: string;
  attemptId: string;
  deviceId: string;
  worldVersion: number;
  leaseEpoch: number;
  fencingToken: number;      // 关键：防止陈旧写入者提交过期结果
  reservedResources: { cpuCores: number; memoryBytes: number; acceleratorMemoryBytes?: number };
  acquiredAt: string;
  expiresAt: string;
  status: "reserved" | "running" | "released" | "expired" | "revoked";
}
```

**fencingToken 非常重要**：设备断网后旧任务可能仍在运行，控制平面可能
已把任务迁移到新设备；旧设备恢复连接后**不能提交过期结果**——只有持有
最新 fencing token 的执行尝试才允许提交状态。

## 16. "像单机一样流转"必须建立在外部化状态之上 🔴

迁移的前提：**计算可以移动，状态不能只存在于设备进程内**。需要外部化：
任务状态、Agent 会话状态、计划图、事件日志、代码版本、输入输出 Artifact、
模型上下文摘要、Checkpoint、验证证据。

```
Agent Session 1001
  ├── Attempt 1：AMD Node，运行到 40%，设备离线
  └── Attempt 2：Linux Server，从 Checkpoint 7 恢复
```

逻辑会话 `agent_session_id` 不变；变化的是 `execution_attempt_id`、
`device_id`、`lease_epoch`、`checkpoint_ref`。

## 17. 任务必须声明迁移语义 🔴

| 语义 | 含义 | 示例 |
|---|---|---|
| Stateless | 无重要本地状态，可重新执行 | 静态分析、只读搜索、生成候选计划 |
| Restartable | 不能从中间继续，但可从头重启 | 普通编译、单元测试 |
| Checkpointable | 可保存中间状态并迁移 | 长数据处理、模型训练、批量文档索引、长任务工作流 |
| Pinned | 必须在原设备完成 | 本地硬件加密密钥、不可移动的生产数据库、USB 设备、macOS 签名 |

对 LLM 生成：通常不能保证从另一台设备无损继续同一 Token 流。更现实的
做法是保存**完整上下文、已完成步骤、工具调用结果、结构化中间状态**，
从最近逻辑 Checkpoint **重新规划**，而不是尝试恢复模型内部隐藏状态。

## 18. 不要追求虚假的 Exactly Once 🟡（仓库：失败签名 + 重试为雏形）

设备断线后控制平面很难立即知道：任务完全没执行 / 执行了一半 / 已执行但
结果没上报 / 已成功并产生副作用。更可靠的设计：

```
At-least-once 调度 + 幂等键 + 任务租约 + Fencing Token + 副作用记录 + 事后对账
```

示例：发送通知，`idempotency_key = task_id + notification_type + recipient`，
重复执行时通知服务据此拒绝重复发送。**无法幂等的不可逆操作**：不能自动
重试、不能投机复制、不能自动迁移——必须进入人工或专用对账流程。

## 19. 设备状态机与执行尝试状态机

```
设备：UNREGISTERED → ENROLLED → PROBING → READY → RESERVED → BUSY
      → DRAINING → OFFLINE / STALE / QUARANTINED

执行尝试：CREATED → PLACED → RESERVED → STAGING → RUNNING → VERIFYING
          → SUCCEEDED / FAILED / LOST → CLEANUP
```

**LOST ≠ FAILED**：FAILED 是明确知道失败；LOST 是设备失联、不知道实际
结果。对 LOST 任务的处理方式取决于副作用类型（见 §24）。

## 20. 零信任安全模型 🔴

设备被授权加入资源池 ≠ 可以访问所有数据和工具。要求：设备身份 +
任务级 Capability Grant + 短期证书 + 最小权限 + 沙箱 + 完整审计。

```typescript
interface CapabilityGrant {
  grantId: string;
  taskNodeId: string;
  deviceId: string;
  allowedActions: string[];
  allowedResources: string[];
  allowedDataClasses: string[];
  maxEffectClass: EffectClass;
  issuedAt: string;
  expiresAt: string;
}
```

前端测试任务可以：读取指定代码快照、启动 Chromium、写入临时 Artifact、
上传测试结果。不能：读取 SSH 私钥、访问生产数据库、读取其他项目、
修改系统配置。

**不可信设备不能接触敏感数据**。信任区：`trusted-local / trusted-datacenter /
trusted-cloud / personal-interactive / untrusted-external`；数据分级：
`public / internal / confidential / secret / regulated`。调度必须满足
`DeviceTrust(d) ≥ DataTrustRequirement(t)`。**沙箱只能限制任务对设备的
影响，不能阻止设备所有者读取任务内容**——不可信设备只能运行公开、可
验证任务。

## 21. SSH 密钥不能随任务分发 🔴

SSH 私钥保存在 Secret Broker / HSM / 系统 Keychain / SSH Agent；任务只
获得**短期授权引用**。导入 `~/.ssh/config` 时只读 Host/HostName/Port/
User/ProxyJump/IdentityFile 引用，**不把私钥复制进数据库**。

设备接入流程：读取 SSH 配置 → 显示待接入设备 → 用户审批 → SSH 安装
Runner → Runner 生成自己的设备密钥 → 控制平面签发设备证书 → 后续 mTLS。

## 22. 设备主动注册优于网络主动扫描

局域网主动扫描有安全/隐私/误识别/网络告警/企业策略冲突风险。默认推荐：
静态配置 + SSH Config 导入 + 设备 Runner 主动注册。`lan_active_scan: false`
单独开关，探测范围明确，不默认扫描所有端口。

## 23. 运行环境必须可复现 🟡（仓库：fingerprint 为雏形）

远程执行最常见问题：本机能跑、远程缺依赖、版本不同、驱动不一致。每次
任务绑定：代码 Commit、依赖锁文件、容器镜像 Digest、模型版本、Runner
版本、工具链版本、输入 Artifact Hash、环境 Fingerprint。

```typescript
interface ExecutionEvidence {
  taskNodeId: string;
  attemptId: string;
  deviceId: string;
  codeDigest: string;
  environmentDigest: string;
  inputDigest: string;
  outputDigest: string;
  runnerVersion: string;
  toolVersions: Record<string, string>;
  result: "passed" | "failed" | "inconclusive";
}
```

否则"远程设备测试通过"可能测试的是旧代码、旧依赖或另一份数据。

## 24. Artifact 使用内容寻址，不依赖共享目录

分布式文件系统/SSHFS/共享目录会引入一致性、锁、延迟、断线、缓存污染、
权限、跨平台语义问题。更适合：Git Commit / Workspace Snapshot +
**Content-Addressed Artifact Store**（`sha256:abc123...`）。设备只下载
缺少的内容：去重、断点续传、完整性校验、缓存复用、版本追踪、垃圾回收。
rsync/SFTP/HTTP/P2P 只是传输方式，**Artifact Hash 才是身份**。

## 25. 对 AI 计算需要额外建模 🔴

```typescript
interface ModelCapability {
  modelId: string;
  backend: "cuda" | "rocm" | "metal" | "vulkan" | "cpu";
  formats: string[];
  quantizations: string[];
  maxContextTokens: number;
  modelCached: boolean;
  modelLoaded: boolean;
  estimatedTokensPerSecond?: number;
  estimatedFirstTokenLatencyMs?: number;
}
```

两种调用方式：

- **方式 A 移动任务**：把 Agent 执行环境和上下文发送到设备运行
- **方式 B 调用远程模型能力**：Agent 仍在控制平面，只调用设备暴露的
  OpenAI-compatible API——很多情况下**更简单、更安全**（AMD 设备长期
  暴露 Qwen 推理/Embedding/Reranker 服务，其他设备无需复制整个项目）

## 26. 设备标签与角色（不硬编码设备名）🟡

```yaml
devices:
  amd-ai-node:
    labels: { workload.memory_heavy: "true", ai.backend.vulkan: "true", ai.large_model: "true" }
  rtx-node:
    labels: { ai.backend.cuda: "true", workload.image_generation: "true" }
  mac-mini:
    labels: { os.macos: "true", build.apple: "true", ai.backend.metal: "true" }
  macbook:
    labels: { interactive_device: "true", schedule_policy: "idle_only" }
  raspberry-pi:
    labels: { control_plane: "true", always_on: "true", heavy_compute: "false" }
```

调度器匹配标签，不把设备名称硬编码进工作流。

## 27. 网络拓扑感知

资源图边记录：同主机 / 同局域网 / VPN / WAN / 中继；估算带宽与延迟；
是否计费网络；是否加密。

```typescript
interface NetworkLink {
  fromDeviceId: string;
  toDeviceId: string;
  topology: "same_host" | "same_lan" | "vpn" | "wan" | "relay";
  estimatedBandwidthMbps: number;
  estimatedLatencyMs: number;
  metered: boolean;
  encrypted: boolean;
  observedAt: string;
}
```

调度器据此判断：任务并行收益是否大于跨设备传输成本。

## 28. 时钟纪律 🔴

多设备时钟存在偏差：超时和持续时间用**单调时钟**；跨设备事件排序依赖
任务事件序号、Lease Epoch、Fencing Token、Lamport Clock / Hybrid Logical
Clock，**不简单比较两台设备的本地时间戳**。系统时间仍需 NTP 同步，但
正确性不能建立在"所有机器时钟绝对一致"上。

## 29. 设备断线处理按任务类型区分 🔴

| 任务类型 | 设备失联处理 |
|---|---|
| 纯计算 | Lease 过期后可重新执行 |
| 只读任务 | 可以在其他节点重试 |
| 幂等写入 | 使用相同幂等键重试 |
| Checkpoint 任务 | 从最后 Checkpoint 恢复 |
| 可补偿任务 | 对账后重试或补偿 |
| 不可逆任务 | 暂停，等待状态确认 |
| 外部副作用不明确 | 标记 LOST，进入人工或专用对账 |

设备重连后**不直接恢复所有旧任务**，而是：上报当前运行任务 → 与控制
平面状态对账 → 检查 Lease Epoch → 终止过期任务 → 上传可接受的
Checkpoint。

## 30. Cordon 和 Drain 🟡（对笔记本尤其重要）

- **Cordon**：不再接收新任务，现有任务继续运行
- **Drain**：停止接收新任务，尽量迁移或结束现有任务

```
READY → CORDONED → DRAINING → MAINTENANCE
```

用户开始使用电脑时，设备自动进入 `interactive / cordoned`，避免 Agent
后台任务抢占全部 CPU/GPU/内存。

## 31. 协议版本协商与滚动升级 🔴

```yaml
runner:
  protocol_version: 2.3
  capabilities: [task.v1, artifact.cas.v2, checkpoint.v1, sandbox.oci.v1]
```

不能默认所有设备必须同时升级。支持：滚动升级、向后兼容、能力降级、
Runner 隔离。

## 32. 背压与准入控制 🟡（仓库：ratelimit.py 为直接基础）

不能因为有很多 TaskNode 就一次性全部推给设备：

```yaml
limits:
  device: { max_running_tasks: 4 }
  capabilities:
    model.large: { max_concurrency: 1 }
    browser.test: { max_concurrency: 3 }
```

资源超卖策略：CPU 允许有限 Overcommit；**内存严格预约；GPU 显存严格
预约**；磁盘保留安全水位；网络限制传输并发。

## 33. 投机执行限安全任务 🔴

把同一任务同时派到两台设备、先完成者胜出（Speculative Execution），
只适合：纯计算、只读、沙箱任务、结果可比较、无外部副作用（两设备同时
编译、两个模型同时出候选方案、多环境同时跑测试）。不适合：发邮件、
写生产数据库、部署服务、创建外部订单、付款。

```yaml
speculative_execution:
  enabled: false
  allowed_effects: [pure, read_only, sandboxed]
```

## 34. 保留手工干预能力

自动调度之外：固定到指定设备（`target_required`）、排除设备（`avoid`）、
优先类别、维护状态、取消/迁移/重试任务、查看设备选择原因。自动系统必须
能解释：为什么选这台、为什么没选另一台、哪些硬约束淘汰了某设备、成本
开关是否参与了选择。

## 35. 渐进实施路线（对本仓库的映射）

| Phase | 内容 | 仓库落点 |
|---|---|---|
| Phase 0 | **抽象本地执行器**：所有工具调用统一经过 ExecutionTarget / ExecutionAttempt / ArtifactRef / ExecutionResult；LocalExecutor 只是第一个适配器 | `pbatch/runner.py` 现在做，最小侵入 |
| Phase 1 | 静态 SSH Inventory：手工配置、导入 SSH Config、只读基础探测、设备标签、状态页面、Placement Dry-run；不迁移、不扫描、不高风险执行 | `pi-batch devices` 子命令 + `device_fabric.yaml` |
| Phase 2 | SSH 远程执行 MVP：指定设备执行、工作目录隔离、超时、取消、日志返回、Artifact 上传下载、代码 Hash、环境 Fingerprint；只允许结构化 TaskSpec，**不允许随意拼接 Shell** | runner 增加 SshExecutionTarget |
| Phase 3 | 安装 Device Runner：设备主动注册、mTLS、心跳、动态资源上报、资源预约、任务 Lease、结构化日志、短期 Capability Grant；SSH 退回接入/诊断角色 | 独立 `device-runner` 进程 |
| Phase 4 | 设备调度：能力匹配、数据局部性、模型缓存感知、关键路径、串并行、亲和/反亲和、Cordon/Drain | 调度器扩展 |
| Phase 5 | 故障恢复与迁移：Checkpoint、Lease Epoch、Fencing Token、断线对账、任务迁移、幂等和补偿 | 对账服务 |
| Phase 6 | 联邦与高级调度：P2P Artifact、多控制平面、跨区域资源池、K8s/Nomad/Slurm Adapter、Gang Scheduling、多租户配额、远程 Attestation | **最后才做** |

## 36. 现在必须预留的数据模型

即使暂时只实现本机执行，现在就把这些概念引入代码：

```
ExecutionTarget / ExecutionAttempt / DeviceId / ArtifactRef
EnvironmentDigest / PlacementConstraint / EffectClass / MobilityClass
ExecutionLease / CheckpointRef
```

否则将来所有代码里会隐含"路径一定是本机路径、命令一定在本机执行、
状态一定在本机内存、文件一定可直接访问、localhost 一定是目标服务"，
等设备织网加入时就需要大规模重构。**正确策略：现在完成抽象隔离，
之后再逐步实现远程能力。**

## 37. 使用更通用的 ExecutionTarget，而不是只叫 Device

未来目标不仅是单台设备：本机、远程物理机、虚拟机、Docker Host、
Kubernetes Cluster、云端 Batch、远程模型 Endpoint、浏览器农场、CI
Runner。顶层接口：

```typescript
interface ExecutionTarget {
  id: string;
  kind: "local" | "device" | "cluster" | "service" | "cloud_batch";
  capabilities: string[];
  probe(): Promise<TargetSnapshot>;
  reserve(request: ResourceRequest): Promise<ExecutionLease>;
  execute(spec: ExecutionSpec, lease: ExecutionLease): Promise<ExecutionHandle>;
  cancel(attemptId: string): Promise<void>;
  collectEvidence(attemptId: string): Promise<ExecutionEvidence>;
}
```

SSH 设备只是一个 Adapter（`SshExecutionTarget`）；将来还有
`LocalExecutionTarget / RunnerExecutionTarget / KubernetesExecutionTarget /
ModelEndpointTarget`。

## 38. 必须避免的反模式

| 反模式 | 后果 |
|---|---|
| 一个 enable_remote=true 开启全部能力 | 权限边界失控 |
| IP 或主机名作为设备身份 | 地址变化后身份混乱 |
| 自动扫描并登录所有设备 | 安全和隐私风险 |
| SSH 拼接任意 Shell | 注入、难审计、难恢复 |
| 设备自报有 GPU 就直接相信 | 能力可能不可用或已过期 |
| 任务状态只保存在执行设备 | 设备离线后无法恢复 |
| 多设备共享一个可变工作目录 | 冲突和一致性问题 |
| 所有任务都允许自动重试 | 重复副作用 |
| 认为网络执行和本机一样可靠 | 分布式失败无法处理 |
| 成本低就选择弱设备或弱模型 | 质量被资源指标劫持 |
| 所有 Agent 永久绑定一台机器 | 无法动态迁移和负载均衡 |
| 直接建设 P2P Mesh | 初期复杂度远超价值 |
| 探测发现缺依赖后自动安装 | 只读探测意外变成高权限修改 |

## 39. 最终规范

### MUST

- 设备感知必须是可关闭的独立执行平面。
- 默认关闭主动扫描、远程执行、迁移和自动安装。
- 逻辑 Agent、任务节点、执行尝试和物理设备必须分离。
- 设备必须使用密码学身份，不能依赖 IP 和主机名。
- 设备能力必须带版本、证据和有效期。
- 每次远程执行必须绑定代码、环境和输入 Hash。
- 远程副作用必须具备幂等、Lease、补偿或人工对账机制。
- 任务状态和关键 Checkpoint 不能只存储在执行设备上。
- 不可信设备不能获得秘密和敏感数据。
- SSH 应主要作为接入、引导和诊断通道。
- 所有远程执行必须经过 Placement Dry-run 和权限授权。
- 成本默认不得改变技术正确性和质量底线。

### SHOULD

- 应该使用设备主动注册，而不是默认主动扫描网络。
- 应该使用内容寻址 Artifact，而不是依赖共享目录。
- 应该对模型缓存、数据局部性和网络拓扑进行感知。
- 应该为交互设备提供 idle-only、cordon 和 drain。
- 应该将设备探测、设备修复和软件安装分成不同权限。
- 应该对高风险结果进行跨设备或独立环境验证。
- 应该使用 Fencing Token 阻止失联旧任务提交过期结果。
- 应该允许根据证据动态重新放置任务。

### MAY

- 可以对纯计算任务进行投机执行。
- 可以将设备、集群和远程能力统一抽象为 ExecutionTarget。
- 可以通过资源开关让时间、Token、金钱和能耗参与同等质量目标之间的放置。
- 可以为长任务增加 Checkpoint 和迁移。
- 可以在成熟后支持 P2P 数据传输和多控制平面联邦。

## 40. 最终表达

```
DeviceFabric = Identity + CapabilityDiscovery + ResourceGraph + PolicyPlacement
             + LeasedExecution + ExternalizedState + Evidence + Recovery
```

> **把远程设备看成不稳定、异构、可能失联的执行资源，而不是把 SSH 主机
> 伪装成本机。上层获得单机般的调用体验，底层保留完整的分布式系统语义。**

## 41. 补充要点（本文档新增，二十年工程视角）

### 41.1 设备失联演练（Game Day / Chaos Drill）🔴

恢复路径（断线对账、迁移、fencing 失效、幂等重试）**必须被定期测试**，
否则它们只是理论。把"设备失联 + 重连 + 对账"做成可编排的演练任务
（对应 `AADM_GOVERNANCE.md` §29 未知的未知）——在生产设备池中定期
故意断网/杀 Runner/延迟心跳，验证恢复不依赖"运气"。

### 41.2 设备生命周期闭环：退役与密钥撤销 🔴

文档有 ENROLLED→OFFLINE/QUARANTINED，但缺 **RETIRED（退役）**：
设备退役时必须吊销证书、撤销密钥、从注册表移除、清理其 Artifact 缓存、
保留审计记录。密钥轮换与吊销列表（CRL）是长期运行的前提——"只增加、
不删除"同样适用于设备。

### 41.3 Checkpoint 必须先验证再恢复 🔴

恢复前校验 Checkpoint 哈希与完整性（防损坏/篡改）；恢复后先运行自检
（环境 digest、依赖版本、状态一致性）再继续执行。否则"从 Checkpoint
恢复"可能把损坏状态当成正确状态，比从头重跑更糟。

### 41.4 成本归属与设备侧预算上限 ✅（仓库：budget-cap 提交即预留 + metering.py 为基础）

远程执行把 token/算力成本**移到设备侧**：计量必须按 task 幂等归账
（对应 `AADM_GOVERNANCE.md` §35.12 计量精度），并设 **per-device
预算 cap**——防止一个失控任务烧掉整台设备的配额（本地模型推理、
GPU 占用、带宽都是钱）。预算超限在调度前拒绝，不是事后追责。

### 41.5 与现有机制复用，而不是重新发明 🟡

- 远程模型 Endpoint 直接接入 pi-batch **现有 provider 配置**与
  `ratelimit.py` 令牌桶（F 线限速逻辑原样适用）
- `campaign` fingerprint 增加 **device 维度**：设备变化 → 复用失效
- `.pi/settings.json` 的 `httpIdleTimeoutMs`（当前 900s）在 WAN/慢设备
  场景必须可调——远程执行超时参数要按拓扑分层

### 41.6 控制平面单点：定义明确语义，不上 quorum 🔴

单控制平面是可接受起点，但必须定义**控制平面宕机时设备 Runner 的行为**：
任务继续跑完、证据本地缓存、恢复后回传与对账。这比一开始就上
quorum/多控制平面（Phase 6）成本低得多，且把"控制平面挂了"从灾难
降级为可恢复事件。

### 41.7 远程验证 = 独立证据的免费落地 ✅（仓库：meta 对抗审查为雏形）

`anti_affinity` 把**实现与验证放不同设备/不同环境**，直接实现
`AADM_GOVERNANCE.md` §12 的 Diversity（不同模型、不同环境、不同 Oracle
降低相关错误）。这应该是 device fabric 的**默认策略**而不是可选优化：
同一设备上"实现 + 验证"的通过，仍是相关错误。

### 41.8 网络传输预算作为资源账本第一公民 🔴

计费网络（手机热点、蜂窝、按量计费 VPS）下，**传输量本身是成本**。
资源账本记录每次 Artifact 传输的字节与费用；Placement 估计包含
artifact transfer 的 p50/p90；超限告警或阻止（对应 `AADM_RUNTIME.md`
§16 成本公式的 `ToolComputeCost` 扩展）。

## 42. 与仓库机制的映射与落地批次

| DeviceFabric 概念 | 仓库现有实现 | 差距 |
|---|---|---|
| ExecutionTarget 抽象 | `pbatch/fabric.py`：ExecutionSpec/Evidence/Attempt + LocalExecutionTarget（进程组、硬超时、输出上限、digest）；`pi-batch devices` 子命令 | ✅ D0 完成；runner.py 执行路径保持零回归，远程路由留待 D2 |
| 准入控制/背压 | `ratelimit.py` 令牌桶 + spawn 前排队 + jitter 重试；`serve --device-quota` per-device 并发上限（超限拒绝/完成后释放） | ✅ 差值表项已闭合 |
| 资源账本 | `metering.py`（session 增量计量、inode/offset 折叠） | 🟡 需加 device 维度与幂等归账 |
| 环境指纹 | campaign fingerprint（模块/快照/pipeline/角色/model/provider） | 🟡 需加 device 维度与 digest 校验 |
| 外部化状态 | memory JSONL + session 索引 + campaign state.jsonl（追加式） | ✅ 基础已具备；需 checkpoint 语义 |
| 失败处理 | 失败签名拒绝、指数退避重试、硬超时 SIGKILL | 🟡 需 LOST 语义（设备失联 ≠ 失败）与对账 |
| 证据 | evidence.py + completion 门禁 + 验证器注册表 | 🟡 需 ExecutionEvidence（digest 绑定） |
| 独立验证 | meta 角色并发 + gate 独立裁决 | ✅ 跨设备验证是自然延伸 |
| 审计 | 决策日志 append-only + events.jsonl（actor/attempt=租约编号） | ✅ 差值表项已闭合 |
| 设备身份/信任区/能力授权 | 无 | 🔴 全新 |
| 设备状态机/Cordon/Drain | 无 | 🔴 全新 |
| 时钟纪律 | `devices clock-skew`：last_seen 与本机时钟偏差（fresh/stale 分类告警） | ✅ 差值表项已闭合 |

**落地批次（对仓库）**：

| 批次 | 内容 | 验收 |
|---|---|---|
| D0（✅ 已完成） | `ExecutionTarget/ExecutionAttempt/ArtifactRef/ExecutionEvidence` 抽象（`pbatch/fabric.py`）；`device_fabric: {enabled: false}` 顶层配置；`pi-batch devices list|probe` 子命令（INVENTORY 模式，P0/P1 只读探测）；17 个单测 | 本机行为零回归（pytest 566 全绿、quality 严格模式 OK、eval 40 通过）；抽象层有单测 |
| D1（✅ 已完成） | SSH inventory（`~/.ssh/config` 只读解析 + 静态配置）；SSH 远程只读探测（固定命令集、BatchMode、不可达也返回报告）；探测状态缓存（`.pi-batch/devices/state.jsonl` 追加式）；Placement Dry-run（OS/架构/内存/能力/副作用等级硬过滤 + 淘汰原因 + stale 警告）；`devices ssh-hosts/placement` 子命令 | 模块拆分为 fabric/fabric_devices/fabric_cli（均 < 500 行）；真实 SSH 探测验证（Manjaro 节点在线）；36 个 fabric 测试全绿 |
| D2（✅ 已完成） | SSH 受控远程执行 MVP：`SshExecutionTarget`（与本地同一证据格式）；结构化 argv（shlex.quote 防注入）；副作用等级上限 sandboxed；硬超时 SIGKILL + 可取消（进程登记表）；scp Artifact 上传/下载；执行前三重门禁（EXECUTE 模式 / 探测证据有效期 / Placement Dry-run）；`devices run NAME -- CMD` | 模块拆为 fabric/fabric_devices/fabric_exec/fabric_ssh（均 < 500 行）；真实 SSH 执行验证（Manjaro 节点 `python3 --version` → passed + digest 证据）；49 个 fabric 测试全绿 |
| D3（✅ 已完成） | Device Runner（`pi-batch devices runner`，主动出站连接）；控制平面（`serve`：注册 TOFU+审批/心跳/租约/证据/对账，追加式 JSONL 持久化）；HMAC-SHA256 签名通道（时间窗 + nonce 防重放）；Lease + Epoch + **Fencing Token**（陈旧 token 拒收证据 = 防 split-brain）；随租约签发最小 Capability Grant；断线指数退避重连；`approve`/`runners` 管理命令 | 模块 fabric_rpc/fabric_server/fabric_runner（均 < 500 行）；回环全流程测试 + **控制平面重启对账演练**（旧 fencing token 失效）17 个；真实 CLI 注册演示通过 |
| D4（✅ 已完成） | 任务调度与 Runner 执行路由：控制平面任务注册表（queued/leased/running/done/failed/cancelled/lost）；Runner 轮询认领 + 本地执行 + 证据回传（fencing token）；任务按 task_id 幂等；**失联设备 stateless/restartable 任务自动重排队**（其他 Runner 认领，pinned/checkpointable 标 lost）；`schedule`（--device 或按约束自动选设备，Cordon 感知）/`task`/`migrate`/`cordon`/`drain` 命令；调度器（关键路径/就绪集/负载/局部性） | 模块 fabric_sched/fabric_cmds（均 < 500 行）；真实 E2E：schedule → runner 执行 → task done + 输出回传；双 Runner 竞争由 fencing 拒收败者（at-least-once）；干净副本 621 测试全绿 |
| D5（✅ 已完成） | 任务迁移（`devices migrate`：重新排队 + 新租约，**旧 fencing token 立即失效**）；任务取消（queued/leased）；Cordon/Drain（`devices cordon/drain [--undo]`）；证据幂等（重复提交 accepted+duplicate）；**Checkpoint 断点续传**（schedule --checkpoint-save/--load：控制平面存取有界检查点，Runner 执行前恢复/执行后回传，迁移后新设备续传） | 迁移后旧 token 证据拒收有测试实证；取消后 Runner 不执行有测试实证；检查点往返/恢复/回传/超限拒绝均有测试实证 |
| D5+（✅ 已完成） | **投机执行**（schedule --speculative --devices a,b：同任务双设备竞跑先完成者胜，败者取消，只限安全副作用）；**设备侧预算上限**（serve --budget-cap：提交即预留，超限拒绝——防失控任务烧配额）；**交互事件账本**（控制平面追加式 events.jsonl + `pi-batch events recent`——谁/何时/何事可审计） | 竞跑无失败任务、预算预留/超限拒绝、事件埋点/有界读取均有测试实证 |
| D6（✅ 最小件已实现；基础设施绑定项预留） | **多租户配额**（serve --tenant-quota "team-a=2"：进行中任务超限拒绝，完成后释放）；**跨区域资源池**（runner --region + schedule --region 过滤）；**集群目标**（device_fabric.clusters + schedule --cluster 集群内选设备）；**Gang 原子提交**（schedule --gang --devices a,b：任一失败全部取消 all-or-nothing）；**注册证明字段**（runner --attestation，注册/status 记录） | 7 个联邦测试：配额限制/释放/租户隔离、区域元数据/过滤、集群解析/选设备、Gang 回滚/全接受 |
| D6+（⏸ 预留） | P2P Artifact 直传（NAT 穿越）、多控制平面转发、K8s/Nomad/Slurm Adapter、Gang Scheduling 全量语义、远程 Attestation 硬件证明 | 需外部基础设施（集群/kubectl/TPM）；`ExecutionTarget` 扩展点已留，`--control` 已支持任意控制平面地址 |
