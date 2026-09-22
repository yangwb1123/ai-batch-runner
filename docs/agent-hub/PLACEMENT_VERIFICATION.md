# 计算调度诊断验收

日期：2026-09-16。使用说明见 [PLACEMENT.md](PLACEMENT.md)。
本轮增加已提交计算任务的只读设备筛选诊断，覆盖 CLI、TUI、当前回合 Agent
工具和 Console 共享 Flutter 界面；不改变任务状态、目标或资源预留。

## 源码与验证范围

Runner 隔离基线 `6c0815b1bbd9d790dc02873d9676f4b053d68e8b` 来自主工作区既有暂存树，
完整 CI 对应 `485d314761e4ffeeab3477c938a6a18b6930d4a8`；之后仅增加验收文档。
Console 隔离基线 `454fb56c9728ba6a9055de1e1586aff63fe525a7` 包含当时用户已有修改。
最终生产源码 `808b6e6d10c742e4d2cd111e642502998d42c9c7`，最终含文档提交为 `114d6d1fee7a3f3a34af8487e0be645ebc55289f`。
回填只叠加本轮已验证增量，主仓 HEAD 保持不变，原有暂存/未暂存/未跟踪修改保留。
Console 测试对应上述隔离快照，不代表之后主工作区并行变更也已验证。

Runner 完整门禁环境为 Python 3.10.7、pytest 9.1.1、Textual 8.2.8、PyJWT 2.12.0、
cryptography 46.0.7、setuptools 63.2.0、wheel 0.47.0。客户端定向测试另在 Python 3.12.9
执行。完整环境与每条命令、源码、日志哈希保存在本机验证记录。

## Runner

| 检查 | 结果 |
|---|---|
| 服务端与既有计算/GPU/传输定向回归 | 主批 209 项通过；最终 HTTP 与既有 HTTP 41 项通过 |
| Python CLI/TUI/Unix broker 与既有回归 | 182 项通过 |
| 真实 Hub → 签名 Fabric HTTP → Runner → 子进程 | 2 项通过 |
| 只读事件历史断言补充后的 API/HTTP | 94 项通过 |
| 完整 make ci | 通过：4138 项测试、53 项 eval、56 项门禁自测、8 项工程门禁 |
| python quality.py --strict . | 通过 |
| OpenAPI | 1.10.0，36 个 operation ID 唯一，262 个本地引用全部可解析 |

上述定向集合有重叠，不能相加作为唯一用例总数。本轮新增 236 项测试。

真实执行用例占用一个 Runner 后提交第二个任务，验证诊断显示忙碌、重复 GET
不新增 Fabric 任务，释放资源后待执行任务只产生一次文件副作用并返回有效证据。
另一个用例通过真实签名 RPC 停收设备，CLI 查询保持只读；任务取消后，不再请求
设备库存。测试使用本机真实子进程，无真实模型调用。完整 CI 包含仓库原有
真实 Codex 集成用例；其模型计费未核验。

专项覆盖包括当前授权与原目标集合交集、分页前过滤、权限缩小/扩大、缺失设备行、
非 queued 跳过库存、读取期间状态推进、数据库与事件历史不变、active-turn 权限、
错误代次、精确 DTO、响应大小、Unicode 排序/游标、迟到响应、身份/选择失效。
CPU/内存 NaN/Infinity 和异常类型在真实调度与诊断中共同关闭失败；正常探测恢复
后才派发。畸形 UTF-8 路径/游标返回 400，合法 U+FFFD 仍按普通标识符处理。

首次完整门禁在测试夹具的 `inventory` 替换上触发知识检查，未进入全量测试。
保持门禁规则不变后，测试明确命名数据库状态/历史快照，并新增 HTTP 事件历史
前后相等断言；初次失败日志保留为 `initial-ci.log`，最终结果以上表为准。

## Console

使用本机 Flutter 3.48.0-0.5.pre（framework `2d06a6e304`）、Dart 3.12.0-168.0.dev；
未使用 CI 固定 Flutter 3.47.4 验证。依赖锁保持本轮基线内容不变。

| 检查 | 结果 |
|---|---|
| 最终诊断新增测试 | 69 项，VM 与真实 Chrome 全部通过 |
| 最终关联 Agent 回归 | 59 项通过 |
| flutter analyze | 通过 |
| Web release / release artifact | 通过 |
| 完整 Flutter VM（核心提交 `493a031`） | 1568 项通过、3 项跳过、0 项失败 |
| 既有浏览器 / guard / Python | 60 / 83 / 24 项通过 |
| Kubernetes minimal/full 模板渲染 | 通过；未部署 |
| 全局 harness | 失败：仅两处既有 Forge 文件长度超限，独立基线已复现 |

全量 VM 后增加了可读观察时间、安全时间范围回退及打开诊断前的身份变化检查，
最终 69 项新测试、关联 Agent 回归、分析、Chrome 与构建均针对最终生产源码复验；
没有把核心提交的全量结果标为最终源码的全量重跑。

文件长度问题为 `lib/api/forge_conversations_models.dart`（948 行）和
`lib/screens/forge/forge_sessions_screen.dart`（937 行）超过 400 行；本轮未修改它们，
也未增加门禁豁免。本次核心全量测试未复现上轮记录中的 Forge widget 失败。
详见 Console 的 `docs/AGENT_COMPUTE_PLACEMENT_VERIFICATION.md` 及本机逐命令记录。

## 实现边界与未执行项

没有新增数据库表、OAuth scope 或持久请求类型；复用任务与原目标账本。
诊断只表示当前可见设备满足哪些 Hub 筛选条件，不代表预留、排队位置或最终
Fabric 策略/配额准入。分页是实时观察；没有隐藏设备总数，Agent 工具只能读取
本回合自己的任务。原会话离线/冻结仍可读已提交任务。

Console Chrome 测试验证真实浏览器中的共享组件与 HTTP 替身，不代表已连接生产
身份服务和远端设备；未构建原生安装包、未在物理移动设备验收、未部署生态服务。
本轮没有跨主机压力、真实 CUDA 任务、HA 或跨控制面联邦验收。

本机证据目录 `/tmp/hub-placement-20260916-uct6alx4/` 保存
`runner-validation.json`、`server-validation.json`、`python-clients-verification.json`、
`integration-results.json`、`console-validation.json`、文档复验与 `applied.json`。
原始日志、源码与产物摘要可按收据逐条核对；临时目录不是长期归档服务。
