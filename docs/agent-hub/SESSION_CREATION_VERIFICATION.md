# 远端按需创建会话验证记录

日期：2026-09-14。功能说明见 [SESSION_CREATION.md](SESSION_CREATION.md)。
本轮范围为用户创建请求、Hub 容量与状态持久化、Gateway 后台创建，以及
CLI/TUI/Flutter Console 的创建与恢复入口。

## 源码与环境

Runner 从上一轮已验证的 `18ff36c82d4b879b6bb4810399a9d5879604dfb1` 创建隔离
worktree；运行代码与完整 CI 对应 `839576a9af9df79db11a271c36f8a371c5f02d8b`，之后仅增加本验收记录。
主仓已有机器认证及用户登录暂存改动保留，不移动主分支 HEAD。

Console 从 `03a3bc4e7c290a3a0e2834083214a7b097d20409` 及当时已有的 11 份 tracked、
2 份 untracked Forge 改动建立独立快照 `92b91f3b944954377f500514812e5fb9a8e0f59e`。
本轮 Agent 增量对应 `99dbbf5f24a4c3aa61af9742adc33da0d74e1f35`。这些既有 Forge 改动不属于本轮交付。
实施期间主工作区仍有并行 Forge 工作；Console 全量结果只对应上述隔离快照，
不冒充包含后来并行变化的主仓验收。应用时只选择 Agent 增量，并另存当前工作区
保护快照，不覆盖或暂存他人的 Forge 文件。

Runner 使用 Python 3.10.7、pytest 9.1.1、PyJWT 2.12.0、cryptography 46.0.7、
Textual 8.2.8，并将本轮源码 editable 安装在独立虚拟环境。
Console 工具链和依赖锁验证见下文及其仓内验证记录。

## Runner 验收

| 检查 | 结果 |
|---|---|
| 新增 Hub request 持久层与真实 HTTP 授权 | 50 项通过 |
| 合并后的创建/客户端/Gateway/登录回归 | 188 项通过，包含下列 5 项整链路 |
| 新建会话整链路 | 5 项通过 |
| 完整 `make ci` | 通过：3597 项测试、53 项 eval；完整命令退出 0 |
| 最终 `python quality.py --strict .` | 通过，退出 0 |

专项集合互有重叠，不能相加为不同测试总数。

整链路使用真实 HTTP、SQLite、Hub 权限检查、Gateway、AgentSession 和 CLI，
仅模型 runtime 为确定性替身：

- 两个空实例各创建一个新会话，重复 key 返回原请求，各自 prompt 只执行一次。
- TUI 的 `All instances` 聚合视图能同时加载多个实例会话；prompt 仍按选中的全局
  session_id 路由，创建/关闭/实例请求入口在聚合视图中保持禁用。
- 用户创建已经受理但回执丢失时，CLI 沿原 key 恢复同一个持久请求。
- Gateway 创建成功后丢失 ACK，Hub 已收到的 prompt 等待本地 adoption，重报后
  使用原会话对象执行，不出现第二次工厂调用或错误的 session_unavailable。
- 后台工厂阻塞时，既有会话仍可接收 prompt、响应取消并更新心跳。
- Gateway 换代后旧 queued 请求为 lost，重试原 key 仍返回 lost；新 key 才创建
  新会话，旧请求不会被隐式重建。

专项还覆盖并发请求原子容量预留、同 key 冲突、旧数据库升级、Hub 重启、
事务失败回滚、tenant/实例/主体类型与 scope 拒绝、Gateway generation fencing、
关闭期间晚到工厂结果、Unicode 名称和实例 ID、原生会话 ID 绑定，以及本地
pending receipt 查询索引。运行中的 Python 工厂线程不被强制终止；关闭后其晚到
返回对象会关闭，不再登记。实际第三方 provider 的卡死恢复仍需遵守其自身能力。

TUI 的不确定创建请求遇到后续授权或容量错误仍保留旧 key，只有第一次明确
未受理才允许释放输入。新增回归保护空实例入口、丢失回执、401 恢复、选择变更
与迟到结果；空选择统一按非字符串占位处理，避免 Textual Select.NULL 成为会话 ID。

本轮新增创建测试没有调用真实模型服务。完整 Runner CI 仍包含已有的真实 Codex
CLI smoke 测试，会使用本机已配置的账户/模型；本轮未核验模型计费。

## Console 验收

工具链为 Flutter 3.48.0-0.5.pre / Dart 3.12.0-168.0.dev。最初使用项目 CI
固定的 Flutter 3.47.4 执行 `pub get --enforce-lockfile`，因既有 lock 与 SDK
不匹配而退出 65；随后切换兼容 SDK，严格依赖解析通过，未修改依赖锁。

| 检查 | 结果 |
|---|---|
| `flutter analyze --no-pub` | 通过 |
| Agent API/UI 专项 | 30 项通过，含 17 项新增创建测试 |
| Chrome 既有浏览器集合 | 60 项通过 |
| Chrome 新建会话专项 | 17 项通过 |
| `flutter test --no-pub -r expanded` | 1404 通过、3 跳过、1 失败 |
| Python 工具测试 / guard-count-pin | 24 / 83 项通过 |
| release Web 构建 / release-artifact-check | 通过 |
| minimal/full Kubernetes 渲染 | 通过，仅渲染 |
| `python3 cli.py harness` | 文件行数门禁失败，其余检查通过 |

全量失败是既有 `test/forge_native_route_test.dart:205` 的 Forge sign-out 路由
断言，已在未包含本轮 Agent 增量的 `92b91f3` 快照复现。行数门禁的三处失败也
在该基线复现：`forge_conversations_api.dart` 454 行、
`forge_conversations_models.dart` 948 行、`forge_sessions_screen.dart` 872 行，
均超过现有 400 行限制。本轮未修改这些文件或放宽门禁，不能称全量通过。
三项跳过保持既有状态。

VM/Chrome 使用实际 API 封装、控制器与 widget，HTTP 为 MockClient；没有连接
真实登录后的 Hub 或生成原生应用包。创建流程与幂等 key 保存在页面内存中，
关闭弹窗可以继续恢复，但离开页面、刷新或重新登录不保留本地恢复状态。
Console 详细验证见其 `docs/AGENT_SESSION_CREATION_VERIFICATION.md`；机器收据
为本轮临时目录的 `console-validation.json`。

交叉审查发现并修复了两个恢复边界：首次创建回执未知后，第二次 POST 被拒绝
不能证明最初请求未受理，因此仍须保留原 key；旧请求的 401 不能在页面销毁或
新身份已替换时清除新的默认会话及 scoped 凭据。回归覆盖恢复后同 key/body、
已排队只发 GET、晚到结果不抢选择、旧 401 不清新身份，以及窄屏和中文布局。

## 留存与边界

本轮未配置真实账号/权限、部署 Hub/Gateway、向生态账号发消息或在物理设备上
运行新的 provider 会话。跨设备公网连通、原生安装包和长时间压力测试未执行。
会话创建使用现有用户 scope 和固定 Gateway 项目/模型配置，不提供远端 cwd、
provider、模型覆盖，不实现会话删除/迁移/恢复或多 Hub HA。
升级时先部署支持新协议的 Hub，再更新 Gateway/客户端；旧 Gateway 未声明
session_capacity 时仍可操作既有会话，不能通过新入口创建。

原始日志、隔离快照和应用收据位于本机 `/tmp/hub-session-create-20260914/`：
`targeted.log`、`targeted-results.json`、`ci.log`、`strict-quality.log`、
`verification-results.json`、`environment.json`、Console 验收收据与 `applied.json`。
这些临时路径是本机核对证据，不是永久部署资产。
