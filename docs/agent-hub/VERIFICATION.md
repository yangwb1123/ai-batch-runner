# Agent Hub P0/P1 验证记录

后续 P2 计算路径已实现，最新结果见 [COMPUTE_VERIFICATION.md](COMPUTE_VERIFICATION.md)。

验证对象是本次 `ai-batch-runner` 与 `snaplink-console` 工作树。
以下结果是本地实现验证，尚未进行真实 Snaplink 部署、跨公网设备联调或原生安装包验收。

| 检查 | 结果 |
|---|---|
| Runner `make ci` | 通过：2,799 项测试、53 项规则评估，以及发布、工程门禁、harness、质量与组织检查；退出码 0 |
| Runner `python quality.py --strict .` | 通过 |
| Hub HTTP、双实例 Gateway 和 TUI 专项复验 | 15 项通过；新增 TUI 容量用例所在文件 4 项通过，均已纳入最终全量回归 |
| OpenAPI 实际响应验证 | 21 项通过；真实 Store/Service 响应以 Draft7Validator 校验本地 schema 引用 |
| Console `flutter test --no-pub` | 1,262 项通过 |
| Console `flutter analyze --no-pub` | 通过，无问题 |
| Console `flutter build web --no-pub` | 通过，产物为 Console 的 `build/web/` |
| Console 部署配置与代理 Python 测试 | 13 项通过 |
| Console `make ui-quality` | 通过，扫描 406 个 lib 文件 |
| Console Agent 页面 UI spec | 通过，扫描 8 个 Agent 页面文件 |
| Console `make spec-check` | 未通过；全仓 `ui-check` 报告下面 8 处既有间距问题 |
| 两仓 `git diff --check` | 通过 |

关键行为覆盖包括：同名本地会话跨实例隔离、授权过滤、精确 prompt 路由、
幂等重试、Gateway 开始确认丢失、排队和运行中取消、旧 generation 拒绝、
断线事件重放、令牌文件轮换、JWT audience/issuer/过期/签名校验、
Console 发送响应丢失后的原键重试，以及 TUI 切换会话后的原键重试。
HTTP 集成测试在 provider 边界使用确定性替身；真实 Codex 测试另行验证本地 TUI。

## 全仓样式门禁的既有问题

下列六个文件及检查器均与 Console 的基线 `12511ad` 内容一致；
本次没有修改这些文件，新增 Agent 页面已单独通过相同检查器。

| 文件 | 报告的问题数与值 |
|---|---|
| `lib/screens/admin/admin_live_events_tab_view.dart` | 1：height 2 |
| `lib/screens/admin/admin_overview_tab.dart` | 1：width 76 |
| `lib/screens/admin/clients_tab_sections.dart` | 1：height 6 |
| `lib/screens/device/device_verify_widgets.dart` | 3：top 1、horizontal 10、height 6 |
| `lib/screens/portal/device_center_widgets.dart` | 1：width 6 |
| `lib/screens/setup/setup_widgets.dart` | 1：width 6 |

## 未执行的部署验收

- 未在实际 Snaplink 中创建 OAuth client、tenant/resource/scope 绑定或部署 Hub grants。
- 未向用户的远端主机部署 Gateway，未验证跨公网连接和设备证书配置。
- 未构建或安装 Android、iOS、桌面客户端；Flutter Web 构建已完成。
- 此处保留 P0/P1 历史结果；Fabric 计算与 Vault 证据归档的后续实现见 P2 记录。
  Vault 工作区传输、Aero ID 成员投影、IM 通知与 Audit 接收端仍待后续实现。

运行方式与接口边界见 [AGENT_HUB.md](../AGENT_HUB.md)，
跨仓变更见 [CHANGE_MANIFEST.md](CHANGE_MANIFEST.md)。
