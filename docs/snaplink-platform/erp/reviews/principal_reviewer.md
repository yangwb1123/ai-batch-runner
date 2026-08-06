Verification complete. I have everything needed. Here is my principal review.

---

# Principal Review — ERP 接入平台六件套路线图（revision 轮次 2 合成审查）

**依据**：kickoff（证据块）＋ 14 份角色评审（reviews/，含 architect/security/protocol/identity_protocol/testing/qa/backend/devops/database/compliance/frontend/async）＋ gate FAIL 裁决（gate.md）＋ 04 号人工复核证据 ＋ 当前 revision.md（11:12 版，263 行）＋ 我对 `/home/dwp/workspace/job/sverpweb-stable` 与 `/home/dwp/snaplink-audit-governance` 的**独立实机复核**。本审查为咨询性意见；不制造任何签署、期限、分数或批准状态；全部 owner 为"Proposed 待任命"。

## 0. 我的独立复核（非转述）

| 断言 | 复核结果 |
|---|---|
| B1 契约不兼容 | ✅ 亲验：`env.ts:76` 默认 `AUDIT_INGEST_PATH=/api/v1/audit/events`；`server.go:76-77` 为 `POST /api/v1/events`/`events:batch`；`models.go:360-387` ValidateBasic 必填 9 字符串 + schema_version>0 + occurred_at + actor.id + payload\|payload_ref |
| B2 withActor 误用 | ✅ 亲验：`contracts.ts:171` 签名只接受 OperationContext；`request-middleware.ts:116` 对 `newAuditEvent(...)` 调用 → TS2345（本人 tsc 复现） |
| B3 门禁红 | ✅ 亲验：`bunx tsc -p apps/api/tsconfig.json --noEmit` = **31 错**，行号与五方评审精确吻合（116 TS2345 / 131 TS2353 / 139 TS2345 / sqlite-outbox.test.ts:122）；`apps/api/src/audit/` 无 forwarding.test.ts |
| B4 审计事实 | ✅ 亲验（修订版）：`data/app.sqlite` 现**有** audit_outbox 表但 **0 行**（与 revision 的"10:32 隐式迁移建表、无版本戳/备份"一致）；生产零审计事实成立 |
| B5 无 git | ✅ 亲验：`.git` 空目录（无 HEAD/objects） |
| P4/N4 fail-open | ✅ 亲验：`main.go:44` `AUDIT_ALLOW_DEV_AUTH` 默认 true；`:67` 开发默认签名/加密密钥 |
| 409 三分支 | ✅ 亲验：`service.go:361-379` 同 event_id+同 digest→Duplicate；异 digest→`event_id_content_conflict`；同 key 异 event→`idempotency_key_conflict` |
| G2-A claim 抹标 | ✅ 亲验：`sqlite-outbox.ts:199/237` `last_error = NULL`、`:203/:353` `WHERE status IN ('pending','failed')` → permanent 必须新增列 |
| G2-G hasField | ✅ 亲验：`service.go:948` switch 只特判 6 字段，required 写 `aggregate_type` 恒失败 |
| 前端 F1/F5、async H1/H4 | ✅ 亲验：`quality/sqlite-store.ts:143` 存在独立附件存储（`FILE_CONTENT_ROUTE_PREFIX=/api/quality/inspection-files`，不在 MediaStore 之下）；`quality-inspections.ts:293` PUT 无条件 update；`http.ts` 零 AbortController/timeout；SystemAuditPage 读 `/admin/audit/events`（outbox） |
| N2 demo 伪造 | ⚠️ 未亲跑复现（security_engineer 运行时复现 ×3，代码路径 `auth/demo.ts` 实证存在）；标记为多源 Verified 但非我本人复现 |
| N8 备份/脏树 | ⚠️ devops 实机核验（192.168.123.52），未亲验主机 |

## 1. 咨询建议：**Conditionally ready**（路线图提案作为人工批准基础）

证据置信度：**审计 L2 主线 High**（B1/B2/B3/B4/G2-A/G2-B/G2-E/G2-F 我均亲验）；**frontend/async 维度 Medium**（源码亲验但未跑运行时测试）。

- revision 已把 gate 的 5 项阻塞全部转化为带可执行验收的硬门槛里程碑（M0→M1→M2），无一项被驳回或淡化；P0"零代码"已诚实证伪并修正；owner 决策表（§6）标注"Proposed 待任命"而非伪造责任人；§10 证据表对 Unknown/Proposed 的划分诚实。**这是可批准的路线图形态。**
- **但**：gate2 裁决标准第 5 条（"对抗审查新发现的阻塞项已处理"）**当前不满足**——review2 轮次 1 的 frontend_engineer（F1–F5）与 async_reviewer（H1–H5，VERDICT: FAIL）发现**未进入 revision §8 阻塞清单**（§8 只有协议/安全/合规/devops/终裁门 G 项）。M3 里程碑仍写"前端零改动"，与 F1 实锤的自相矛盾未被修正；async H1（超时不确定→重复提交→重复业务记录）与 H4（并发编辑静默覆盖）是 ERP 生产主文档的 High 级数据缺陷，在 revision 中无对应工作项。
- **L2 宣布（release）**：**Not ready**——任何情况下 M2 全绿 + `AUDIT_ALLOW_DEV_AUTH=false` + 真实密钥 + HTTPS + demo 关闭 + fail-closed 校验之前不得宣布。这与 revision §4.2 一致，无需更改。

## 2. 合并后发现（去重，含来源）

### Critical

| # | 发现 | 来源（去重后） | 证据 | revision 处置 |
|---|---|---|---|---|
| C1 | L2 契约不兼容（路径/9 必填/active schema 三缺一即全批 400） | gate B1、architect H1、backend B1、protocol B1-B3、compliance F-2、devops H1、testing H3 | ✅ 亲验 | §2 五步路径 + M0 对拍 |
| C2 | 回退事件 actor 恒 null + 顶层键污染 | gate B2、QA F2/H1、security S1、protocol P2、compliance F-1、database F-D4 | ✅ 亲验 | §3 精确修复 + 先红后绿 |
| C3 | permanent 终态无实现路径（claim 抹 last_error、failed 被重领） | 终裁门 G2-A、backend | ✅ 亲验 | §2.2 新增 permanent 列（零重建）+ claim 排除 |
| C4 | 批量毒丸/HOL（publish 全或无、队首坏事件饿死队尾） | 终裁门 G2-B、protocol P3、testing H4 | ✅ 结构亲验 | §2.2 逐事件 receipts + 隔离 |
| C5 | 门禁红（31 TS 错）＋激活路径零测试 | gate B3、QA F1/F3、testing H1-H3、protocol | ✅ 亲验（本人复现 31 错） | §4.1 M1 硬门槛 + forwarding 6 类测试 |
| C6 | 生产零审计事实（0 行；10:32 隐式建表无版本戳/备份） | gate B4、compliance F-3、database F-D1 | ✅ 亲验（0 行） | B4 复核版 + M1 迁移治理 |
| C7 | 账本认证默认 fail-open（dev-auth=true + 开发密钥） | protocol P4、终裁门 G2-F、compliance | ✅ 亲验 | §4.2 L2 前置 + owner #8 |
| C8 | 无 git/CI、生产二进制脏树不可溯源、备份过期 | gate B5、devops C1/C2/H2、终裁门 N8、QA F7 | ✅ 亲验（.git 空）+ ⚠️ 主机面转述 | §4.3 M4 前置（git 基线提前至 M1 前） |

### High

| # | 发现 | 来源 | revision 处置 |
|---|---|---|---|
| H1 | id_token 收到即存从不校验、nonce 从不发送（OIDC Core §3.1.3.7 MUST 违反） | protocol P1 | ✅ §8 N1 → M1 |
| H2 | demo 模式伪造授权码 → `demo:root`+`["*"]` 全量管理员（复现 ×3） | security S2 | ✅ §8 N2 → M1（L2 宣布前提） |
| H3 | 全链路明文 HTTP + `.env` 活体密钥 | security S3、compliance F-4、devops | ✅ §8 N3 → M1 |
| H4 | 未认证 POST 写放大 outbox + 256MiB/50MB 内存 DoS | security S4/S7 | ✅ §8 N5 → M1/M2 |
| H5 | null-actor 探针事件（审计中间件先于认证注册）→ L2 启用即积压 | 终裁门 G2-E、compliance F-1 | ✅ §3 挂载点跳过裁定 |
| H6 | **前端 timeout-uncertain + 重复提交无幂等（写操作超时重试 → 重复检验单/附件/报价）** | async H1 | ❌ **未入 revision** |
| H7 | **并发编辑静默覆盖（SaveRequest 无 version、PUT 无条件 update）** | async H4 | ❌ **未入 revision** |
| H8 | **M3"前端零改动"被证伪 + 质检附件第二存储不在 MediaStore 之下** | frontend F1 | ❌ **未入 revision**（M3 行仍写"前端零改动"） |
| H9 | countPending 含 failed → "24h 无 failed"恒不可达；console 审计页无实现无 owner | 终裁门 G2-D | ✅ §4.2 重写 + not_executed 显式化 |
| H10 | schema 计数低估 22→30（-36%），新 event_type 随发版 422 | 终裁门 G2-G、backend §3.7 | ✅ §2.1 30 条 + 发布门禁 |

### Medium

| # | 发现 | 来源 | revision 处置 |
|---|---|---|---|
| M1 | 前端竞态裸奔（ContentsPage 翻页、BsPricingPage 编辑态被旧响应覆盖） | async H2/H3 | ❌ 未入 revision |
| M2 | 列表页 N+1 请求风暴（100 行并发附件请求）；导出只取前 100 条 | async H5 | ❌ 未入 revision |
| M3 | 上传交互缺进度/取消/部分成功报告/幂等 | frontend F3 | ❌ 未入 revision |
| M4 | SystemAuditPage 把 outbox 呈现为"审计日志"（L2 后语义变为投递队列运维视图） | frontend F5 | ❌ 未入 revision |
| M5 | 遗留媒体行（data:/uploads 2 行）新代码下 404，M3 回填无源可读 | database F-D2 | ❌ 未入 revision（M3 回填源未定义） |
| M6 | 保留/删除两侧皆空（ERP 无 purge；governance retention 只读无执行器） | compliance F-3、database F-D3 | ✅ owner #5 + §4.2 |
| M7 | 刷新令牌轮换/重用零测试 | protocol P7 | ✅ N6 P2 待办（触发条件） |
| M8 | SSO 撤销后 ERP 会话残余窗口（生产 1h + 请求间隔） | protocol P5 | ✅ N7 声明不支持 + 触发条件（合规要求实时撤销时重评） |
| M9 | 无周期 drainer；停机低流量时 failed 不可见 | architect M1、compliance R9 | ✅ M1/M4 |
| M10 | `occurred_at` ms vs date-time 类型级不兼容 | protocol P6、终裁门 A3 | ✅ §2.5 适配器转 RFC3339 |
| M11 | 升级=隐式无版本无备份就地迁移；`synchronous=NORMAL` 断电窗口 | database F-D1/F-D8 | ✅ B4/M1 + M4（sqlite .backup） |
| M12 | 幂等键检查 O(N) 全扫 | database F-D11 | ✅ owner #8 硬化项 |
| M13 | 通知入口静态占位、M5 前端范围低估、审批深链 409/404 未定义 | frontend F4 | ⚠️ 部分（M5 有里程碑，细节 owner #6） |

### Low / Info

证据 01 provenance 不实（QA F9——修订清单未含证据 01 归一，**应补入**）；P8 ingest 路径与 sso-server 读接口命名空间冲突；P9 issuer 严格比较（可接受，fail-closed）；trace 头投毒有界；/health 泄露；迟到 202 语义已由守卫验证为无害 no-op（终裁门 A1，✅ 已入 §4.1 测试清单）。

## 3. 权衡账本（trade-off ledger）

| 冲突 | 选项 | 建议 | 后果 | 决策 owner |
|---|---|---|---|---|
| "零代码 L2" vs 契约适配器（被证伪） | A 适配器+schema 注册 / B governance 兼容端点 / C 停留 L1 | **A**（单仓、可逆回退 L1） | ~150 行适配器 + 30 条 schema + 运行手册；B 否决（违反组合纪律） | ERP 后端负责人 + governance 维护者 |
| M3 presign 直传 vs 前端零改动（互斥） | presign（两阶段前端改造，cookie/CSRF 失效） / BFF 流式透传（保持前端，需流式能力） | **必须显式二选一写进 M3**；若保前端零改动则选 BFF 流式（multipart→stream，非 arrayBuffer 缓冲） | 选 presign：前端改动 + vault CORS/TTL 依赖（未验证）；选流式：50MB 不落 BFF 内存的断言改为流式峰值断言 | ERP 前端/后端负责人 + vault owner |
| idempotency_key=event.id vs 映射 Idempotency-Key 头 | 两者 | **event.id**（409 陷阱已亲验） | 409 两分支成为"发端缺陷"检疫信号而非去重成功 | ERP 后端负责人 |
| 4xx 无限重试 vs 终态 | 新增 permanent 列 + claim 排除 vs 状态机 hack | **新列**（零重建，已亲验 claim 抹标） | admin 重放路径暂缺（远期）；队列永不毒丸 | ERP 后端负责人 |
| 未认证探针是否落审计 | 落（actor null）/ 不落 | **不落**（actor.id 必填无法满足；探针噪音） | 认证前攻击流量不进账本（web 日志兜底，须写入部署文档） | 平台安全负责人 |
| SSF/CAEP/BCL 接不接 | 接入 vs 声明不支持 | **声明不支持 + 触发条件**（合规要求实时撤销时重评） | 残余窗口最长 1h+请求间隔，写入可接受风险清单 | 平台安全负责人 |
| demo 模式 | 保留 LAN demo / 生产禁用 | **生产禁用 + 删除死配置 DEMO_PASSWORD** | 伪造身份污染审计 actor 真实性 → L2 宣布前提 | ERP 后端负责人 |
| 审计 vs 业务耦合 | 同步阻塞 / best_effort_async | **best_effort_async**（outbox 已异步，业务永不阻塞） | 账本延迟 ≤ drainer 周期；停机积压由 permanent 分类 + 告警兜底 | 平台架构负责人 |

## 4. 前置条件 / 可执行验收 / 回滚 / 监控 / 排除项 / 残余风险

**前置（批准即生效，全部 gate 化）**：§6 八项 owner 具名任命（现为 Proposed，批准时任命——这是人工批准检查单第 1 项）；M0 契约对拍（golden payload → 预期 400 断言列表 = ValidateBasic 字段清单）；M1：tsc 31→0 + 全部测试绿 + forwarding 6 类测试 + 4xx/409/批量部分成功/超时 uncertain + permanent 列 + 毒丸隔离 + null-actor + 迟到 202 + 30 条 schema（不启用）+ 安全项 N1–N3；M2：E2E 全绿（建单/审批/上传 → 账本命中 → outbox 清空 → 24h 无新增 permanent）才宣布 L2。

**可执行验收命令**（我亲验可行的形态）：`bunx tsc -p apps/api/tsconfig.json --noEmit` → 0 错；`bun test src/audit/`（含新增 forwarding.test.ts）；`curl -X POST <gov>/api/v1/events:batch -d @erp-sample.json` → 400 且列出缺失字段；`sqlite3 data/app.sqlite "SELECT COUNT(*) FROM audit_outbox"` → 积压归零；T1–T11（protocol 一致性测试）+ C 组完整性测试（revision §4.1）全部先红后绿。

**回滚触发器**：M2 试运行期间 H1 场景（拒收→积压）→ 清空 `AUDIT_APP_URL` + 重启即回退 L1，outbox 无损（schema 注册只读叠加可回退）；M3 回填中断 → 双读保留本地回退，禁双写；任何 M1/M2 硬门槛不绿即停（revision §11.3）。

**监控**：drainer（60s > lease 30s，fake-clock 接管测试入 M1）；`countPending` 拆 pending/failed/permanent 三计数（终裁门 D 项）；告警接线（/metrics）；备份演练一次 + RTO/RPO（owner #8）。

**显式排除（不接）**：revision §9 十一项 + SSF/CAEP/SAML/SCIM（触发条件见 N7）+ 替换 outbox（仅当 governance 提供事务性中继时重评）。

**残余风险**：governance 为设计期单节点文件存储参考实现（硬化验收 owner #8）；schemaVersion 兼容值、`occurred_at` 格式裁定仍 [Unknown]（M0 回答）；租户映射（owner #1）阻塞 M2 验收；vault CORS/TTL 未实机验证（frontend A2，vault 仓库评审面不可达）；遗留 media 行回填源（F-D2）；S2 demo 伪造与 N8 主机面为转述证据。

## 5. 缺失证据与下一步（窄行动）

**缺失/不一致**：
1. **gate2 全文**（11:11 被拒轮）不在盘上，仅 G2-A..H 经 revision §8 存续——终裁门对 frontend/async 发现的态度不可考。
2. **frontend/async 轮次 1 发现未折入 revision**（H6/H7/H8/M1/M2/M3/M4/M5 即上文 ❌ 项）——这是 gate2 标准 5 当前的失败点。
3. vault 仓库 CORS/TTL 未验证（architect 验证了 presign 存在，frontend 侧 vault 不可达——两评审存在可达性矛盾，需一次实机核验）。
4. 证据 01 provenance 归一（QA F9）未列入修订清单。
5. 50MB 直传"不落 BFF 内存"是**计划中的压测**，非已完成基准。

**下一步（按序，均为可执行）**：
1. **修订 revision**：把 async H1–H5、frontend F1/F3/F5、database F-D2 折入——§8 阻塞清单补 frontend/async 项；M3 重写（显式 presign vs BFF 流式二选一 + quality-inspection-files 第二存储覆盖 + 回填源定义）；M2 验收补 SystemAuditPage 语义更新；M5 补通知深链 409/404 与上传交互链（进度/取消/幂等）。
2. 然后重跑 review2 → gate2；gate2 标准 5 方可满足。
3. 人工批准时逐项任命 §6 八项 owner（含 N1–N3 修复责任人）——批准清单已就绪。
4. 启动 M0（半天）；M0 结果回读后再动 M1。
5. **L2 宣布的唯一路径**：M2 E2E 全绿 + dev-auth=false + 真实密钥 + HTTPS + demo 禁用 + fail-closed 校验——在此之前账本状态保持"设计期参考实现"定位。

**结论**：revision 是诚实、门禁化、可执行的路线图提案，审计 L2 主线修复规格全部有源码级证据支撑（我亲验核心七项）；批准其作为工作基础是合理的。但 gate2 在当前内容下应继续 **FAIL**（标准 5 未满足：frontend/async 轮次 1 阻塞发现未处理），除非维护者明确将前端异步硬化列为带触发条件的非目标。本审查不批准任何仓库门禁豁免；全部结论为提案，待人工按证据标准核对后提升。
