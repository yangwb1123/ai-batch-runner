# Component Spec — AI UI Generation 规范资产

所有组件必须满足本文件的尺寸、状态与行为要求。数字全部来自
`ui-specs/tokens.json`，不得出现 token 以外的值。

## 1. Button

| 项 | 值 |
|---|---|
| 高度 | 40（紧凑 36） |
| 圆角 | 8 |
| 内边距 | 16 水平 |
| 图标 | 18 |
| 状态 | default / hover / pressed / focus / disabled / loading |
| Loading | 禁用重复点击 |

- 危险操作（删除/作废）用 danger 样式 + 二次确认；与主操作分开摆放。
- 文案描述结果（"提交审核" 而非 "确定"）。

## 2. Input / Select / DatePicker

| 项 | 值 |
|---|---|
| 高度 | 40 |
| Label | 16 |
| Placeholder | 14（次级色） |
| Error | 12（danger 色，内联展示） |
| 状态 | default / hover / focus / disabled / error / readonly |

## 3. Card

| 项 | 值 |
|---|---|
| 内边距 | 20（紧凑 16） |
| 圆角 | 12 |
| 阴影 | level 1（`0 2 8 rgba(0,0,0,0.06)`） |
| 标题 | h3 + margin-bottom 16 |

## 4. Dialog / Drawer

| 项 | 值 |
|---|---|
| Dialog 宽度 | 560（小 420 / 大 720） |
| Dialog 内边距 | 24 |
| 按钮区 | 右下，主操作在右 |
| Drawer 宽度 | 480 |
| 行为 | 遮罩点击关闭、Esc 关闭、焦点圈闭 |

## 5. Table

| 项 | 值 |
|---|---|
| 行高 | 48（紧凑 40） |
| 表头 | 固定 + 次级背景 |
| 对齐 | 数字右对齐 / 文本左对齐 / 状态居中 |
| 金额 | 千分位；日期格式统一 |
| 关键列 | 可固定；大数据量虚拟滚动 |
| 批量选择 | 显示已选数量 + 批量操作栏 |

### 表格列宽与测试字体（实战教训）

- 列宽按测试字体估算：字符 ≈ 13px（Ahem 字体）——'Suspended' 9 字符
  ≈ 117px + 图标 + 内边距，列宽 < 190 会溢出
- 表头 label 包 Flexible + ellipsis（排序图标占位）
- 状态徽章列预留 190px（含图标 + 双编码文字）
- 单元格内容优先单行 ellipsis；长字段（路径/名称）允许 2 行
- 横向滚动容器内 Column 不可用 stretch（无界宽度崩溃）——行容器定宽

## 6. 状态要求（所有组件）

loading / skeleton / empty / error / disabled / hover / pressed /
focused / selected 必须齐全——**AI 最容易漏空态和错误态**。

## 7. 行为要求

- 保存失败**不得**清空表单；失败提示 + 重试入口。
- 批量操作报告：总数、成功数、失败数、失败原因。
- 异步任务（导出/生成）：返回任务编号，页面可继续操作，完成通知。
- 删除：低风险 → 确认 + 回收站 + 可撤销；高风险 → 说明影响范围 +
  输入对象名确认 + 审计日志。
