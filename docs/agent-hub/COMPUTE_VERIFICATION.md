# P2 计算路径验证记录

验证范围：Hub、Fabric、Gateway 调度工具、CLI/TUI、Flutter Console 与可选 Vault
证据归档。工作区代码已整合；运行场景和配置见 [COMPUTE.md](COMPUTE.md)。
最后整体验证日期：2026-09-12。
后续 P3 生态集成结果见 [ECOSYSTEM_VERIFICATION.md](ECOSYSTEM_VERIFICATION.md)。
后续 GPU 增量结果见 [GPU_VERIFICATION.md](GPU_VERIFICATION.md)。
丢失任务显式恢复见 [LOST_RETRY_VERIFICATION.md](LOST_RETRY_VERIFICATION.md)。

| 检查 | 结果 |
|---|---|
| Runner `make ci` | 通过：2,850 项回归测试、56 项门禁自测、53 项规则评估；发布检查、组织规范、能力注册表和无环依赖图检查通过 |
| Runner `python quality.py --strict .` | 通过 |
| 工程门禁 `make check` | 8 个门禁通过 |
| Hub/Fabric 最终专项 | 59 项通过，含真实双 HTTP 服务与 Runner 子进程执行、持续心跳、运行中取消 |
| Vault REST 契约 | 10 项通过，使用本地 HTTP 替身，未连接生产 Vault |
| Compute TUI | 7 项交互测试通过 |
| OpenAPI | YAML 和新增 schema 合法；4 个实际 Store/资源 DTO 样本通过 Draft7Validator 校验 |
| Console Flutter 全量测试 | 1,268 项通过；随后补充的边界与 UTF-8 用例所在定向组 22 项通过 |
| Console `flutter analyze --no-pub` | 通过，无问题 |
| Console `flutter build web --no-pub` | 通过；已将相同源码构建的产物同步到主 Console 工作区 `build/web/` |
| Console 改动页面 UI spec/质量 | 通过 |
| Console 全仓 `make ui-check` | 仍有 8 个既有间距问题，文件与 P0/P1 记录一致 |

关键验证包括：按目标实例授权过滤、CPU/内存准入、默认零内存预算、运行时
探测、并发幂等、丢失 ACK 后重启恢复、原服务身份绑定、退出后撤销的本地工具
能力、发起用户与 Gateway 权限交集、turn actor 归属、写/取消权限不泄露结果、
用户/租户入队配额、单 Runner 执行槽、慢归档不阻塞计算和取消、mTLS 与禁止重定向。

两项真实计算集成运行在临时目录和 loopback 端口：一项确认命令只执行一次并
返回 accepted evidence digest；另一项确认长任务仍持续心跳，取消后停止进程，
释放预留 CPU，且不产生取消后的文件。没有调用付费模型或修改真实外部账号。

本轮审查修复了 turn 关联归属、mutation 回执输出泄露和共享队列隔离三个问题，
均有回归用例。Fabric 回归同时修复了禁止重定向后 HTTPS context 的传递方式。
Campaign 的 4 个旧测试改为显式注入假 Runner，且补充无项目本地入口时的
工具入口回退测试；46 项通过。主工作区已有的 Campaign 入口实现修改保持原样。
旧 SSH 取消测试补上 finally 清理，防止模拟进程残留使后续 Runner 被误判为忙碌；
该测试与两项真实计算集成按原顺序复测，3 项全部通过。

## 尚未执行或实现

- 未部署实际 Snaplink OAuth client/resource/grants、Hub 或远端 Runner，也未进行跨公网实机验收。
- 未在真实 Aero Vault 服务上传文件；固定版本 REST 行为依据仓库契约以 HTTP 替身验证。
- 工作区需预先配置；本次 P2 验证时未实现 Vault 不可变快照传输、GPU 调度或
  OS 资源强制限额。GPU 的后续实现与验证见本页顶部链接。
- 此记录对应 P2；Aero ID、IM、Audit 的后续接入见 P3 验证记录。多控制面高可用仍未实现。
- 未构建 Android/iOS/桌面安装包；已验证共享 Flutter 代码和 Web 构建。

历史验证及既有 Console 问题明细见 [VERIFICATION.md](VERIFICATION.md)。

## 2026-09-21 派发前失联恢复追加

Hub 派发后、Runner 启动前的设备失联窗口已补齐：Fabric 明确返回
`started_at: null` 时，旧句柄被原子清理，任务恢复 `queued`，并使用递增的
Fabric request id；有启动证据或字段缺失时保持 `lost`。排队中的显式目标约束不被
自动清除。详细契约见 [PRESTART_RECOVERY_VERIFICATION.md](PRESTART_RECOVERY_VERIFICATION.md)。

| 检查 | 结果 |
|---|---|
| `python -m pytest -q tests/test_hub_compute.py` | 23 项通过 |
| Hub 计算、transport、Broker、TUI 联合回归 | 46 项通过 |
| Hub GPU/Fabric/HTTP 集成回归 | 46 项通过 |
| `python quality.py`（触及的 Hub 文件，`--strict`） | 通过 |
| `make check` | 8 个工程门禁通过 |

这些结果是本地 SQLite 与 loopback 测试替身的实现证据；没有把真实 Snaplink
部署、跨公网设备或真实副作用命令的验收混入本轮结论。

## 2026-09-21 丢失任务显式重试追加

启动后丢失的任务现在可以由任务创建者显式确认后重新入队。重试要求
`confirm_duplicate=true`，只作用于 `lost` 且未启用归档的任务；原显式目标、授权
集合和项目绑定均保留，旧句柄与结果清理后使用新的 Fabric request id。CLI、TUI、
active-turn broker 与 Flutter Console 已接入相同接口。详细行为和限制见
[LOST_RETRY_VERIFICATION.md](LOST_RETRY_VERIFICATION.md)。

| 检查 | 结果 |
|---|---|
| Hub/HTTP/Broker/TUI 相关回归 | 52 项通过 |
| 触及 Python 文件 strict quality | `quality: OK` |
| Console API 与丢失任务卡片定向测试 | 9 项通过 |
| Console `flutter analyze` | 本轮 Agent 改动无错误；另有 Forge 页面既有 2 项错误 |

## 2026-09-21 同租户多 Fabric 联邦追加

一个 tenant 现在可以配置多个独立 Fabric 控制端点。Hub 在设备查询和 placement
诊断中合并各端点的授权 inventory；任务选定设备后，工作目录、服务身份、提交、
状态轮询和归档均使用该设备所属的连接，不会把请求发送到同租户的其他端点。配置
加载时拒绝重复 URL 或重复 `device_id`，单连接的旧配置和 `connections[tenant]`
调用保持兼容。不同 Vault 的连接要求显式目标设备，共享 Vault 才允许无目标快照操作。

| 检查 | 结果 |
|---|---|
| 同租户双 Fabric 配置、URL/device 冲突校验 | 通过 |
| 聚合设备清单、目标设备跨端点派发 | 通过：新增 2 项回归 |
| 单端点 inventory 失败时健康端点继续自动调度 | 通过；完整 inventory 读取仍 fail closed |
| Hub compute/workspace/placement/HTTP 回归 | 141 项通过 |
| 触及 Python 文件 strict quality | `quality: OK` |

## 2026-09-21 跨端来源实例可见性追加

聚合会话和设备目录的展示现在保留来源 `instance_id`：Python Compute TUI 的会话、
设备标签，以及 Flutter Console 的实例选择器、会话列表/详情和设备卡片都会显示
实例来源。这样同名会话或设备不会在跨实例视图中被误认；发送 Prompt 和任务目标
仍使用全局 session/device ID，不改变授权或调度边界。

| 检查 | 结果 |
|---|---|
| Python Compute/Hub TUI 定向回归 | 18 项通过 |
| Python 触及文件 strict quality | `quality: OK` |
| Console `agent_gpu_widget_test.dart` 与 `agent_operations_widget_test.dart` | 通过 |

## 2026-09-21 Console 设备与任务分页并发追加

设备目录和任务列表在刷新与“加载更多”同时发生时，现在合并 pending 意图：会话或
项目切换的 reset 优先，普通刷新保留当前请求结果，加载更多继续使用刷新后返回的
最新游标，避免把用户已经打开的下一页丢失或重复加载第一页。任务列表的会话切换
也会显式提交 reset，使旧会话的响应不能污染新会话。

| 检查 | 结果 |
|---|---|
| Console `agent_compute_refresh_test.dart` | 5 项通过 |
| Console Dart 格式化 | 通过 |
