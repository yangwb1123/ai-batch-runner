# Spacing Spec — AI UI Generation 规范资产

本文件约束所有 Agent 生成的 TSX / Dart / Vue 页面的间距。**违反即被
`uispacing` 验证器拒绝，不落盘。**

## 1. 8pt Grid

所有间距值必须来自以下 token 集：

| Token | 值 | 典型用途 |
|---|---|---|
| `space.0` | 0 | 重置（仅此场景） |
| `space.xxs` | 4 | 图标与文字间隙、标签内边距 |
| `space.xs` | 8 | 紧凑元素间隙、表格单元格内距 |
| `space.sm` | 12 | 按钮组间隙、toolbar 间隙 |
| `space.md` | 16 | 列表项间距、表单字段间距、卡片内小分区 |
| `space.lg` | 20 | 卡片 padding、表单 item 间距 |
| `space.xl` | 24 | 页面左右 padding、卡片之间、Section 内间距 |
| `space.2xl` | 32 | Section 上下间距、弹窗 padding |
| `space.3xl` | 40 | 页面主区块间距 |
| `space.4xl` | 48 | 页面大区块分隔 |
| `space.5xl` | 64 | 页面级留白（宽松模式） |

## 2. 关键尺寸（组件级）

- Button 高度：**40**；Input 高度：**40**
- Card padding：**20**（compact 模式 16）
- Dialog padding：**24**
- Page 左右 padding：**24**
- Section 上下间距：**32**
- Title margin-bottom：**16**
- Form Item 间距：**20**
- Card 之间：**24**
- 列表 Item：**16**
- Toolbar 按钮间隙：**12**

## 3. 禁止事项（验证器会拦截）

以下写法**一律禁止**，无论值是否"看起来合理"：

```dart
// ❌ 禁止：魔法间距
SizedBox(height: 3)      // 3 不在 token 集
SizedBox(height: 7)
EdgeInsets.only(left: 13)
padding: EdgeInsets.all(19)
```

```tsx
// ❌ 禁止：任意数值
margin: 11
padding: 13
gap: 7
style={{ marginLeft: 18 }}
```

```tsx
// ✅ 正确：使用 token
<Box sx={{ p: 2 }} />                     // theme spacing (8px)
style={{ marginBottom: theme.spacing(2) }}
EdgeInsets.all(16)                        // 仅限 token 值
SizedBox(height: 24)
```

## 4. 判断规则

- 只允许 `{0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64}`（TSX/Dart/Vue 通用）。
- 0 仅允许用于 reset（`margin: 0` 清默认样式）。
- 语义化命名优先：`space.md` 优于裸 `16`（若平台支持 token 引用）。
- compact 模式 ≠ 没有间距：密度差异通过 token 档位表达（如 tableRow
  48→40），**不得**出现 5/9/14/17/19/21/26 等非 token 值。

### Iris CSS 命名间距变量

Iris CSS 消费端使用命名变量：`--iris-space-xxs`、`xs`、`sm`、`md`、
`lg`、`xl`、`2xl`、`3xl`、`4xl`、`5xl`。数字本身属于 8pt 刻度并不代表
CSS 变量存在；`var(--iris-space-4)` 仍是未定义变量，会在浏览器计算阶段使
`gap`/`padding` 等声明失效。必须按以下映射改为命名变量：

`1→xxs`、`2→xs`、`3→sm`、`4→md`、`5→lg`、`6→xl`。

`check-ui-spec.py --mode spacing` 会把这类 numeric Iris spacing 引用作为
间距违规拦截；不要通过声明数字别名来绕过门禁。

Tailwind-like `className` utilities are checked too: numeric scales use the
default `0.25rem` scale (`p-2` → 8px, `p-4` → 16px), while arbitrary values
such as `p-[13px]` must resolve to the same token set. Use `uifont` for
typography values; it derives the allowed 12/14/16/20/24/32px family from
`ui-specs/tokens.json`.
