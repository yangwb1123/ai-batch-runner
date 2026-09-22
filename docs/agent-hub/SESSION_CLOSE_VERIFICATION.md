# 远端空闲会话关闭验收记录

日期：2026-09-15。使用与恢复边界见 [SESSION_CLOSE.md](SESSION_CLOSE.md)。
本轮范围是跨端关闭空闲会话、实际关闭确认后回收容量、保留历史与幂等恢复。

## 源码与环境

Runner 从上一轮已验证的 `4c94110b967e260540b012a583452d367a23ecad` 建立隔离
worktree。完整 CI 对应 `0e197a0770502301b825eb0b8ee346d16d5abec3`；之后仅增加本记录。主仓已有的机器认证、
用户登录和会话创建暂存内容保留，主分支 HEAD 不移动。

Console 从应用了上一轮 Agent 增量、并包含当时用户 Forge/依赖修改的主工作区
建立隔离快照 `d2c98b03769116a8e8d6143a70a44ee72dcb3e1c`，本轮关闭增量对应
`281e649172026d8bac7ba55d0a5228d3b6662974`。实施期间主 Console 仍有并行工作，本报告的全量结果只对应
该隔离快照及本轮 Agent 增量，不冒充之后主工作区并行修改的验收。
回填时只叠加验证过的 Agent 文件，保留原有暂存内容、未暂存修改和未跟踪文件。

Runner 使用独立 Python 3.10.7 环境，pytest 9.1.1、PyJWT 2.14.0、
cryptography 50.0.1、Textual 8.2.8、setuptools 84.0.0，editable 安装本轮源码。
完整版本记录保存在本轮临时目录的 environment.json。

## Runner 检查

| 检查 | 结果 |
|---|---|
| 新增 Hub 关闭/并发准入/HTTP 专项 | 69 项通过 |
| Hub 与原有 store/HTTP/creation/compute 回归 | 155 项通过；追加旧 generation 断言后 3 项复测通过 |
| Gateway/creation/close/delivery/AgentSession 回归 | 114 项通过 |
| 合并后的客户端、Gateway、HTTP 及认证回归 | 289 项通过（含下述 9 项整链路） |
| 真实 HTTP 关闭整链路 | 9 项通过 |
| 完整 make ci | 通过：3752 项测试、53 项 eval、56 项门禁自测、8 项工程门禁 |
| python quality.py --strict . | 通过 |

上述集合包含重叠用例，不能相加作为不同测试总数。

9 项整链路使用真实 HTTP、SQLite、Hub 授权、Gateway、AgentSession 和 CLI，
仅 provider runtime 使用确定性替身，覆盖：

- 关闭后重建容量、历史完全保留、另一实例及同实例其他会话继续执行。
- 关闭调用阻塞时仍保留名额，其他会话 prompt、取消和心跳继续工作。
- Gateway 关闭 ACK 丢失时重报持久回执，不再次 close，也不重复占用名额。
- runtime.close 失败后保留只读失败状态与容量，不把后续幂等空返回冒充成功。
- 用户在创建成功但 Gateway 尚未完成本地 adoption 时请求关闭，会安全等待。
- Gateway 换代把旧 queued 关闭记为 lost，不向新 generation 重放。
- CLI 沿原 key 恢复用户侧丢失的关闭回执。
- 已完成 turn 的终态事件 ACK 丢失后关闭会话，旧事件仍可获得一致补回执。
- 取消排队 turn 后关闭会话，迟到的 running 事件被有限淘汰，不堵塞其他会话。

交叉审查指出旧事件处理把 frozen 与 stale_generation 混为一谈，会让单会话关闭
误触发整个实例 fencing。已修复：当前 generation 的既有 canonical 事件重放返回
原回执；冻结后的新事件返回 session_read_only，Gateway 将对应本地事件标记过期，
继续处理其他会话；真实代际冲突仍停止旧 Gateway。

准入与并发测试覆盖相同 key、不同 key、actor/client/tenant 隔离、关闭与新 turn /
compute 的两种竞争顺序、终态计算结果与待归档记录保留、旧数据库升级、重启恢复、
普通 session 更新不能伪造容量释放或复活会话，以及成功回执与新建请求并发。

本轮新增关闭用例未调用真实模型。完整 Runner CI 仍包含仓库已有的真实 Codex
CLI 集成测试，使用本机已配置的运行环境；本轮未核验模型计费。

## Console 检查

Console 使用 Flutter 3.48.0-0.5.pre（framework `2d06a6e304`）、
Dart 3.12.0-168.0.dev，执行 `pub get --enforce-lockfile`；本轮未改依赖或 lock。

| 检查 | 结果 |
|---|---|
| flutter analyze | 通过 |
| Agent 定向 VM 测试 | 53 项通过 |
| 完整 Flutter VM 测试 | 1435 项通过、3 项跳过、0 项失败；本轮新增 23 项 |
| 仓库 Chrome 浏览器测试 | 60 项通过 |
| 新增关闭测试在真实 Chrome 中执行 | 23 项通过 |
| Python 测试 / guard | 24 项 / 83 项通过 |
| Web release 构建与 artifact 检查 | 通过 |
| Kubernetes minimal/full 模板渲染 | 通过；未部署 |
| 全局 harness | 失败，仅下述 3 个原有 Forge 文件超长；其余检查通过 |

对同一隔离基线独立运行完整 Flutter 测试得到 1412 项通过、3 项跳过、0 项失败。
`check-filesize` 在基线和本轮源码均报告以下文件超过 400 行：
`lib/api/forge_conversations_api.dart`（454）、
`lib/api/forge_conversations_models.dart`（948）、
`lib/screens/forge/forge_sessions_screen.dart`（935）。
这 3 个文件在本轮增量中完全未变，保留问题，不将全局 harness 记为通过。
结果、命令、日志 SHA256、源码路径清单及基线对照见 console-validation.json。

API、控制器、确认弹窗、同 key 恢复、queued 只 GET、窄屏中英文、关闭后的
prompt/compute 禁用以及迟到身份响应的测试使用 MockClient；未连接真实登录的
Hub，也没有构建原生安装包或在物理设备上运行新的关闭流程。

## 发布与边界

本轮只改 Runner 与 Console，未配置或部署生态服务、发送外部通知、修改账户权限。
使用现有 Snaplink 用户/机器身份与 Aero ID 门禁，不新增 OAuth scope；授权仍按实例
共享管理，不将创建者幂等身份误当作独占所有权。

关闭请求与历史数据不自动删除；没有强制关闭、会话重开、迁移或多 Hub HA。
任意 Python provider 的阻塞 close 无法被强制终止，Hub 会保留 queued 和容量。
底层 close 抛错后保留 failed 与名额，需 Gateway 重启恢复；没有自动重复调用。
客户端追踪为进程/页面内存，重载或重新登录不保留本地恢复 key。

升级顺序为 Hub → Gateway → 客户端。旧 Gateway 默认不声明关闭能力，其既有
prompt/会话功能保持兼容。真实跨设备公网、生产身份与部署、长期压力和原生设备
验证未执行，应在实际环境分别验收。

本机原始日志与应用收据位于 `/tmp/hub-session-close-20260915/`：
`targeted.log`、`targeted-results.json`、`ci.log`、`strict-quality.log`、
`verification-results.json`、`store-validation.json`、`gateway-verification.json`、
`console-validation.json` 与 `applied.json`。临时路径用于本机证据核对。
