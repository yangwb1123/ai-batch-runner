kickoff 交付完成。核心结论摘要（全文档 87 处证据标注，A–E 全覆盖）：

## 关键判断

**P0 唯一项（审计 L2）——"最大杠杆，零代码"**
- 源码实证（`forwarding.ts`）：`createAuditIntegration` 仅在 `AUDIT_APP_URL` 非空时才构造 publisher + ForwardingAuditSink，当前为空 → 纯本地 outbox。**设置 env 即激活**，L2 是部署规则而非编码。
- 真源裁定：本地 outbox = **投递真源**（retryable truth，源码注释实证）；governance 账本 = **审计事实真源**（platform-map 所有权）。不冲突：2xx 确认后 outbox 退为运维语义，业务永不读 outbox 当事实。
- 主要风险：governance 是"设计期+参考实现"（Verified）→ 先按 L1 sink 验收、账本 E2E 通过后再宣布 L2。

**P1 四项**：媒体 BFF 代理迁移（`MediaStore` 接口已抽象，换适配器即可，前端零改动；懒迁移 + 回填，禁止 blob 双写）、aero-id 组织只读投影（`DepartmentSnapshot` 可空预留位已在契约中）、审计事件组织上下文 + 事件注册、aero-im 通知（best_effort_async，与 change-manifest 示例对齐）。

**不接清单 11 项**（每条带触发条件）：aero-im 全量 IM、vault RAG/MCP/WebDAV、aero-id 账户生命周期、ERP 多租户数据模型、替换 outbox、gRPC、OTel 替代审计等——克制原则落位。

**工程运营**：pi-batch 嵌入**做但限定分析面**（campaign 发现 + 提案流水线，`git_commit: false`，不直接改生产代码）；platform-status/247 的 `PLATFORM_REPOS` 数组加第 7 个仓库（iris-ui 不加，是 npm 上游依赖，由锁版本+升级门禁管）；跨仓库验证复用 suite-template + check-change-manifest.py + 决策摘要门（decisions/ 01→05 流程现成）。

## 待人工核对的关键点
1. `schemaVersion` 与 governance ingest 的兼容值（契约测试前未知）
2. vault 预签名 URL 支持（BFF 代理下载 vs 302 重定向）
3. governance 生产硬化进度（L1 试运行门槛）

按流水线设计，下一阶段是 meta 对抗审查 → VERDICT 门，本提案状态为 Proposed，未经人工批准不进入任何仓库门禁豁免。
