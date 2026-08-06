6 项反馈已逐项落实，未改任何代码。产出：**`docs/snaplink-platform/v2/v21-fix-log.md`**（149 行，每项含文件/小节/改前→改后）。

## 修订摘要

**FB-001 — F5 计数 → 可复现门禁语义**
- `audit-ingestion-design-v2.md`：§0 anchors 行、§0.1 日志（新增 v2.1 行 + 修正 F5 行/重审行）、aero-id § Scope note、任务清单 #1
- `implementation-batches.md` / `implementation-gate.md`：头部注记 + B2-1 行
- 全部由 "49 个 `_ =` 站点" 改为 `make check-audit-swallow` 门禁语义 + 实测基线（raw `_ =` 142 / 审计 Record 25），机械校验 live 三文档残留 = 0

**FB-002 — 第 9 点**：aero-id 路径清单表新增 `account_provision.go:168`（live 树，6+1 写路径）行；governance 计数 8 → 9（ingestion 5 处 / batches 1 处 / gate 1 处同步）

**FB-003 — 公式正文内联**：`audit-events-v2.md` 新增 §1.9 完整正文（公式 + 五输入表 + distinct-occurrence + 四层去重键 + 四条规则）；B2-4/B3-3 行、vault § 与任务清单行内联公式；registry §0.1 加 v2.1 行（本节仍 canonical）

**FB-004 — B1-8 描述修正**：`service.go:389 静默重标` → `:397-399 已判 mismatch→422（HTTP 422 + gRPC FailedPrecondition + 三层测试已钉），:401 仅空 envelope 盖章`；任务收敛为回归钉死；OpenAPI 缺口（"Body tenant_id is ignored"）并入 B1-6

**FB-005 — 第一动作**：sink 任务清单顶部新增「修复或移除 `internal/kafka/replay.go:205` 孤儿」（quality 绿验收），接线决策并入 B1-6（batches/gate 同步）

**FB-006 — 跨仓跟踪**：两清单门禁表新增「跨仓依赖跟踪」列，G1 行 = **B4-1（IdP 部署仓 HEAD `b3c839bb`）状态 → G1 状态**，未绿则 G1 不可达；要点追加 ④

历史日志（fix-log/fix-gate/revision-gate）与反馈原文不改写；`audit-events-v2.md`/`integration-design-v2.md` 以追加附注方式纠错（append 不重写）。
