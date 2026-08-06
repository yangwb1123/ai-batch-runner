Verification complete. I have everything needed — source-verified findings plus actual frontend gate runs (vue-tsc 0 errors, 115 tests green). Here is my review deliverable.

---

# Frontend Engineering Review — ERP 接入平台六件套路线图（revision.md 前端面）

**角色**：frontend_engineer · review2 对抗审查 · 范围：`docs/snaplink-platform/erp/revision.md`（233 行）的前端工程面
**目标平台**：sverpweb-stable `apps/web`（Vue 3.5 + Vue JSX `.tsx` + iris-ui `0.2.18` + pinia + vue-router + vue-i18n）+ Hono BFF `apps/api`（Bun + SQLite）
**性质**：advisory review，不改动任何文件；结论均为提案，按证据等级标注后由人工提升。

---

## 1. 分类与假设（Classification + Assumptions）

| 维度 | 判定 |
|---|---|
| 页面类型 | ERP 生产系统：列表页（媒体库、审计日志）、详情+附件上传（质检单）、复杂表单（质检明细编辑）、登录页、全局壳（AppShell） |
| 平台 | Vue 3.5 + JSX/TSX（非 SFC，`@vitejs/plugin-vue-jsx`）、iris-ui 0.2.18、Pinia、BFF 同源 API（cookie + CSRF） |
| 密度 | 高信息密度桌面端（erp profile：表格为核心、紧凑间距、motion 0） |
| 风险等级 | **高**：质检附件（生产事实文件）、审计展示、审批通知；审计 actor 真实性受前端链路影响 |
| 主任务 | 质检单编辑+附件上传（主）、媒体库管理（辅）、审计日志查询（运维辅）、会话生命周期（横切） |

**假设（推断，未实机验证）**：
- A1（推断）：质检附件是生产主路径，媒体库（MediaPage）是辅助页 —— 依据：`QualityInspectionDetailPage` 的附件上传是业务表单内嵌流程，MediaPage 仅独立"媒体库"页；**需人工确认两者业务权重**（影响 M3 范围排序）。
- A2（推断）：presign 直传方案下 vault 与 ERP 非同源，PUT 不能带 cookie，需 CORS + 签名认证 —— 依据 presign 语义；vault 侧 CORS/TTL 配置未实机核验（vault 仓库本机不可达，**未验证**）。
- A3（推断）：审批流程的前端页面存在（"审批"被 M2 列为 E2E 三类之一），但本次未定位到独立审批页（`pages/` 无 approval 命名）；审批可能是表单内状态流转。**需人工确认**。

---

## 2. 已核验前端事实（全部本次实机验证）

### F1 [High] M3"前端零改动"与"50MB 直传不落 BFF 内存"自相矛盾，且范围漏了第二个存储

- **实锤**：前端上传走 `apps/web/src/utils/quality-inspections.ts:245` `uploadQualityInspectionFile` —— FormData POST `/api/quality/inspection-files`（cookie + CSRF 头），`QualityInspectionDetailPage.tsx:525` 顺序循环逐文件。
- **实锤**：内容存储有**两个独立实现**，M3 只写了 MediaStore：
  - `apps/api/src/media/store.ts`（媒体库，`/api/media/*`）
  - `apps/api/src/quality/sqlite-store.ts`（质检附件，自带 `content` 列 + `FILE_CONTENT_ROUTE_PREFIX`，`/api/quality/inspection-files/*`）
  质检附件**不在 MediaStore 接口之下**，M3 的"MediaStore 新实现"不覆盖生产主路径。
- **实锤矛盾**：`media.ts:80-100` 上传路由 `file.arrayBuffer()` 全量载入内存（50MB 上限 → 每请求峰值 50MB）；质量附件路由同构。若用 presign 直传消除 BFF 缓冲，前端必须改为**两阶段**（申请 presign → 跨域 PUT → 确认），cookie/CSRF 认证失效、进度/取消/错误语义全变——"前端零改动"不成立；若保前端零改动，则只能 BFF 流式透传（multipart → stream），那便不是"presign 直传"语义。**二选一必须显式写入路线图**（见 G1）。

### F2 [High] 前端会话过期无全局处理（N7 之下是真实高频路径）

- **实锤**：`apps/web/src/utils/http.ts` 无响应拦截、无 401 全局处理、**无超时、无 AbortController**；`inspectionErrorMessage`/`SystemAuditPage.errorMessage` 只回显 `cause.message`，无状态→恢复动作映射。
- 唯一 401 处理在**路由层**（`runtime-route-coordinator.ts:119`）；页内 API 401（长开表单页、附件上传中途）→ 用户看到"操作失败"而不是重新登录，且无 returnTo 保留。
- N7 已裁定会话最长 1h —— 这正是"页内 401"高频触发条件。路线图 N1–N3 全在后端，**前端侧无对应工作项**。

### F3 [Medium] 上传交互链缺进度/取消/部分成功报告/幂等（现状缺陷，M3 必须一并修）

- **实锤**：`uploading` 单布尔锁 + 顺序循环；无单文件进度、无取消、无超时、无幂等键、无不确定（uncertain）语义；多文件部分成功无汇总报告（第 2 个失败时列表已含第 1 个，但提示是笼统错误）。
- 违反 `async-data.md` 决策表（mutation 需 pending lock + 失败路径 + 部分成功报告）与 `component-spec.md` §7（批量操作报告总数/成功/失败/原因）。

### F4 [Medium] 通知入口是静态占位，M5 前端范围被低估

- **实锤**：`AppShell.tsx:839` 铃铛下拉是硬编码 `暂无通知`（disabled 项），无任何状态/传输/未读逻辑。
- M5 实际前端工作量：通知列表/未读徽标/深链跳转（审批上下文）/传输生命周期（SSE 或轮询退避）/404 清理——路线图 M5 只标"偏好 UI 位置待定"，且"审批触发通知"深链存在并发处理（他人已审批）的 409/404 链路未定义。

### F5 [Medium] SystemAuditPage 把本地 outbox 呈现为"审计日志"

- **实锤**：`SystemAuditPage.tsx` 读 `/audit/events`（outbox 记录，含 `pending/publishing/published/failed` 投递状态，还有 flush 重试按钮）。
- 真源裁定（§2.4）：业务永不读 outbox 当事实。但该页标题即"审计日志"——L2 后此页语义变为**投递队列运维视图**，不标注会误导用户/审计员把 outbox 当账本事实。M2 验收应含此 UI 语义更新（标题/数据源标注 + governance console 链接）。

### F6 [Low-Medium] MediaPage 错误态被吞 + 客户端全量分页

- **实锤**：`MediaPage.tsx` `load()` catch `items.value = []` —— 错误显示为空表，无错误消息、无重试（只有 loading/empty，无 error 态，违反 anti-patterns §4"只实现成功路径"）。
- **实锤**：`media.list()` 一次全量拉取 + computed 切片分页（无服务端分页参数）。M3 迁 vault 后媒体库规模增长时此策略无上限声明。

### F7 [Info/Verified] demo 模式无前端暴露面；N2 后端处置可具体化为 fail-closed

- **实锤**：`env.ts:53` `IDENTITY_MODE === "demo"` 才启用 `DemoIdentityClient`（opt-in，非默认）；`LoginPage.tsx` 仅 SSO 跳转（`auth.beginLogin`），无 demo 登录表单。N2 处置建议措辞升级：`IDENTITY_MODE=demo && NODE_ENV=production` → 启动即失败（fail closed），并删除 `DEMO_PASSWORD` 死配置。

### F8 [Info/Verified] iris-ui 锁版本现状已符合 owner #7

- **实锤**：`apps/web/package.json` `@iris-ui-kit/vue: "0.2.18"`、`@iris-ui-kit/core: "0.2.0"` 精确锁定（无 `^`），pnpm-lock.yaml 存在。owner #7 只需补"升级触发条件 + 验证门禁"（升级后 vue-tsc + vitest + 视觉回归）。

---

## 3. 状态模型与交互链（含失败路径）

### 3.1 上传链（mutation，现状 → M3 两种候选）

**现状（Verified）**：

```
选择文件 → [前置: canWrite 权限 + 检验单已保存] → uploading 锁 →
  per-file FormData POST（顺序循环）→ 成功: files 前插 + notice
  → 失败: error 文案（无状态映射）→ finally: 清空选择
缺口：无进度/取消/超时/幂等/uncertain；部分成功无汇总
```

**M3 候选 B（presign 直传，若采纳必须显式建模）**：

```ts
type UploadFlow =
  | { status: "idle" }
  | { status: "signing"; file: File }                       // POST presign（CSRF）
  | { status: "uploading"; file: File; progress: number }   // XHR PUT → vault（签名认证，无 cookie）
  | { status: "confirming"; key: string }                   // POST confirm（幂等键 = key）
  | { status: "success"; key: string }
  | { status: "presign_expired"; file: File }               // 403 → 重签一次（同 key），仍败则 failed
  | { status: "failed"; file: File; error: AppError }
  | { status: "uncertain"; key: string }                    // 超时/断网后查询 key 状态，禁止盲目重试
```

- 失败映射：presign 403 → 重签一次（同 key 幂等）；PUT 中断网 → 同 key 重试（PUT 同 key 覆盖语义需 vault 确认，推断）；confirm 409（key 已注册）→ 视为成功（幂等确认）；取消 → AbortController 中止 PUT，孤儿 key 由回填对账 job 清理。
- 多文件：per-file 状态机 + 汇总报告（总数/成功/失败/原因），禁止当前"整批一个布尔"。

### 3.2 会话过期链（横切，N7 触发）

```
页内 API 401 → [现状] APIError 冒泡 → 页面 error 文案（无恢复）
[要求] 统一拦截：401 → auth.bootstrap()（静默探测）
  ├─ 会话有效（短暂刷新窗口）→ 重放原请求一次（幂等读可重放；写操作进入 uncertain，需确认）
  └─ 会话失效 → 保留表单草稿（form-table-state: 脏跟踪 + localStorage 草稿）→
     引导重新登录（returnTo 保留当前路由 + 筛选条件）→ 登录后恢复
```

### 3.3 通知链（M5，前端规格草案）

```ts
type NotificationState =
  | { status: "idle" }
  | { status: "connecting"; attempt: number }        // SSE 或轮询启动
  | { status: "live"; cursor: string }               // 未读数 = cursor 之后
  | { status: "polling"; backoffMs: number }         // 降级：30s → 120s 封顶，指数退避+jitter
  | { status: "disabled" }                           // 权限/配置关闭，静默隐藏铃铛

深链：通知项 → 审批页(带上下文) → 目标不存在/已被处理 → 409/404 →
  刷新列表 + 明确提示"该审批已被他人处理"，禁止静默
```

- 传输决策（async-data 决策表）：aero-im 若支持 SSE → SSE（含重连退避 + `visibilitychange` 暂停）；否则轮询退避。资源生命周期：SSE close 于 unmount/登出/会话失效。
- 竞态防护：未读计数/列表刷新用游标版本比对，禁止仅 loading 布尔。

### 3.4 审计页语义（M2 附带）

- 页面标注数据源："本地投递队列（运维视图）"，账本事实链接 governance console；`actorDepartmentSnapshot` 可空占位渲染（`—`）已做对，保持。

---

## 4. Implementation：路线图必须并入的前端工作项（G1–G6）

> 本阶段为对抗审查，不改文件。以下为 revision.md 需增补的前端规格与验收标准（Proposed）。

| # | 增补项 | 挂靠里程碑 | 验收标准（可执行） |
|---|---|---|---|
| G1 | **M3 二选一显式化**：A) BFF 流式代理（multipart→stream 透传，前端零改动成立，附内存断言）；B) presign 直传（附前端改动清单 §3.1 状态机）。**范围扩到质检附件存储**（`quality/sqlite-store.ts`），或显式声明"质检附件本地保留、M3 仅媒体库"（克制，但必须写明） | M3 | 路线图文本含二选一裁决 + 两个 store 的迁移矩阵（media / quality × 迁移/保留） |
| G2 | **前端会话过期处理**：http 层 401 拦截 + 静默探测 + returnTo 保留 + 表单草稿保留；写操作超时 → uncertain 查询 | M1（与 N7 对齐） | 页内 API 401 用例：长开表单页过期 → 不丢输入 → 重新登录后恢复筛选/页码 |
| G3 | **上传交互升级**（与 G1 方案绑定）：进度/取消/超时/幂等/部分成功报告 | M3 | 多文件上传含 1 失败：报告"2 成功 1 失败+原因"；双击不重复提交（请求仅一次）；取消后无 setState-after-unmount |
| G4 | **审计页数据源标注** + governance console 链接 | M2 验收 | 页面含"投递队列（运维视图）"标注；L2 后无把 outbox 当账本事实的文案 |
| G5 | **MediaPage 三态修复**：error 态（消息+重试）与 empty 态分离；`media.list()` 分页策略声明（服务端分页或全量上限） | M3 顺带 | `load()` 失败显示错误+重试按钮，非空表；列表接口契约注明分页语义 |
| G6 | **M5 通知前端规格**：§3.3 状态机 + 深链 409/404 处理 + 未读竞态防护 + SSE 生命周期 | M5 | 审批被他人处理后点通知 → 明确提示不静默；通知服务宕机 → 业务页面无感、无重试风暴（与路线图 best_effort 一致） |

**不改动项（克制确认）**：前端不做 demo 登录入口（F7，无暴露面）；不新增全局 Store 承载通知（M5 通知状态放 feature 模块，AppShell 只挂载入口）；iris-ui 维持锁版本 + 升级门禁（F8，owner #7 只需补触发条件）。

---

## 5. Self-check（缺陷模式/反例核对）+ 实际运行验证

### 缺陷模式逐条核对（defect-patterns.md）

| 模式 | 现状（源码核对） | 结论 |
|---|---|---|
| async-race | `QualityInspectionDetailPage` 用 `request === loadId` 版本比对（良好）；`MediaPage.load()` 仅 loading 布尔 | 部分合规；MediaPage 需 requestId（并入 G5） |
| duplicate-submit | 上传有 `uploading` 布尔锁（防重入），但无幂等键/超时 | 部分合规 → G3 |
| stale-closure / effect-loop | 未见明显实例 | 合规 |
| event-listener-leak | 无 window 监听、无裸 setInterval | 合规；M5 SSE 引入时必须成对释放 |
| permission-ui-only | 前端 `hasPermission` 控制按钮 + 后端 `requirePermission`/`requireSession`/`requireCSRF` 双保险 | 合规（前端权限仅体验，服务端复核已落实） |
| pagination-filter-mismatch | MediaPage `setPageSize` 重置 page=1 | 合规（客户端分页场景） |
| optimistic-update-rollback | 未见乐观更新 | 合规 |
| form-dirty-state-loss | 质检单有 `FormStatus new/copy/modified/clean` + `markDirty` | 部分合规；离开提示未实机验证 |
| derived-state-divergence | `MediaTableRow`/rows 均为 computed 派生 | 合规 |
| selection-cross-page | 无批量选择功能 | 不适用 |
| timeout-uncertain | `http.ts` 无超时 → 写操作无 uncertain 路径 | **不合规** → F2/G2/G3 |

### 反例核对（anti-patterns.md）

- 成功路径唯一实现：MediaPage 错误态吞为空表（**违反**，F6）；其余页 error/empty/loading 三态齐全（SystemAuditPage 有 error+retry）。
- 状态只用颜色：审计页/质检页均文字+颜色双表达（合规，erp profile）。
- 权限散落：能力标识 `hasPermission("media:write")` 集中调用（合规）。
- 硬编码魔法值：页面无硬编码间距/颜色（本次未跑 `uispacing/uicolor` 门禁，未验证）。

### 实际运行的验证命令

| 命令 | 结果 |
|---|---|
| `cd apps/web && npx vue-tsc --noEmit` | ✅ exit 0，0 错误（与 revision.md "web vue-tsc 0 错 [Verified]" 一致） |
| `cd apps/web && npx vitest run` | ✅ 20 文件 / 115 用例全绿（与 revision.md "115 pass" 一致） |
| 静态核验（grep/sed/read）：`utils/http.ts`、`stores/auth.ts`、`pages/MediaPage.tsx`、`pages/QualityInspectionDetailPage.tsx`、`pages/SystemAuditPage.tsx`、`utils/quality-inspections.ts`、`layouts/AppShell.tsx`、`router/runtime-route-{guard,coordinator}.ts`、`apps/api/src/routes/{media,quality-inspections}.ts`、`apps/api/src/{media/store.ts,quality/sqlite-store.ts,config/env.ts,auth/demo.ts}` | 全部完成 |

**未能运行**（含原因）：
- `pnpm check` 全仓 + api bun test：本次聚焦前端面，api 门禁已由 review1 五方复现 [Verified]，未重复跑；
- 浏览器 E2E/视觉回归：无测试环境（headless 浏览器未配置）；
- `uispacing/uicolor/uistyle` 验证器：本仓库 ui-specs 验证器面向生成物路径，review 阶段未应用；
- vault（presign CORS/TTL）与 governance 契约测试：本机不可达/属跨仓库项（A2 待 M0 回答）。

---

## 结论

revision.md 后端面（B1–B5、N1–N8、4xx permanent、409 三分支）与前端面**无直接冲突**，且前端当前门禁（tsc 0 / 115 绿）属实。但 **M3 是前端面最薄弱处**：自相矛盾（"前端零改动" vs "presign 直传"）+ 范围遗漏质检附件存储（生产主路径）——必须在路线图内二选一并补前端改动清单；M2/M5 各有一个被低估的前端工作项（会话过期处理、通知 UI 状态机）。G1–G6 并入后，前端面才算生产级闭环。本评审为提案，不豁免任何门禁。

---
*前端工程对抗审查 · review2 阶段产出 · 全部结论按证据等级标注，待人工核对后提升。*
