# Form & Table Engineering Spec — 表单与表格状态一致性

## 1. 表单工程

### 字段提交策略（显式）

```ts
type FieldSubmissionPolicy = 'submit' | 'omit' | 'submit-null' | 'preserve-original';
```

- 隐藏字段 ≠ 不提交；禁用字段 ≠ 不参与业务校验；清空 ≠ null
- 必做：初始值回填、同步/异步/跨字段校验、字段依赖显隐、脏状态
  跟踪、离开页面提醒、保存失败保留输入、服务端字段错误映射、
  草稿恢复、提交锁
- 大表单按业务分组拆分，禁止单文件巨型表单组件

## 2. 表格状态一致性

### 选择模式（必须显式区分）

```ts
type SelectionMode =
  | { type: 'current-page'; ids: string[] }
  | { type: 'explicit'; ids: string[] }
  | { type: 'all-matching'; excludedIds: string[] };
```

禁止"表格显示 20 条、全选却操作全部 20000 条"。

### 状态一致性检查清单

- 分页/排序/筛选是否服务端执行
- 切换筛选后是否回到第一页
- URL 是否保存查询条件；刷新后是否恢复
- 跨页选择策略；数据刷新后选择项是否失效
- 删除最后一条后页码是否回退
- 批量操作是否只处理当前页；导出/汇总口径（当前页 vs 全量）
- 排序字段与后端一致

## 3. 状态放置（内聚性）

| 状态类型 | 位置 |
|---|---|
| 服务端数据 | Query/Repository 层 |
| URL 筛选参数 | Router/Search Params |
| 表单状态 | Form 模块 |
| 弹窗开关 | 最接近使用位置 |
| 跨组件业务流程 | Feature model |
| 全局登录/主题 | App store |
| 派生状态 | 计算得到，不重复存储 |
| 临时视觉状态 | 组件局部 |

禁止把所有状态提升到页面层或全局 Store；状态放在能完整拥有其
生命周期的最小边界内。

## 4. 业务状态显式建模

同一流程出现 3 个以上相互关联的布尔状态（isLoading/isSuccess/isError/
isSubmitting...）必须改用判别联合或状态机：

```ts
type ApprovalState =
  | { status: 'idle' }
  | { status: 'confirming'; orderId: string }
  | { status: 'submitting'; orderId: string }
  | { status: 'success'; orderId: string }
  | { status: 'conflict'; orderId: string; message: string }
  | { status: 'failed'; orderId: string; error: AppError };
```

业务条件集中到 Policy，禁止在模板里散落复制
`hasPermission && order.status === 'pending' && ...`。
