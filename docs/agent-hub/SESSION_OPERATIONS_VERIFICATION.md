# 会话请求记录验收

日期：2026-09-15。使用与恢复边界见 [SESSION_OPERATIONS.md](SESSION_OPERATIONS.md)。
本轮仅增加已受理创建/关闭请求的跨端发现与只读追踪。

## 源码与环境

Runner 隔离基线 `1eb4da926fee3adf4d66bfe1c6a67f5ca36b91f4`；完整 CI 对应
`d93892765eaa6edc8ac4e4a901eb1d462b8a4337`，之后仅增加本验收记录。主仓保留前轮已暂存改动，HEAD 不移动。
Console 从本轮主工作区建立隔离快照 `000677f3ea14da87c17b9c16f99867489adc582a`，
包含当时用户 Forge/依赖修改；本轮 Agent 增量为 `1bc16205edaf9d83b09db19bbca8e21382f767fc`。
Console 验证只对应该快照加本轮增量，不代表之后主工作区并行修改也已通过。
回填只叠加已验证 Agent 路径，保留原有暂存、未暂存与未跟踪内容。

Runner 环境：Python 3.10.7、pytest 9.1.1、PyJWT 2.14.0、cryptography 50.0.1、
Textual 8.2.8、setuptools 84.0.0、wheel 0.48.0；完整版本见 environment.json。

## Runner 检查

| 检查 | 结果 |
|---|---|
| Store/API 及既有会话、HTTP、身份授权回归 | 203 项通过 |
| CLI/TUI 与既有客户端相关回归 | 核心提交 177 项通过 |
| 客户端最终 UTF-8 标识符补充修复 | 全部 78 项请求记录专项通过，含 8 项新增断言 |
| 合并后真实 HTTP 整链路 | 5 项通过 |
| 完整 make ci | 通过：3902 项测试、53 项 eval、56 项门禁自测、8 项工程门禁 |
| python quality.py --strict . | 通过 |

各定向测试集合可能重叠，不能相加作为唯一用例总数。

5 项整链路使用真实 HTTP、SQLite、Hub 授权、Gateway、AgentSession 与 CLI，
仅 provider runtime 为确定性替身：

- 创建 POST 已成功但回执丢失，新客户端通过列表找到同一请求并定位创建结果，
  查询不重复创建；显式发送新 prompt 后仅执行一次。
- 关闭 POST 回执丢失后找回关闭结果，实例离线时仍能读取历史，不再次 close。
- 不同用户/client 的实例 read grant 可读取共享记录，不能读取其他实例或租户，
  只读用户不能写入，Gateway 机器身份不能调用用户历史接口。
- Hub 重启保留 queued 请求；Gateway 换代后列表反映 lost，不隐式重放请求。
- CLI 逐页获取创建/关闭记录，每页只做显式 GET，不返回原幂等 key。

Store 与 HTTP 专项包括同时间排序、跨表同 ID、分页前授权过滤、状态更新不移动
记录、时钟回退、同一读取事务面对并发写入、索引升级/查询计划、畸形游标、C1
标识符、GET body/重复参数和错误方法。客户端专项检查精确 DTO、响应大小、
游标/重复项/目标绑定、旧 Hub、迟到响应、显式导航和登录上下文变化。

本轮新增测试不调用真实模型。完整 Runner CI 包含仓库原有真实 Codex CLI
集成测试，使用本机既有运行配置；未核验模型计费。

## Console 检查

Console 实际环境为 Flutter 3.48.0-0.5.pre（framework `2d06a6e304`）、
Dart 3.12.0-168.0.dev。隔离基线及本轮源码均通过 `pub get --enforce-lockfile`，
本轮未修改 pubspec/lock；没有使用 CI 固定的 Flutter 3.47.4 验证。

| 检查 | 结果 |
|---|---|
| flutter analyze | 通过 |
| Agent 定向 VM 测试 | 120 项通过 |
| 本轮新增请求记录测试 | 61 项，全部在 VM 和真实 Chrome 通过 |
| 仓库既有 Chrome 测试 | 60 项通过 |
| Python 测试 / guard | 24 项 / 83 项通过 |
| Web release 构建 / artifact | 通过 |
| Kubernetes minimal/full 模板渲染 | 通过；未部署 |
| 完整 Flutter VM 测试 | 1502 项通过、3 项跳过、1 项既有 Forge 失败 |
| 全局 harness | 失败，仅下述 2 个原有 Forge 文件超长；其余检查通过 |

对隔离基线独立执行完整 Flutter 测试得到 1441 项通过、3 项跳过、1 项失败。
唯一失败为 `test/forge_sessions_widget_test.dart:322` 的
`keeps the conversation page cursor after load more fails`，在 `ensureVisible`
处出现 `StateError: Bad state: No element`；在基线上单独运行同一用例也稳定失败。
本轮新增 61 项请求记录用例全部通过，未将该既有失败计作通过或修改其代码。

文件大小门禁在基线和本轮源码均报告
`lib/api/forge_conversations_models.dart`（948 行）及
`lib/screens/forge/forge_sessions_screen.dart`（937 行）超过 400 行。
上述 3 个 Forge 测试/源码文件在本轮增量中完全未变。命令、日志 SHA256、
源码哈希及独立基线对照完整保存在 console-validation.json。

API、控制器与共享 Flutter 组件使用 MockClient，Chrome 用例运行在真实浏览器中，
不代表已连接真实登录的 Hub/Gateway 完成设备验收。未构建原生安装包或安装到
物理设备。本轮不修改 Forge、原生宿主、依赖清单或 lock。

## 实现与恢复边界

不增加第三套请求状态，也不改变 Gateway 或请求写入路径。两个账本只新增查询
索引；读事务各取最多 limit+1 条，再有界合并。按 tenant/当前实例 read grant
过滤后分页，不按发起者或 OAuth client 私有分组，不猜测 pairwise subject 对应关系。

记录排序使用不可变创建时间、operation 和 request_id。游标是有界且版本化的
opaque 值，客户端不解析浮点排序字段。列表为实时分页，非冻结快照；状态更新
不移动记录，时钟回退的新请求按记录时间进入更早区间，需刷新核对。

列表/详情/Open 均只读，不清理原有未知提交 key，不自动 POST。未查到请求不能
证明原提交未受理；请求仍在传输、权限变化和未翻完页面都影响当前可见范围。
实际恢复已有 key 的方式保持原创建/关闭契约。请求中心不实现强制关闭、重开、
删除、迁移、跨 Hub 联邦或 HA。旧 Hub 不支持列表时，原有会话功能不受影响。

本轮只修改 Runner 与 Console，未部署生态服务、注册账户/scope或发送外部通知。
真实公网、多设备生产身份、长期压力和原生设备验证未执行。

本机原始日志与应用收据位于 `/tmp/hub-session-history-20260915/`：
`targeted-results.json`、`verification-results.json`、`documentation-results.json`、
`store-validation.json`、`clients-validation.json`、`console-validation.json`、`applied.json`。
临时路径用于本机证据核对。
