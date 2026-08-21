# 验证比分析产出高（Verify by Doing）

> 归纳来源：iris-ui iter 84 复盘——"The highest-value work this session
> wasn't the 5 originally-planned directions themselves — it was that
> actually building and running real-world-shaped checks surfaced 4
> additional, previously-invisible, real defects."

## 1. 为什么

静态审查只能验证"代码看起来对"；真实运行验证"交付物在真实环境能用"。
两者之间的差距里藏着静态审查看不见的缺陷类别：

| 真实运行形态 | 抓到的实战缺陷（iris-ui） |
|---|---|
| 工作区外的真实 `npm install` + `import()` | Svelte dist ESM 扩展名缺失；Solid 模块加载期 DOM 调用（纯 Node import 崩溃） |
| 行为契约 harness（真实点击/键盘事件） | pagination 非受控模式冻结、table-select 缺 aria-selected、calendar 非受控失效 |
| 真实截图（pixel baseline） | 四框架渲染漂移（vue 2.8% 已知基线）、IrisButton 静默丢 aria-label |
| 真实浏览器 E2E | 既有 smoke 测试长期失败被 continue-on-error 掩盖 |

## 2. 什么时候用（scope / counter_signals）

- 适用：平台/组件库/发布型项目（platform_product）——发布面、跨框架面、
  外部消费面是缺陷高发区；标准及以上档次的实现任务。
- 降级：探索/原型（探索期验证是负担）、一次性快速任务。
- 底线：任何"声称已验证"的产物都必须说明验证形态与运行环境
  （completion_report 门禁），禁止"理论上应该通过"。

## 3. 怎么挂

- pipeline 阶段类型 `verify-experiment`：实现后追加一个真实运行验证阶段
  （外部安装/契约重放/截图/浏览器），验证失败 → 修复 → 重验（gate-fix
  循环复用）。
- 方向建议（campaign/evolution suggest）默认带验证任务，而非纯静态分析。
- 验证器输出（stderr/JSON）回喂下一轮修复 prompt（validator feedback）。
