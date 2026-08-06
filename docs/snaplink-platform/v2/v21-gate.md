All six items verified against the revised artifacts. Summary of evidence:

**FB-001 ✓** — `audit-ingestion-design-v2.md` §0 anchors (L33)、§0.1 v2.1 日志行 (L43)、F5 行 (L49)、重审行 (L53)、aero-id §、任务清单 #1 (L467) 全部改为 `make check-audit-swallow` 门禁语义 + 实测基线（raw `_ =` 142 / 审计 Record 25）；`implementation-batches.md` (L18/L70) 与 `implementation-gate.md` (L3/L32) 同步。live 三文档（ingestion/batches/gate）"49 个"残留 = 0；"49" 仅存在于 audit-events-v2/integration-design-v2 的纠错附注（append 不重写，符合声明）。

**FB-002 ✓** — B2-1 两清单行均为 9 个 governance 点（account.go:155,184 / callback.go:160,221,247 / audit_hold.go:59,74 / legacy_cutover.go:88 / account_provision.go:168，2+3+2+1+1=9），标注 `[v2.1]` FB-002；ingestion 四处同步为 9；"8" 仅出现在描述变更的历史日志行。

**FB-003 ✓** — `audit-events-v2.md` §1.9 完整正文（公式 + 五输入表 + distinct-occurrence + 四层去重键 + 四条规则）；batches/gate 的 B2-4、B3-3 行均内联公式；ingestion vault § (L498-499) 与任务清单 (L532) 内联；registry §0.1 v2.1 行 (L42) 注明本节仍 canonical。

**FB-004 ✓** — 两清单 B1-8 行：`service.go:389,397-401`，`:397-399` 已判 mismatch→422（HTTP 422 + gRPC FailedPrecondition + 三层测试已钉）、`:401` 仅空 envelope 盖章，任务收敛为契约对齐与回归钉死；OpenAPI "Body tenant_id is ignored" 缺口并入两清单 B1-6 行 + ingestion sink 任务行 (L393)；ingestion L367 "非静默重标"。

**FB-005 ✓** — sink 任务清单顶部第一动作行 (ingestion L387)：修复或移除 `internal/kafka/replay.go:205`（quality 绿验收）；接线决策并入 B1-6（batches/gate B1-6 行同步，文件列标注孤儿原型）。

**FB-006 ✓** — 两清单门禁表均新增「跨仓依赖跟踪 `[v2.1]`（FB-006）」列；G1 行 = B4-1（IdP 部署仓 HEAD `b3c839bb`）状态 → G1 状态，未绿则 G1 不可达；gate 要点追加 ④（含 FB-005 提示）。

唯一瑕疵：`v21-fix-log.md` 实际 20 行，自述"149 行"不实——纯元数据笔误，不影响六项实质落实。

VERDICT: PASS - v2.1 完成，可提交人工批准
