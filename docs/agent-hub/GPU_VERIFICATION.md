# GPU 增量验证记录

验证日期：2026-09-13 UTC。范围为 Hub、Fabric、Runner、CLI/TUI 和 Flutter Console
的 NVIDIA 物理 GPU 计算路径。实现与使用边界见 [GPU.md](GPU.md)，持久化设计见
[GPU_DESIGN.md](GPU_DESIGN.md)。

Runner 在隔离工作树 `hub-gpu-core` 中验证，基线为 P3 的 `cdc09719`，合入 Fabric
提交 `b41c873b` 后执行联合检查。Console 基线为已有 P0–P2 暂存内容的快照
`3d019e10`，最终实现提交为 `5f2bf037`。

| 检查 | 结果 |
|---|---|
| Runner `make ci` | 通过：2,984 项回归（586.43 秒）、56 项门禁自测、53 项规则评估；8 个工程门禁、发布元数据、17 个组织节点、72 个能力与无环依赖图检查通过 |
| Runner `python quality.py --strict .` | 通过 |
| GPU/Hub/Fabric/TUI 联合专项 | 82 项通过，68.44 秒 |
| 最终 TUI 展示模块拆分复验 | GPU/TUI/恢复专项 53 项、打包 11 项、`make check` 8 个门禁及严格质量检查通过 |
| OpenAPI | 37 个原始组件通过 Draft4 元 schema 检查；8 个计算组件及真实 HubService DTO 样本通过实例校验 |
| Console Flutter 全量 | 基础 GPU 实现 `2fcb130` 的 1,285 项通过；后续 DTO 及 API 提取改动按下列定向组复验 |
| Console 最终 `flutter analyze --no-pub` | 通过 |
| Console 最终定向回归 | 8 文件、41 项 VM 测试通过；Chrome API/模型 19 项通过 |
| Console 最终 Web release / artifact / harness | 全部通过；invariants 12 项、产物检查 25 项 |
| Console 其他门禁 | 基础实现的 Python 24 项、Guard 83 项、Kubernetes 本地渲染通过；未改部署配置 |

完整 CI 在 GPU 功能整合后执行。随后提交钩子发现计算 TUI 超出 500 行预算，
将纯展示函数移至 `hub_compute_tui_views.py`（主文件 424 行，展示模块 98 行），
保留旧导入接口；对这次模块提取执行了上表中的最终定向回归、打包及工程门禁，
未再次重复全部 2,984 项。提取后的模块依赖图仍无环。

联合专项启动实际 Hub HTTP、带签名的 Fabric HTTP、Runner 循环和任务子进程；
仅将 `nvidia-smi` 替换为输出确定性 CSV 的本地可执行文件。端到端及兼容
测试确认：

- 两卡分配与子进程 `CUDA_VISIBLE_DEVICES`、已接受证据和摘要一致；相同提交键只执行一次。
- CPU 资源任务不会继承可见 GPU；没有资源声明的旧 Fabric 任务与普通本地执行保持原环境。
- 运行中取消结束进程组，不产生后续文件副作用；预留释放后可再提交两卡任务。
- GPU 提交回执丢失及控制面重启后，原 ID 和分配保持不变，任务按既有策略进入 `lost`；旧 Runner 缺少 GPU 能力时拒绝 GPU 请求，CPU 仍可提交。

其余专项覆盖逐卡显存、数量/类型/上限、过期和异常探测、MIG 模式、UUID 重复、
授权目标过滤、探测期间取消、执行前显存下降、伪造或变化的分配、Hub 重启恢复、
工具能力失效、设备分页、写权限回执脱敏和 TUI 短窗口交互。
坏分配回执仍保留远端句柄供取消，不能将不可信完成结果标为成功。

OpenAPI 样本包含 5 个有效资源边界、4 个非法边界、3 个零时间戳拒绝样本，
2 个设备 DTO、2 个任务 DTO、2 张 GPU 卡和 1 份分配；同时验证省略 GPU 与显式
零 GPU 在同一幂等键下得到同一 CPU 任务。实例校验仅将 OpenAPI 3.0 的 `nullable`
映射为 Draft4 类型联合；原有 5 处冗余 `required: []` 已删除，不改变接口含义。
逐卡可用显存不超过总量、时间先后、UUID 忽略大小写后唯一、分配与请求匹配及
测量新鲜度是跨字段或时态约束，由 Hub/Fabric 的运行时校验执行。

## 门禁收尾与工作区

首轮 Runner 前置知识门禁把局部显存解析变量 `amount` 和设备目录测试替身识别
成财务字段修改。解析变量改为准确的 `mebibytes`，测试说明明确设备观测替身与
ComputeStore 持久任务历史的职责；未改门禁或增加豁免。

Console 原有 API 文件超预算和测试命令字符串的 `print(...)` 误报已修复：响应
解析按已有 `part` 模式提取，测试保留参数边界及请求/证据断言并使用等价的
`sys.stdout.write`。最终全局 harness 通过，未放宽规则、修改阈值或改依赖锁文件。
Console 的详细分阶段结果见其 `docs/AGENT_GPU_COMPUTE.md`。

Console 主工作区的源码与最终提交逐文件一致，Web 产物也已同步。94 个文件、
44,938,360 字节均通过文件集合、长度及 SHA-256 比对；清单摘要为
`2c0e06cad5caad03769bb314b3e7b078ba908bf13d4164c76be4d07311cecd3a`。
Runner 合回时保留主工作区既有的 `pbatch/campaign_runtime.py` 修改。

本地日志：`/tmp/hub-gpu-combined-tests.log`、`/tmp/hub-gpu-runner-ci.log`、
`/tmp/hub-gpu-runner-quality.log`、`/tmp/hub_gpu_openapi_audit.log`；OpenAPI 检查脚本为
`/tmp/hub_gpu_openapi_audit.py`，Web 哈希清单为 `/tmp/hub-gpu-console-web-manifest.json`。
Console 日志文件名列在其 GPU 验证文档中。

## 未执行项

当前验证机的 `PATH` 中没有可用的 `nvidia-smi`。未运行真实 CUDA 内核、训练任务、
GPU 性能或跨主机部署验收；本地探测替身不能代替这些结果。
未构建 Android/iOS/桌面安装包，未部署 Hub/Runner 或向生态生产服务发送数据。
Vault 工作区快照分发、OS/GPU 强制资源限额和多 Hub 高可用仍待实现。
