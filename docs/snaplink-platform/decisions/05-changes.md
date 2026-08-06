变更设计完成，已写入 `docs/snaplink-platform/decisions/05-changes.md`。

**要点：**

- **API changes**：vault 新增 3 个管理端点（检索 / 删除含 `reason` + 应答带 `audit_status: confirmed|pending` / 异步链路状态查询）+ 所有者列表加 `include_deleted` 参数（默认关闭）；console BFF 聚合页（显示名 fail_open 兜底）；snaplink / aero-im / audit-governance **零 API 变更**。
- **Event changes**：`file.delete.admin` v1（审计，含 actor 会话引用）与 `file.deleted.notify` v1（通知，自包含载荷）——同事务同源生成，字段只增不删，旧消费者透明。
- **Permission changes**：`file.file.delete`（domain.resource.action 规范格式，04 简写 `file.delete.admin` 的注册名）→ 管理员角色种子。
- **Schema/迁移**：vault 新增 `deleted_files`（tombstone）+ `audit_outbox`（若已有则复用）；snaplink / aero-im 为种子数据；audit-governance 无 schema 变更。
- **UI**：console 新「文件管理」页（原因必填弹窗、审计状态呈现）+ vault 所有者「已删除」区段（P1 兜底）。
- **Compatibility**：全增量、向后兼容；唯一注意点是管理员需先获权限（有意为之的 fail_closed 语义），由部署顺序消除"有页面无权限"窗口。
- **Deployment order**：snaplink 授权 → vault（迁移+部署）→ im 模板（须在首次通知前）→ console → staging 验证账本 ingest 后放行。
- **Rollback**：功能面逆序回退；**删除不可逆、账本事件永不回滚**（审计留痕是特性）。
- **Test plan**：unit（事务原子性/outbox 幂等/权限强制）→ contract（OpenAPI + 双事件 schema）→ adapter（GovernanceSink 离线补投 / AeroIm 零回取）→ composition e2e（主路径 + 4 条失败路径 + 禁止项回归）。
