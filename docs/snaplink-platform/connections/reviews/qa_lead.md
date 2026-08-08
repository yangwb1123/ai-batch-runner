QA lead 评审已产出：`docs/snaplink-platform/connections/reviews/qa_lead.md`（约 300 行）。

## 执行摘要

**本次实测命令（全部 PASS，逐条记录于文档 1.2）**：
- snaplink：`go test ./test/ -run 'TestScope_|TestNetPolicyHTTP_MutationsAudited'` → ok（scope 门禁 + netpolicy 审计）
- aero-id：`go test -tags=integration ./internal/app/ -run TestAuditBinding` → PASS 16.6s（标记套件，需 DB）
- aero-vault：rest 定向（Department/Presign/Lock）+ `internal/access/` → ok
- audit-governance：`internal/service/` + `internal/outbox/` → ok

**核心发现（13 条）**：

| 级别 | 要点 |
|---|---|
| Critical | **QA-1** aero-im 连接 4 关键路径零测试——`auth.rs`/`rooms.rs`/`notifications.rs` 的 `#[cfg(test)]` 均为 0，`tests/` 仅 authz 源码 lint，outbox relay（C-1）将落在无回归网上 |
| Critical | **QA-2** snaplink C-1 空 AllowedScopes=完全不设限是**测试锁定的现状行为**（UnrestrictedPassthrough byte-identical 证明，实测 PASS）——注册表必须测试先行（fail-closed） |
| High | **QA-3** aero-id `audit.go:79` Publish 错误静默吞掉，无失败路径测试（幂等 ≠ 丢失可发现） |
| High | **QA-4** vault 删除 commit→emit 崩溃窗口"删了没记"无 kill-point 测试，reconcile 兜底未验证 |
| High | **QA-5** branding/backup/admin-token-revoke 三端点 0 审计断言；**netpolicy 已确认覆盖**（实测 PASS，从缺口名单排除） |
| High | **QA-6** vault 部门四写端点 grep 无 Record 调用 + 差集语义测试缺失（调用方 sverpweb 本机不可达 🚫） |

另有 7 条 Medium/Low（schema 审批链无状态机测试、aero-id 死路径 fail-open、JWKS grace 契约无测试、console nginx 零测试等）。

**对裁决门的影响**：架构方向无阻塞项；但 QA-1/Q-2 为变更级阻塞——aero-im C-1 与 snaplink C-1 验收必须携带对应测试，否则决策点不得标记完成。sverpweb 不可达（🚫）作为已知证据缺口记录进决策日志。
