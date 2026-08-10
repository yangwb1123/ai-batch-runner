# Deferred Backlog

以下条目是历史总结中的方向，不是当前承诺。没有明确触发条件和验收标准时，
不得由 Agent 静默扩展为实现任务。

| 候选 | 当前决策 | 提升触发条件 |
|---|---|---|
| 运营智能埋点机制 | Deferred；runner 已有事件/usage，但业务埋点属于目标项目契约 | 明确事件 schema、隐私边界、消费方和可执行验收 |
| 多 Agent 协作调度深化 | Deferred；当前已有 meta 并发、相关性上限、独立会话和失败预算 | 出现可复现调度缺陷并定义公平性/吞吐/隔离指标 |
| 分发渠道工具化 | Closed for current scope；核心三件套复制和 wheel 均分发 checker、模板、pipeline 与规范资产 | 出现明确的新渠道、签名或来源验证需求后重新立项 |
| `examples/snaplink-platform/wait-for-run.sh` 退役 | Retained for compatibility；`--wait-lock` 已是推荐入口 | 所有外部调用方迁移并有引用扫描证据 |
| sverp bridge 同类缺陷 | External；属于 iris-ui 源仓，不在本仓库变更半径 | 在 iris-ui 立项并通过其 source-first 门禁 |
