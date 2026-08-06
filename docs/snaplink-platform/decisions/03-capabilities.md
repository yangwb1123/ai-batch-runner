能力发现完成，已写入 `docs/snaplink-platform/decisions/03-capabilities.md`。

**方法**：先无项目名列出 14 项能力需求（C1–C14），再映射到 platform-map.yaml 的项目与能力目录。

**结论摘要：**

- **reuse（8）**：控制面导航、授权检查、审计账本/查询/导出/完整性、身份解析、通知发送、投递状态、审计时间线——全部现成，零改动。
- **adapt（3）**：管理面文件定位（vault 管理视图+所有者投影）、审计事件生成（复用 AuditSink 端口，新增 `file.delete.admin` 事件类型）、所有者删除历史查询（audit-query 加 owner 维度——数据源裁决留给 04，避免与 file-metadata 属主冲突）。
- **extend（3+1 数据项）**：文件删除执行（注意：**删除未列入 vault 能力清单**，需核心能力化）、删除原因字段、审计异步可靠投递（GovernanceSink + 持久队列）；另 snaplink 权限目录登记删除权限项。
- **create（0）**：不需要新通用能力，**不需要新项目**——优先级守则（reuse > adapt > extend）完整生效。
- **exclude（8 项）**：回收站/恢复、Legal Hold、审批流、批量删除、第二渠道、管理端删除历史专页、申诉工单、webhook——与 intake 的 P2/P3 克制范围一致。

**留给 04 组合设计的已知裁决点**：C7 可靠投递机制（outbox 放哪）、C13 查询数据源（账本直查 vs tombstone）、C3 管理视图的所有者投影数据来源（aero-id 查询方向）。
