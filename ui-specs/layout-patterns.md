# Layout Patterns — AI UI Generation 规范资产

本文件定义每种页面类型的**固定骨架**。Agent 不得自由发挥页面结构；
布局由 `layout-patterns` 约束，组件由 `component-spec.md` 约束。

## 1. 列表页 (list)

```
Page Container (padding-x: 24)
├─ Page Header        (title + status + primary action, margin-bottom 16)
├─ Summary Row        (可选：统计卡片组, gap 16)
├─ Toolbar            (search input + filter + batch actions, gap 12)
├─ Data Table         (fixed header, status column, row height 48)
│   ├─ Empty state    (icon + text + action)
│   ├─ Loading state  (skeleton rows)
│   └─ Error state    (message + retry)
├─ Batch Action Bar   (选中后出现：影响数量 + 确认)
└─ Pagination         (总数 + 页码 + 每页条数)
```

## 2. 详情页 (detail)

```
Page Container
├─ Object Header      (title + core status + quick actions)
├─ Basic Info Card    (分组字段, form item gap 20)
├─ Related Sections   (tabs 或并列卡片：关联对象/时间线/操作日志)
└─ Action Footer      (主操作右上或底部，危险操作与主操作分开)
```

## 3. 表单页 (form)

```
Page Container
├─ Header             (title + description + back)
├─ Form Sections      (分组卡片, section gap 32)
│   ├─ 实时校验        (错误内联展示, hint 12)
│   └─ 必填标记        (label 旁 *)
├─ Sticky Action Bar  (保存/取消；保存失败保留输入)
└─ Unsaved Warning    (离开前提示)
```

## 4. 工作台 (workbench)

```
Page Container
├─ Welcome + Role Info
├─ Todo / Urgent       (按紧急程度排序)
├─ Core Metrics Row    (卡片组)
├─ Quick Entries
├─ Recent Records
└─ Notifications
```

## 5. 向导页 (wizard)

```
Stepper (当前步骤高亮 + 可返回)
├─ Step 1: Basic Info
├─ Step 2: Business Config
├─ Step 3: Confirm & Submit
└─ Result (成功/失败 + 下一步)
```

## 6. 通用规则

- **一个页面一个主任务**：主要按钮唯一、视觉等级最高。
- **操作分级**：一级（保存/提交/发布）、二级（预览/导出/暂存）、
  三级（删除/作废/重置）——危险操作不得与主操作同视觉样式。
- **反馈闭环**：触发 → 处理中（禁重复点击）→ 结果 → 解释 → 下一步。
- **详情展示**：简单确认用弹窗；中等复杂度用抽屉（保留列表上下文）；
  复杂编辑/多步骤用独立页面。
- **空/错/载三态**：每个数据区必须实现 loading / empty / error。
