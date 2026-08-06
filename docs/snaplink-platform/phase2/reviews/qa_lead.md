QA Lead 评审已完成，输出至 `docs/snaplink-platform/phase2/reviews/qa_lead.md`（357 行）。

## 评审方法（本会话实测，非文档转述）

- **脚本断言逐条追踪**：`/tmp/deploy-build/test-platform.sh`（与服务器版本一致）静态追踪确认 status 25（9 单元+11 端口+5 健康）、audit 8、e2e 8，`bash -n` 三个脚本语法全 OK
- **7 仓测试文件实测盘点**：sverpweb 58 个 TS 测试文件（forwarding.test.ts 实测 **9** 个用例，文档写 8——清单漂移）、governance 20 个 Go 测试（erp_contract 5 契约用例确认）、snaplink 1456 个（signingkeys 无重启持久化测试）、vault 185 个、console dart 测试存在但无 governance 时间线
- **断言源码读码**：`integrations.test.ts` 全走 demo 身份（权限面零断言）、`routes/integrations.ts` 仅 requireSession（security F1 在测试层无任何抓手）、`forwarding.ts` 仍是静默回退静态 token、`env.test.ts` 无 fail-closed 用例

## 核心发现（12 项，按严重度）

| 级别 | 发现 |
|---|---|
| **Critical** | e2e 账本查询窗口**硬编码 2026-08-06→08-07**——8 月 7 日零点起 e2e 必然失败，7×24 巡检信号失真 |
| High ×4 | e2e 账本断言是"窗口存在性"不绑定本次运行（丢事件仍假绿）；重投断言弱 oracle（不断言 HTTP 码/receipt.status）；全体系零负向授权测试（tester→integrations 403 等）；1.2.1 验收单测与 rotation 子命令不存在 |
| Medium ×5 | int-e2e.sh 纯打印零断言（presign/IM"实测通过"实为目检）；测试事件永久污染追加式账本真源；sleep 5+sync 重启竞态抖动；2.2/2.3 fail-closed 无钉死测试；console 无探测/自愈演练 |
| Low ×2 | 脆性断言（healthz 精确体、端口 grep 误报、sqlite 锁）；文档计数漂移 |

## 输出覆盖

1. **测试清单与实测命令**（§1，含"未实跑"声明——服务器套件需副作用改造）
2. **需求→测试矩阵**（§2）：路线图 10 项验收中 **8 项无测试落点**
3. **12 项 findings**（每项带证据、未测失败、回归风险、精确修法、验收断言）
4. **18 个优先场景**（P0-P2：happy/boundary/error/race/recovery 全覆盖）
5. **CI 缺口、8 项 flake 风险、4 类 fixtures、8 条退出标准**（L2 宣布前置）

克制红线保持：不新增 chaos/load/benchmark/conformance 套件，仅登记触发条件。
