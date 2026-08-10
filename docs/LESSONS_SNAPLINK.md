# 实战校准记录（第 2 期：snaplink-console 设计工程实践）

> 2026-08-06~08-07：用 ai-batch-runner 的规范/门禁体系维护 snaplink-console
> （Flutter 管理控制台）——设计系统 9 波 + 动效 5 波 + 导航重构 + i18n 清零 +
> 组件库扩充 + 表格化。每条 = 真实遇到的问题 → 根因 → 工具修正/教训沉淀。

## E. 检查器实战修正（工具侧，本次会话新检查器迭代）

| # | 问题 | 根因 | 修正 |
|---|---|---|---|
| E1 | design-intelligence 检查器 KPI 误报 177 条（service/model/theme 文件全被查） | 未限定 UI 文件 | 加 `Widget build` 信号限定 |
| E2 | KPI 误报 100 条（widgets 组件文件） | 组件 ≠ 页面 | 页面文件判定（screens/ 目录 + 文件名） |
| E3 | KPI 误报 60 条（detail 页全被查） | **`/home/` 路径匹配了 'home' 关键词** | 关键词只对文件名判断（`Path.name`） |
| E4 | KPI 误报（shell/组件化页面） | 导航壳/StatCard 子组件无本地大字样式 | 豁免 NavigationRail 壳 + StatCard/CountUp 组件化 KPI |
| E5 | backend-experience 检查器 `db.Update(` 未命中 | 正则只匹配小写 `.update(` | 大小写变体（update/Update/save/Save...） |
| E6 | async 生命周期误报（`Status: 'queued'` 被当散落字面量） | 状态字面量检查不识别队列状态 | 豁免 async 状态词表（queued/processing/done/failed...） |
| E7 | 关键字段审计误报（`count`/`total` 赋值被当金额） | 关键字段词太宽 | 收紧为 amount/balance/price/inventory/stock/salary + 中文金额库存价格 |
| E8 | knowledge 检查器误报（正则模式字符串里的"金额/余额"被匹配） | 全文件 `=` 匹配 | 只匹配"字段赋值"形态（`field +=|-=| =`） |
| E9 | **真实抓错**：ProgressRing `Colors.amber` 裸语义色 | 检查器设计目标达成 | snaplink 侧 token 化修复 |

**方法论**：新检查器必须在真实仓库跑通 183→0 才算可用——误报率是检查器
质量的度量；宁可多轮收紧（每次用真实违规校准），不一次性宽松。

## F. 批量编辑与结构操作教训（对 Agent 的通用约束）

| # | 教训 | 规则 |
|---|---|---|
| F1 | **替换必须最长匹配优先**：token 化 `Colors.orangeAccent` 被 `Colors.orange` 前缀吃掉 → 40 个编译错误 | 批量字符串替换按长度降序执行；或逐 token 精确匹配 |
| F2 | **新增 i18n key 必须先查跨 catalog 重复**：3 次 const map 编译错误（`{count} selected`/`{label}: {value}` 等已在别的 catalog） | 新增翻译前 grep 全部 catalog；const map spread 重复 = 编译失败 |
| F3 | **UI 批量插入要确认容器类型**：副标题插进标题 Row（Spacer 列）→ 水平溢出 ×2 页 | 批量插入 UI 元素前先看容器是 Row 还是 Column；渲染测试兜底 |
| F4 | **easeOutBack 回弹曲线 overshoot 值域**：opacity 直接接曲线值超 1 → 动画断言崩溃 | 回弹曲线（easeOutBack/elastic）用于 opacity/布局必须 `.clamp(0,1)`；scale 可保留 overshoot |
| F5 | **手势竞争**：Tooltip 默认长按触发吞掉行 InkWell 长按 → 批量选择失效 | 可交互单元格内嵌套手势组件注意 recognizer 竞争；Tooltip 用 `triggerMode.tap` |
| F6 | **纯数据展示用 Text 不用 LocalizedText**：值==key 的占位翻译污染 source-pattern 匹配（'Page 3 · 12 users' 被 '{x} · {y}' 截获） | 数据（版本号/计数/动态字段）用 Text；界面文案用 LocalizedText |
| F7 | **不可变列表排序崩溃**：`_entries.sort()` 修改不可变列表 → 运行时异常 | 排序前复制（`[...list]..sort()`） |
| F8 | **表格在 ListView 内嵌套垂直滚动冲突** | 表格组件提供 `scrollable` 参数；页面滚动容器内 false，Expanded 内 true |
| F9 | **测试字体列宽溢出**：StatusChip 'Suspended' 在 140px 列溢出 | 表格列宽考虑测试字体（字符≈13px）；表头 label 包 Flexible |

## G. 设计实践验证（规范有效性证据）

| # | 规范/原则 | 实战证据 |
|---|---|---|
| G1 | 克制原则（三次原则/god-file 保留） | sso_client 607/dashboard 597 判定内聚保留；AsyncView 推广回滚（分页/路由耦合）——不硬拆正确 |
| G2 | 语义色 token 化 | 200+ 硬编码 → AppColors；muted 调深（对比 4.3:1）；深色点缀 75 处直引改造成本>收益 → 评估关闭 |
| G3 | 信息优先级/异常优先 | 工作台 hero（大数字+统计）、异常卡片置顶、UsageBar/Sparkline 数据可视化——用户 3 秒理解 |
| G4 | 两级导航 IA（一级 ≤8） | admin 35→6 组、portal 10→3 组；SectionSelector 组内 tabs；组记忆 |
| G5 | 徽章文字色按渲染底色（color-intelligence 06 节） | StatusChip 浅底白字 1.18:1 bug——onColor 基于 blended 底色亮度 |
| G6 | 状态双编码（色+图标+文字） | StatusChip 8 处 + 方法徽章 + 时间线语义色 |
| G7 | 对比度门禁价值 | dark 3 个真实可读性 bug 由测试暴露（深色文字 2.17:1/浅底白字 1.18:1/muted 2.56:1） |
| G8 | 动效有限时长原则 | 15 项动效全用一次性动画（90-600ms）——测试 settle 安全，无循环动画挂起 |

## H. 教训沉淀状态

| # | 教训 | 已落点 |
|---|---|---|
| H1 | 动画曲线 overshoot 值域（F4） | `ui-specs/design-intelligence/05-emotional-design.md`、`ui-specs/rules.yaml` |
| H2 | 手势竞争清单（F5） | `ui-specs/engineering/interaction-patterns.md` |
| H3 | 两级导航 IA（G4） | `ui-specs/design-intelligence/03-information-priority.md` |
| H4 | 测试字体溢出（F9） | `ui-specs/component-spec.md` |
| H5 | 批量编辑纪律（F1/F2/F3） | `backend-specs/agent-guardrails.md` 第 6 节 |

## I. 工具能力验证总结

- **门禁抓错 40+ 次**：spec-check（间距/色值/i18n）×15、analyze ×10、渲染测试 ×8、
  动画断言 ×2、编译错误（const map 重复/未导入）×5——门禁守护是常态
- **检查器 3 个新增**（designintelligence/backendexperience/knowledge）全部经
  "真实仓库 183→0 校准"流程
- **snaplink-console 最终态**：671 测试全绿、spec-check 绿、i18n 债清零
  （静态 431→0、动态 50→0 有骨架）、导航两级化、组件 20+、动效 15+
