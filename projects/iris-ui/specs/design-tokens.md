# iris-ui 设计系统统一规格（Design Tokens Spec）

> 目标：所有组件样式必须由 `--iris-*` token 家族驱动。本文件是四框架
> 适配器修复的唯一权威规则。所有替换必须保持 4 框架行为一致、视觉等价或
> 更统一；不改组件逻辑与 API。

## 1. 字号映射（fontSize / font-size 裸像素 → token）

| 原值 | 替换 | token |
|---|---|---|
| 12px | `var(--iris-font-size-xs, 12px)` | xs |
| 13px | `var(--iris-font-size-sm, 13px)` | sm |
| 14px | `var(--iris-font-size-md, 14px)` | md |
| 15px | `var(--iris-font-size-base, 15px)` | base |
| 16px | `var(--iris-font-size-lg, 16px)` | lg |
| 18px | `var(--iris-font-size-xl, 18px)` | xl |
| 20px | `var(--iris-font-size-2xl, 20px)` | 2xl |
| 22px | `var(--iris-font-size-2xl, 20px)` | 2xl（22 无刻度，就近） |
| 24px | `var(--iris-font-size-3xl, 24px)` | 3xl |
| 30px | `var(--iris-font-size-4xl, 30px)` | 4xl |
| 32px | `var(--iris-font-size-4xl, 30px)` | 4xl |
| 8/10/11px | `var(--iris-font-size-xs, 12px)` | xs（提升可读性，属统一） |

- 保留 `fontSize: 'inherit'` 与相对值。
- `--iris-font-size-sm/md/lg` 旧值语义：sm=13 不变、md 15→14、lg 18→16（消费面仅 drawer/charts，已核对）。

## 2. 间距映射（padding/margin/gap 裸像素 → token）

| 原值 | 替换 | token |
|---|---|---|
| 1–4px | `var(--iris-space-xxs, 4px)` | xxs |
| 5–6px | `var(--iris-padding-sm, 6px)` | padding-sm（紧凑控件） |
| 7–8px | `var(--iris-space-xs, 8px)` | xs |
| 9–12px | `var(--iris-space-sm, 12px)` / `var(--iris-padding-md, 12px)` | sm/md |
| 13–16px | `var(--iris-space-md, 16px)` | md |
| 17–20px | `var(--iris-space-lg, 20px)` | lg |
| 21–28px | `var(--iris-space-xl, 24px)` | xl |
| 29–40px | `var(--iris-space-2xl, 32px)` | 2xl |
| 41–56px | `var(--iris-space-3xl, 40px)` | 3xl |
| 57–72px | `var(--iris-space-4xl, 48px)` | 4xl |
| >72px | `var(--iris-space-5xl, 64px)` | 5xl |

- **多值简写必须逐个值映射**：`'6px 12px'` → `'var(--iris-padding-sm, 6px) var(--iris-padding-md, 12px)'`；`'8px 10px'` → `'var(--iris-space-xs, 8px) var(--iris-space-sm, 12px)'`；`'2px 6px'` → `'var(--iris-space-xxs, 4px) var(--iris-padding-sm, 6px)'`；`'2px 0'` → `'var(--iris-space-xxs, 4px) 0'`。
- **数字型同样处理**：`gap: 6` → `gap: 'var(--iris-space-xs, 8px)'`（数字 6 视为 6px）。
- rem/em 换算 1rem=16px 后按表映射：`0.125rem 0.5rem`(2px 8px) → `'var(--iris-space-xxs, 4px) var(--iris-space-xs, 8px)'`。
- 已是 `var(--iris-gap-*)` / `var(--iris-padding-*)` 的**不要改**（旧 token 保留）。
- CSS 数值（svelte/vue style 块）同样替换；0 值保留 `0`（reset 场景）。

## 3. 阴影映射（硬编码 boxShadow → token）

| 场景 | 替换 |
|---|---|
| Dialog/Drawer 级大浮层（`0 24px 48px -16px` 系） | `var(--iris-shadow-xl)` |
| Menu/Dropdown/Popover/Select 弹出/Toast/Tour（`0 8px 24px` 系） | `var(--iris-shadow-lg)` |
| BackTop/FloatButton/Card/Ribbon 小件（`0 2–6px` 系） | `var(--iris-shadow-md)` 或 `var(--iris-shadow-sm)`（就近） |

- **含 `var(--iris-*)` 的复合阴影（focus ring 如 `0 0 0 3px var(--iris-background)`）豁免，不改。**

## 4. fallback 漂移修复（var(--iris-x, FALLBACK)）

- fallback 值必须与 `packages/tokens/src/light.ts` 的 token 定义**完全一致**。
- 常见漂移：`--iris-success, #16a34a` → `#10b981`；`--iris-border, #e5e7eb` → `#e2e8f0`；
  `--iris-muted, #6b7280` → `#64748b`；`--iris-primary, #2563eb` → `#6366f1`；
  `--iris-info, #0ea5e9` → `#3b82f6`；`--iris-danger, #e5484d` → `#ef4444`；
  `--iris-surface, #f5f5f5` → `#f8fafc`；`--iris-muted, #888` → `#64748b`。
- 以 light.ts 实际定义为准（运行 `grep "'iris.xxx'" packages/tokens/src/light.ts` 核对）。
- 修复 = 只改 fallback 值，**不动 var 名与结构**。

## 5. 未知 token（fallback 存在但 token 未定义）

- `--iris-cal-today-bg`（#6366f1）→ `var(--iris-primary, #6366f1)`
- `--iris-dashboard-widget-bg` / `--iris-kanban-card-bg`（#fff）→ `var(--iris-surface, #f8fafc)`
- `--iris-danger-light`（#fee2e2）→ `var(--iris-danger-subtle, #fef2f2)`
- `--iris-primary-subtle` 为 ADR-006 派生 token，**保留不动**。

## 6. 裸 hex 修复

- 背景/表面 `#fff` / `#ffffff` → `var(--iris-surface, #f8fafc)`；前景文字 `#fff` → `var(--iris-foreground, #0f172a)`。
- 语义色（成功/危险等）→ 对应 `var(--iris-*)` token；无法判断语义时用
  `var(--iris-muted)` 系。
- ColorPicker/图标 SVG 内部色值**豁免**（已由审计脚本排除目录）。

## 6b. 彩色表面上的前景（对比度纪律）

- **用户着色表面**（calendar 事件 chip 等任意色背景）上的文字/图标 →
  `var(--iris-on-color, #ffffff)`（light）/ `#0b1020`（dark，随主题翻转）。
  **禁止**使用 `var(--iris-primary-foreground)`（那是 primary 专属）。
- **warning 背景**上的文字（kanban WIP badge 等）→
  `var(--iris-warning-foreground, #78350f)`（light）/ `#451a03`（dark），
  保证 AA 对比度。

## 6c. 插件注册 token 的消费纪律（嵌套 var 间接）

插件 core 注册的语义 token（`--iris-cal-today-bg`、`--iris-dashboard-widget-bg`、
`--iris-kanban-card-bg`、`--iris-kanban-gap`、`--iris-dashboard-gap`、
`--iris-pro-table-selected-bg`）**必须被渲染层消费**，否则宿主覆盖静默失效。
消费写法用**嵌套 var**：

```
var(--iris-cal-today-bg, var(--iris-primary, #6366f1))
var(--iris-dashboard-widget-bg, var(--iris-surface, #f8fafc))
var(--iris-kanban-card-bg, var(--iris-surface, #f8fafc))
var(--iris-kanban-gap, var(--iris-space-sm, 12px))    // 或 gap-lg 16px，与注册一致
var(--iris-pro-table-selected-bg, var(--iris-surface-selected, #eef2ff))
```

这样宿主可覆盖、默认回退到语义 token。已注册但零消费者的 token 必须恢复消费；
无法恢复的删除注册。

## 7. 纪律

- 只改 `packages/<fw>/src/**` 与 `packages/plugin-*/src/<fw>/**` 源码；**禁止**改
  dist/、*.tgz、node_modules、manifest.json、llms.txt。
- 不改变组件 props/事件/行为；不改测试断言依赖的类名与 DOM 结构。
- 修复后运行 `pnpm --filter @iris-ui-kit/<fw> test` 与 `typecheck`（Svelte 为
  `pnpm --filter @iris-ui-kit/svelte test` + `svelte-check`），确保全绿。
- 若某处替换会破坏测试快照/断言，调整测试以匹配新 token 值（这是有意统一）。
- 完成后运行审计验证：`python /home/u1/ai-batch-runner/projects/iris-ui/scripts/iris-ui-spec.py --mode all --pkg <fw>`，报告剩余违规数。
