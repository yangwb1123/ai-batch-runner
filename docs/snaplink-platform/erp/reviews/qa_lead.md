QA 评审完成，交付物已写入 `docs/snaplink-platform/erp/reviews/qa_lead.md`（meta 对抗审查阶段输出目录）。核心结论：

## 实测命令（本次修订真实运行）
| 命令 | 结果 |
|---|---|
| `pnpm test`（turbo） | ✅ api 133 pass / **1 skip**（snaplink E2E）/ 0 fail；web 115 pass |
| `SNAPLINK_E2E=1 bun test snaplink.e2e.test.ts` | ✅ 通过（真实 sso-server，code+PKCE，31 expect）——**默认 CI 跳过，手动可跑** |
| `pnpm check` | ❌ **红**：apps/api typecheck 31 个 TS 错误 |
| 源码核验 | `forwarding.ts` 激活分支、`.env` 无 `AUDIT_APP_URL`、`DepartmentSnapshot` 可空契约、`MediaStore` 抽象、`PLATFORM_REPOS`=6 项 —— 均与 kickoff 一致 |

## 关键发现（按严重度）
- **F1 [High]** `pnpm check` 门禁红（31 个 TS 错误，含审计中间件本体）——证据 01 只声明门禁未测状态；所有"pnpm check 通过"的验收标准当前不可达成
- **F2 [High]** 未埋点路由的回退审计事件：`withActor` 误作用在 AuditEvent 而非 OperationContext，**actor/部门上下文静默丢失**；bun 不做类型检查 + 测试不断言 actor 字段，运行时无感知——直接打脸 P1"审计事件组织上下文"
- **F3 [High]** P0 激活路径（`ForwardingAuditSink`/`createAuditIntegration`）**零测试**；走查发现 flush 期间追加的事件依赖下次 append/worker 才投递，无补偿语义测试
- **F4 [Medium]** 跨仓库契约（schemaVersion=1 兼容、event-id 去重）无可执行测试，governance 仓库本机不存在——待核对 #1 仍是 Unknown
- **F5 [Medium]** 跨仓库 E2E 默认跳过；**F6** 媒体仅 1 个测试、无路由级/conformance 测试；**F7** 仓库无 git（.git 空目录）、sso-client 是仓库外 file: 依赖、无 CI——campaign 与 platform-status 扩展的运营前提缺失
- **F8/F9 [Low]** outbox 坏行 500 全查询无检疫；证据 01 provenance 不实（"git log"/"vitest"/"无 campaign 配置" 均与现状不符，campaign yaml 08-06 09:07 已存在但从未运行）

## 退出标准（P0 可验收前置）
1. `pnpm check` 转绿 + E2E 进独立 job；2. `forwarding.test.ts` 6 类用例（激活/下游错误/并发追加/重启/空批/去重）；3. 回退事件 actor 断言测试（当前必失败，修复后过）；4. suite-template governance 契约用例回答 schemaVersion 兼容；5. 证据 01 归一；6. git/运营前提决策。

评审结论：P0"零代码"判断成立，但 **P0 不可零代码宣布**——激活层零测试 + 契约无验证 + 门禁已红，按 F3→F4→F1 补齐后方可启动 L2 试运行。本评审为咨询性意见，不构成发布批准。
