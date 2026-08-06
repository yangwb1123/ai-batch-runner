场景建模完成，已写入 `docs/snaplink-platform/decisions/02-scenario.md`（严格按流水线阶段命名，供 03-capabilities 消费）。

**场景要点：**

- **Entry**：管理员在 snaplink-console 经 capability-discovery 进入 aero-vault 管理面，定位文件、填删除原因（P1）后发起。单文件、无审批（与 intake 的克制范围一致）。
- **Preconditions**：管理员权限（snaplink）、文件与所有者可解析（aero-vault / aero-id）、通知端口配置为 AeroImNotification、审计链路 L2 就绪；Legal Hold 仅记录为前置条件变量（P3，未启用则无检查）。
- **Happy Path（7 步）**：授权检查 → 删除生效（元数据+对象存储+分享/版本/RAG 引用全失效）→ 审计事件**异步**入账本（dependency_rules 强制）→ 通知所有者（含原因）→ console 呈现成功 + 审计确认状态。
- **Failure Paths（7 条，表格化）**：核心原则——**同步失败（授权/文件态）在变更前阻断；异步失败（审计/通知）不阻塞业务、不静默丢失、可观测**。审计离线 → 本地持久队列补投不丢事件；通知失败 → 不回滚，账本是事实源，所有者靠查询兜底。
- **Follow-up**：所有者查删除历史（P1）、审计员 query/export/verify + console 时间线、申诉为人肉闭环（P3）。
- **Final Business Result**：不可逆删除 + 所有者知情（有原因）+ 全程可追责审计，无悄悄消失。

**UX 核查表（6 项全过）**：管理员对进度/成功/失败即时可见（审计未确认时显示"待确认"，不谎报成功）；可幂等重试；所有者主动通知 + 历史查询兜底；审计员全程可查。

**留给后续阶段的问题（未越权裁决）**：审计事件字段契约（→05）、删除历史的数据来源冲突（file-metadata 已删 vs 查询需要，→04 组合设计裁决）、审计交付保证机制、Legal Hold 检查是否启用。
