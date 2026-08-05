# 外部项目维护报告：snaplink-console

> 用 ai-batch-runner 的全部特性维护 `~/workspace/demo/snaplink-console`
> （Flutter/Dart 管理控制台，280 个 dart 文件，约 5 万行）。

## 使用的特性

| 特性 | 应用 |
|---|---|
| `classify` | 项目任务识别：frontend_ui [dart/]（flutter 平台命中） |
| `rules` | 规则匹配：dart 任务加载前端视觉核心 + 工程规范 manifest |
| `assess` | 需求评估：克制处方（生产关键词不抬高规模档）、工作流 L0 直改建议 |
| `check-ui-spec.py --dir` | 项目级 UI 规范扫描（uicheck 机制） |
| `check-frontend-quality.py --dir` | 项目级工程扫描（uiquality 机制） |
| 目标项目自身门禁 | `python cli.py check` 全过（filesize/complexity） |

## 扫描发现（真实信号）

### UI 规范（check-ui-spec --all：280 文件 / 125 违规）

- **魔法间距 106 处**：`SizedBox(height: 10)` ×30+（最高频）、
  `height: 14/6`、`EdgeInsets.only(bottom: 10)` 等——非 8pt token 值
  （2/3/5/6/7/9/10/14/18/22）
- **硬编码颜色 19 处**：`Color(0xFF6366F1)` ×6+（主色散落）、
  `0xFF0F172A/1E293B/2563EB/059669...`
- 修复建议：建立 `AppSpacing`/`AppColors` token 类，`SizedBox(height:10)`
  → `AppSpacing.sm`（12）或 `xs`（8）最近档

### 工程（check-frontend-quality --strict：71 违规，误报已消除）

| 信号 | 数量 | 文件 |
|---|---|---|
| 循环内 await（N+1） | 4 | authz_check_tab / webhook_detail_screen / notifications_tab / overview_tab |
| 空 catch 吞异常 | 5 | audit_log_service / cross_tab_sync / list_state_manager |
| 上帝文件 >500 行 | 6 | sso_client 607 / dashboard 597 / admin_operations 546 / governance 512 / clients 509 / token_security 624 |
| 深度嵌套 >8 | 17 | tenants_tab 21 / users_tab 20 / clients_tab 20 / sessions_tab 15... |
| api 调用 >5/文件 | 13 | commerce_api 14 / user_support_tab 16 / snaplink_admin_api 8... |
| 决策点 >30 | 8 | oidc_authorization_flow 58 / governance 52 / token_security 51... |

### 检查器自身改进（本次维护的副产品）

对 Dart 的启发式误报被本项目门禁发现并修复：
- `EdgeInsets.only(` 合法 API 被误判为测试 `.only`（121 处误报 → 消除）
- Dart 命名参数 `html:` 被误判为不安全 HTML（`html: resp.body`）
- Flutter widget 树结构性深嵌套 → `.dart` 嵌套阈值放宽到 8
- 误报总量：280 → 71，真实信号全部保留

## 建议修复优先级

1. **P0 吞异常 ×5**：`catch (_) {}` 加日志/错误上报（失败路径不可见）
2. **P0 N+1 ×4**：循环 await → `Future.wait`/批量接口
3. **P1 魔法间距**：建立 spacing token 类，全局替换（106 处）
4. **P1 颜色散落**：`0xFF6366F1` 等提取为主题常量（19 处）
5. **P2 上帝文件**：sso_client/dashboard 等按业务域拆分
6. **P2 深层嵌套**：抽出条件 widget/helper（tenants_tab 深度 21）

## 沉淀

- `docs/rules/drafts/GATE-DART-ADAPT-001.yaml`：检查器 Dart 适配规则草案
- 本报告存于 `docs/reviews/snaplink-console-maintenance.md`
