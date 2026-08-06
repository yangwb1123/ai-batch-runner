已完成 ERP 场景流水线 `evidence` 阶段任务 [1/3] 的证据归一。

**产出**：`docs/snaplink-platform/erp-inputs/01-sverpweb-current-state.out.md`

**处理方式**（零 LLM 分析成本原则）：
- 事实与证据等级**原样保留**（技术栈 / 已接入平台能力 / 尚未接入（Verified 缺失）/ 工程门禁），未新增任何分析或推断，避免污染证据链
- 仅追加最小 provenance 头（源文件、扫描方法、日期、验证等级），供下游 aggregate 合并与人工核对时追溯
- 关键证据点核对无误：审计 Outbox 载荷格式与 governance ingest 吻合、`AUDIT_APP_URL` 可配置未指向 governance、媒体本地存储、无 pi-batch 嵌入

下游阶段（kickoff aggregate → meta 对抗审查 → VERDICT 门）将基于此归一产物继续。
