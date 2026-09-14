# NVIDIA GPU 计算

在已有 [计算配置](COMPUTE.md) 上，Hub 可以按卡数及每卡最低可用显存选择
NVIDIA CUDA 物理 GPU。CLI、TUI 和 Flutter Console 共用同一 API；当前轮次的
Agent 调度工具也接受 GPU 需求。客户端查看的是各授权实例的设备资源与实际分配。

## 使用

升级 Hub、Fabric 控制面和计算 Runner。Runner 主机需要可用的 `nvidia-smi`，
且 GPU 必须关闭 MIG。原有设备审批、项目工作目录映射和任务权限继续生效。
预留按 Runner 设备身份管理；每张物理卡配置给一个 Runner，同主机的多个 Runner
使用完整 UUID 白名单划分互不重叠的可见设备。

```bash
python pi-batch.py fleet compute devices --project-id project-one
python pi-batch.py fleet compute submit --session-id SESSION \
  --argv-json '["python3","job.py"]' --cpu-cores 2 --memory-mib 512 \
  --gpu-count 2 --gpu-memory-mib 4096 --timeout 300 \
  --idempotency-key MY_STABLE_GPU_REQUEST_KEY
python pi-batch.py fleet compute status TASK
python pi-batch.py fleet compute tui
```

示例要求同一台 Runner 上有两张各至少空闲 4096 MiB 的卡。显存不能跨卡相加。
卡数范围 0–16，每卡显存范围 0–1 TiB；卡数为 0 时不得要求显存。
GPU 需求是选机和启动前检查条件，不是内存硬限额，也不会自动把程序改成多卡程序。
每卡显存为 0 仅声明需要卡，不保证有启动模型所需的空闲显存；运行模型时应填写
实际需要的最低容量。

TUI 和 Console 的计算表单提供卡数与每卡显存输入；设备目录显示 GPU 型号、
UUID、显存、预留状态，任务详情显示控制面确认的实际 UUID 分配。
CLI/API 的普通设备目录支持游标分页。Agent 工具每页最多四台设备、60 KiB，
用 `devices --after <上一页最后的 device_id>` 继续，直到返回空列表。
每次查询只获取该页授权实例的资源；工具只暴露当前会话项目。

## 发现、预留与执行

| 环节 | 行为 |
|---|---|
| Runner 探测 | 有界调用 `nvidia-smi`，最多等待 2 秒，输出最多 64 KiB；读取 UUID、型号、总/空闲显存和 MIG 状态 |
| 设备目录 | 最多 16 张物理卡；使用 GPU 自身的测量时间，超过 90 秒或未来偏差超过 5 秒不参与调度 |
| Hub 选机 | 同时满足租户、项目、实例权限、CPU/内存、GPU 卡数和逐卡显存条件；不足则保持 queued |
| Fabric 准入 | 在任务锁内再次检查空闲执行槽及 GPU，选择并持久化 UUID；调用方不能指定分配或伪造 UUID |
| Runner 执行 | 重新探测本机资源，检查分配及 fencing，给任务子进程设置完整 UUID 形式的 `CUDA_VISIBLE_DEVICES` |
| 结果与恢复 | 分配进入执行证据及摘要；重试和快照恢复保留原分配，运行中取消结束进程组后释放预留 |

控制面能力为 `hub.compute.gpu.v1`，Runner 能力为 `task.gpu.v1`。缺少能力、
探测失败、MIG 或过期资源都不能派发 GPU 任务；CPU 任务不要求 NVIDIA 工具。
不请求 GPU 时仍使用原 CPU 请求结构，保持既有幂等键及摘要兼容。

Runner 启动环境中的 `CUDA_VISIBLE_DEVICES` 可进一步收紧可用设备：未设置时
使用探测到的全部物理卡；空值或 `-1` 表示没有 GPU；完整 UUID 列表是白名单。
数字序号、缩写 UUID、MIG 标识不参与本轮调度，避免 CUDA 序号与管理工具序号
对应不确定。GPU 子进程只获得最终分配的完整 UUID 列表；远程 CPU 资源任务
的该环境变量为空，普通 CUDA 应用不会使用未申请的卡。普通本地执行仍沿用
原有环境行为；该设置不构成对恶意程序的隔离。

损坏或变化的 GPU 分配回执会触发远端取消，仍保留任务句柄供后续协调。
已完成任务若分配无法核验，会标记 failed 并清空不可信结果。
Fabric 重启沿用现有 `lost` 尝试语义；恢复任务快照和分配不代表自动重新执行。
相同提交键继续返回原任务，避免在无法确认副作用时重复计算。
Vault 配置存在时，归档的执行证据包含需求与分配，沿用固定版本归档校验。

## 实现边界

每个 Runner 仍同时执行一个任务，一次任务可使用多卡，多台 Runner 可并行。
GPU 预留只协调本控制面的任务；其他本机程序仍可能消耗显存。
`CUDA_VISIBLE_DEVICES` 是遵守 CUDA 环境的应用可见性设置，不能隔离恶意程序的
设备访问。AMD、Apple GPU、MIG 调度、容器设备隔离、GPU 显存硬限额尚未实现。
本轮不包含真实 CUDA 工作负载性能或跨主机部署验收。

持久化模型见 [GPU_DESIGN.md](GPU_DESIGN.md)，具体验证结果见
[GPU_VERIFICATION.md](GPU_VERIFICATION.md)。

探测字段与可见性语义依据 NVIDIA 官方文档：
[nvidia-smi](https://docs.nvidia.com/deploy/nvidia-smi/)、
[CUDA 环境变量](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html)、
[MIG](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/getting-started-with-mig.html)。
