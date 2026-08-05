# Anti-Patterns — AI UI Generation 规范资产（反例清单）

审查 Agent 与生成 Agent 都必须核对本清单。**每一条都是验证器或
Review Agent 的硬检查项。**

## 1. 间距类（uispacing 验证器机械拦截）

```dart
// ❌ SizedBox(height: 3) / EdgeInsets.only(left: 13) / padding: 19
// ✅ 只用 4/8/12/16/20/24/32/40/48/64
```

## 2. 颜色类（uicolor 验证器机械拦截）

```tsx
// ❌ style={{ color: '#1296db' }} / Color(0xFF444555)
// ✅ theme.colors.primary / colorTokens.text
```

## 3. 样式类（uistyle 验证器机械拦截）

```tsx
// ❌ style={{ marginLeft: 13, padding: 4 }}
// ✅ 平台样式机制：Tailwind 工具类 / CSS Module / styled 组件 / Theme
```

## 4. 结构类（Review Agent 检查）

- ❌ div 套 div 套 div 搭页面 → ✅ Page/Card/Section 语义组件
- ❌ 组件顺序随意（Header 下面直接 Table 再 Button）→ ✅ 按
  `layout-patterns.md` 骨架
- ❌ 只实现成功路径 → ✅ 每个数据区有 loading/empty/error
- ❌ 按钮显示只看 `status !== 'completed'` → ✅ 业务状态 × 权限 ×
  数据条件 × 系统状态共同决定

## 5. 交互类（Review Agent 检查）

- ❌ 保存/提交/发布混为同一个按钮
- ❌ 删除无确认；驳回不要求原因
- ❌ 批量操作只提示"失败"不报明细
- ❌ 长任务全屏 loading 遮罩
- ❌ 保存失败后清空表单
- ❌ 离开编辑页不提示未保存
- ❌ 操作后无反馈、无下一步

## 6. 视觉类（visual_reviewer 检查）

- ❌ 一屏十几种图表/卡片堆叠
- ❌ 状态只用颜色不用文字
- ❌ 营销页大留白用在 ERP 表格页（密度错配）
- ❌ 特效动画阻碍内容阅读、无跳过入口
- ❌ 颜色无业务含义（success/warning/danger 混用）

## 7. 技术类

- ❌ 魔法数字（间距/圆角/字号 15/17/18/19/21/26）
- ❌ 硬编码颜色值
- ❌ 未复用公共组件（重复实现 Card/Button）
- ❌ 缺少 TypeScript 类型 / 状态管理混乱
