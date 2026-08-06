# Async & Data Reviewer — sverpweb-stable 前端异步正确性对抗审查

审查对象：`~/workspace/job/sverpweb-stable/apps/web`（Vue 3 + TSX + iris-ui 0.2.x，BFF 走 `utils/http.ts`）。范围：请求频率控制、竞态防护、重复提交、重试矩阵、状态模型、生命周期、数据一致性、错误恢复。对照 `ui-specs/engineering/async-data.md`、`form-table-state.md`、`error-recovery.md`、`defect-patterns.md` 四份决策表逐条核验，并以 `apps/api` 路由源码与 `packages/types` 契约为佐证。

**本次实际执行**：全量源码走查（`utils/http.ts`、14 个页面、6 个 store/协调器、3 个 APS 设置编辑器、SettingsSelect）；grep 全树确认 `AbortController/signal/Idempotency/retry/Retry-After/debounce/throttle` **零使用**；契约核验 `packages/types/index.ts:840-843`（SaveRequest 无 version）、`apps/api/src/routes/quality-inspections.ts:241-370`（PUT 无条件覆盖）；测试文件扫描确认 **0 个测试引用 fetch/api**（现有测试全部是纯函数/结构断言）。

---

## 1. Attack checklist 逐项结论

| # | 检查项 | 结论 |
|---|---|---|
| 1 | 请求频率 | ✅ 搜索输入均走显式"查询"按钮（符合决策表"用户明确点击查询 → 直接请求"），无需要 debounce 却缺失的场景；无乱用 debounce |
| 2 | 竞态防护 | ⚠️ **大部分有**（12 处 requestId/version 守卫），**ContentsPage、BsPricingPage 三处缺失**（H3/H4）；loading 布尔单独不作为竞态依据 |
| 3 | 重复提交 | ⚠️ 全部写操作有 UI 锁（saving/deleting/uploading），但**无幂等键、无 timeout-uncertain 处理**（H1/H2） |
| 4 | 重试矩阵 | ❌ **全缺**：http.ts 无超时、无 GET 重试、无 429 Retry-After、无 401 刷新重放（H1/M5） |
| 5 | 状态模型 | ✅ 各页 loading/error/notice 分域，无全局 loading 控多请求；APS 排程用版本号乐观并发 |
| 6 | 生命周期 | ✅ SettingsSelect 监听成对释放 + rAF cancel；AppShell/ApsViewPage fullscreenchange 卸载清理；无 setInterval |
| 7 | 数据一致性 | ⚠️ 派生值均 computed（好）；但**检验单保存无版本控制 → 静默覆盖**（H5）；本地回写（upsertArrangement）以服务端响应为准（好） |
| 8 | 错误恢复 | ⚠️ 409 有明确文案（aps 域）但无"取最新→对比→决策"流；**无 401/403/404/409/422/429 分类映射**，统一走 `cause.message`（M4）；MediaPage 静默吞错（M1） |

---

## 2. Findings（按严重度）

### High

| # | 缺陷模式 | 证据 | 根因 | 修复（引用决策表） | 能抓住它的测试 |
|---|---|---|---|---|---|
| H1 | **timeout-uncertain + duplicate-submit** | `utils/http.ts:9-35`（fetch 无 AbortController/超时）；全树 grep 零 `AbortController/signal`；`QualityInspectionDetailPage.tsx:472 save()`、`:525 upload()`、`ContentsPage.tsx:160 save()`、`BsPricingPage.tsx:390 save()` 均 POST 无幂等键 | 写请求超时后服务端可能已成功，客户端无 uncertain 状态、无查询操作结果路径；用户重试 → 重复创建检验单/附件/内容/报价 | async-data.md §4 五项全做：① UI 锁已有；② `Idempotency-Key`（客户端生成，如 `operationId`）随 POST 发送；③ 超时**不自动重试**；④ 页面刷新后轮询 GET 该 key 查询结果；⑤ uncertain 状态（写入状态机加 `uncertain` 分支，error-recovery.md AppError `timeout` 类型） | 用两个 Deferred Promise mock：POST 挂起 → 超时 → 断言 UI 进入"结果未知"而非失败；重试 → 断言请求头幂等键相同、服务端去重后不产生第二条记录 |
| H2 | **async-race（请求竞态）** | `ContentsPage.tsx:146 load()` 无 requestVersion；分页组件未传 `disabled={loading}`；`setPage`(`:179`) 与搜索可并发 | 快速翻页/搜索，旧响应晚到覆盖新响应 → 显示上一页数据（pagination-filter-mismatch 同源） | async-data.md §3 强制：加 `let requestVersion` 守卫（与其他 12 处页面同款）；分页 `disabled={loading}`（SystemUsersPage 已有范例） | 两个 Deferred Promise：page=2 请求慢于 page=3，断言最终 items 是 page=3 数据、total 一致 |
| H3 | **async-race（编辑态被旧响应覆盖）** | `BsPricingPage.tsx:249 search()`、`:283 loadDetail()`、`:296 loadOverview()` 均无 requestId；三函数共享 `document/currentId` 可变状态 | 搜索在飞时双击总览行 / 二次搜索 → 旧响应后到，把用户正在编辑的 dirty 表单整体替换；`loadDetail` 不咨询 `dirty` 标志 | async-data.md §3：三个函数共用一个 requestId 计数器，响应提交前校验；`loadDetail` 在 `dirty.value` 时先走 discard 确认（复用 `requestBrowseMode` 已有模式） | 编辑表单置 dirty → 触发慢速 search → 断言 dirty 表单未被替换且弹确认 |
| H4 | **409 静默覆盖（并发编辑无版本）** | `packages/types/index.ts:840-843`：`QualityInspectionSaveRequest` **无 version 字段**；`apps/api/src/routes/quality-inspections.ts:283-321` PUT 无条件 `inspections.update`；前端 `QualityInspectionDetailPage.tsx:472` 也不发版本 | 两人同时编辑同一检验单 → 后保存者整体覆盖，无 409、无冲突提示 | error-recovery.md §2：SaveRequest 增加 `version`（对齐 APS 排程/计划日志的既有模式）；保存带版本；后端 409 返回 `version_conflict`；前端 409 时"获取最新版本 → 展示冲突字段 → 覆盖/合并/取消"，禁止静默覆盖 | 契约测试：A 载入 v1 → B 保存 v2 → A 用 v1 保存 → 断言 409 且 UI 出现冲突处理而非成功提示 |
| H5 | **N+1 请求风暴** | `QualityInspectionPage.tsx:248-256`：每行 `listQualityInspectionFiles` 并发（pageSize=100 时单次页面加载 100 个请求）；`:332-352 exportStatistics()` 硬编码 `page:1, pageSize:100` | 列表页逐行拉附件；导出只取前 100 条即声称"一品一单统计"（form-table-state.md 口径：导出/汇总必须声明当前页 vs 全量） | ① 服务端列表响应内联附件摘要或提供 `ids=` 批量端点（消除 N+1）；② 导出循环分页拉全量并对总数>100 时提示，或导出走服务端生成 | 集成测试断言页面加载请求数 ≤ 3（列表+统计+零附件）；导出 mock 返回 total=150 → 断言发起第二次分页请求 |

### Medium

| # | 缺陷模式 | 证据 | 根因 | 修复 | 能抓住它的测试 |
|---|---|---|---|---|---|
| M1 | **静默吞错** | `MediaPage.tsx:127` `catch { items.value = [] }` | 失败与空数据不可区分，无错误信息、无重试入口 | async-data.md 禁止事项 #3：catch 置 error 文案 + 表格 error 态/重试按钮（SystemRoutesPage 已有 error slot + 重试范例） | mock 500 → 断言页面显示错误而非空表 |
| M2 | **409 无取最新流程** | `ApsArrangedPlanDialog.tsx:360-370`、`BsPricingPage.tsx:407`、`SystemRoutesPage.tsx:570`：409 仅 toast 文案，草稿仍持旧版本 → 再次保存必然再 409，需手动刷新 | 版本冲突后无"获取最新版本"联动 | error-recovery.md §2：409 分支自动 `load()`/`getBsStandardPricing(id)` 刷新本地版本并提示差异，而不是让用户手动点刷新按钮 | mock 第一次 PUT 409 → 断言自动发起 GET 且草稿版本被更新 |
| M3 | **form-dirty-state-loss** | `QualityInspectionDetailPage.tsx`（status="modified" 但不拦路由/关闭）；`BsPricingPage.tsx`（仅拦截 browse 切换，`confirmBrowse` 有 discard 确认；路由离开无拦截）；`ContentsPage.tsx`（drawer 关闭即弃）；`ApsArrangedPlanDialog.tsx`（dirtyCount>0 时关闭按钮无确认，:652 附近） | 只跟踪 dirty，未接 beforeunload/路由守卫/关闭拦截 | defect-patterns #9 + form-table-state.md：`onBeforeRouteLeave` + `beforeunload` 双拦截（dirty 时提示）；弹窗 `closeOnOutsideClick={!dirty}` 或关闭前确认 | 编辑后触发路由离开 → 断言出现确认且取消后状态保留 |
| M4 | **错误分类缺失** | 全站 `errorMessage()` 一律 `cause.message` 透传；无 401/403/404/409/422/429/network/timeout 分支 | BFF 错误未映射为用户可执行下一步 | error-recovery.md §1 统一 AppError 判别联合：`http.ts` 按 status 分类；页面按类型渲染不同提示与动作（401→重登、409→刷新对比、429→稍后重试、timeout→查询结果） | 单元测试：mock 各 status，断言错误消息文案按类型生成而非透传 |
| M5 | **无 GET 重试矩阵** | `utils/http.ts` 全文件；`auth.ts refresh()` 从未被 http 层调用 | 网络抖动/5xx 直接失败；429 无 Retry-After；会话过期无刷新重放 | async-data.md §5：GET 网络/5xx 有限重试（指数退避+抖动，最多 2-3 次）；429 读 `Retry-After`；401 经 `auth.refresh()` 重放一次后仍失败才登出 | mock fetch：第一次 500、第二次 200 → 断言最终成功且重试间隔带退避；mock 401+refresh 成功 → 断言原请求重放 |
| M6 | **部分成功报告缺失** | `QualityInspectionDetailPage.tsx:537-544` 上传循环：第 2/3 个文件失败时仅整体报错，已成功文件无明细；`ApsArrangedPlanDialog.tsx:374-384 saveAll`：首个失败即 break，无"已保存 N / 失败 M"汇总 | 批量操作无逐项结果报告 | error-recovery.md partial success 行：上传循环逐文件 catch 并汇总"成功 x / 失败 y（文件名）"；saveAll 记录失败清单并继续或汇总展示，草稿保持 dirty 可重试（保留现有 break 语义 + 汇总提示） | mock 3 个上传 1 个失败 → 断言提示含成功/失败明细、失败文件仍可重传 |

### Low / Info

| # | 缺陷模式 | 证据 | 说明 |
|---|---|---|---|
| L1 | 分页总数近似 | `SystemAuditPage.tsx:294-297` `total = offset + items.length + (hasMore ? 1 : 0)` | 末页边界可能差 1；建议后端返回精确 total |
| L2 | 测试空白 | 现有测试 0 引用 fetch/api；`BsPricingPage.test.ts`/`SystemRoutesPage.test.ts` 仅纯函数断言 | H1–H4 均无回归测试可执行；先红后绿需先补（对齐 testing_reviewer 的 B2 要求） |
| L3 | 跳转竞态豁免 | `ApsViewPage.tsx:252 load()` 成功后 `router.replace` 改 URL | 页面不 watch query，无循环；但刷新页面时 URL 与初始 filters 一致性依赖服务端回写，属可接受 |

---

## 3. 正面事实（非问题，避免过度处方）

- **runtime-route-coordinator.ts**：generation 计数 + in-flight 共享 + 过期响应判 stale + 会话隔离，是本库竞态防护的标杆实现（`runtime-route-guard.ts` 双保险重试 stale）。
- 12 处页面/store 已具备 requestId/version 守卫（QualityInspectionPage、SystemAuditPage、SystemRoutesPage、SystemUsersPage、SystemRolesPage、ApsPlanPage、ApsViewPage、ApsStatisticsPage、ApsSettingsPage、ApsPlanLogDialog、QualityInspectionDetailPage、runtime-routes store、AppShell refresh）——**ContentsPage/BsPricingPage 是例外而非常态**。
- APS 排程（ApsArrangedPlanDialog）与计划日志（ApsPlanLogDialog）：version 乐观并发 + 409 用户可读文案 + 逐草稿保存锁 + 失败保留 dirty——是 H4/H5 修复的对齐模板。
- 删除末页最后一条回退页码（QualityInspectionPage:322、ContentsPage:231）——pagination-filter-mismatch 已处理。
- SettingsSelect 全局监听按 open 状态成对 add/remove + `onBeforeUnmount` 兜底 + rAF cancel——生命周期纪律到位。
- 全站无 `setTimeout` 模拟数据、无全局 loading 控多请求、派生状态全部 computed 不落本地副本。

---

## 4. 结论

本前端异步工程的**骨架是高质量的**（守卫纪律普遍、生命周期成对、派生状态纯净、APS 域乐观并发正确），但存在三个系统性缺口：**① http 层无超时/重试/幂等/错误分类（H1/M4/M5）——导致超时后重试必然可能产生重复业务记录；② 两个页面竞态裸奔（H2/H3）——ContentsPage 翻页、BsPricingPage 编辑态可被旧响应覆盖；③ 检验单主文档无版本控制（H4）——并发编辑静默覆盖**。后两者是"旧请求还能改状态、重复提交会重复执行"的直接命中，且全部无测试可执行。H1/H4 属于会污染审计账本数据的事实层问题（本仓库审计中间件对每次保存落 audit 事件，重复创建=重复审计事实）。

```
VERDICT: FAIL - 阻塞缺陷：① http.ts 无超时/幂等键/timeout-uncertain（H1），写操作超时重试可产生重复检验单/附件/内容/报价，且审计中间件会落重复事件；② ContentsPage.load 无竞态守卫、分页未禁（H2），旧响应可覆盖新数据；③ BsPricingPage search/loadDetail/loadOverview 无 requestId 且不咨询 dirty（H3），在飞旧响应可覆盖用户编辑中的表单；④ QualityInspectionSaveRequest 无 version、PUT 无条件覆盖（H4），并发编辑静默覆盖。以上四项均无可执行测试（全站测试零 fetch/api 引用）。修复须按 async-data.md §3/§4/§5 与 error-recovery.md §1/§2 决策表落实，并以 APS 排程域的 version+409 模式为对齐模板；本评审为咨询性意见，不构成发布批准。
```
