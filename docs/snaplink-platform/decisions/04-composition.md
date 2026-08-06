组合设计完成，已写入 `docs/snaplink-platform/decisions/04-composition.md`，并裁决了 03 遗留的三个裁决点。

**核心结论：**

- **Primary Project：aero-vault（唯一）**——删除执行、审计事件生成、通知触发、删除历史全落在文件生命周期属主上；其余项目只供能力或收事件。
- **Collaborating（5）**：snaplink（授权复用 + 权限目录登记 `file.delete.admin`）、console（文件管理页 + 时间线复用）、aero-im（模板 + in-app + delivery-status，纯复用+配置）、audit-governance（纯复用，零代码）、aero-id（零改动）。
- **Unaffected（显式）**：**aero-id 完全不受影响**——仅被 console-BFF 以现有 user-profile 查询，vault 侧只做 uid 引用。
- **同步调用 4 条，全部 1 跳**（远低于 3 上限）：console→snaplink 授权、console→vault 管理 API、console→aero-id 显示名解析、vault→snaplink 变更时复验。
- **异步事件 2 个（均 v1）**：`file.delete.admin`（审计）与 `file.deleted.notify`（通知，**载荷自包含**——遵守 im-serial-fetch-context-on-send 禁止项）。审计与通知在删除事务内同源生成。
- **失败策略**：授权 fail_closed（阻断）；审计 durable_async（outbox 滞留重试，不丢事件、不阻塞）；通知 best_effort_async（delivery-status 跟踪 + tombstone 兜底）；显示名 fail_open（uid 兜底）。
- **禁止项检查 7/7 通过**：无直连 DB、无双向同步、无审计回调、无 IM 串行回取、授权全走 snaplink、浏览器不绕 BFF。

**对 03 的三处修订（组合层裁决）**：① C13 取消 audit-governance owner 查询（不在 allowed_sync 内），改为 vault 内 tombstone（vault 自身生命周期投影，非第二审计真相）；② outbox 落位 vault 事务内（不引入新组件）；③ 显示名由 console-BFF 向 aero-id 解析，vault 不碰 aero-id。
