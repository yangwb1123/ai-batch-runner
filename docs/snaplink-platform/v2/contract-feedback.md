# 契约反向反馈（实施提案 → 契约清单修正待办）

> 批次提案（B1-B6）在逐项核对实施清单时发现的契约层缺陷。
> 汇总后并入 v2.1 修订（或人工批准前修正），不直接改契约。

## FB-001：F5 计数争议（来源：aero-id B2 提案，实证 commit `756b65b`）
- 契约/实施清单声称 aero-id 有 **49 个 `_ =` 站点**（SEC-V2-7 原始 grep 无存档）
- 实测：raw `_ =` = **142 站点**；`_ = s.audit.Record/Log(` = **25 站点**
- 处置建议：v2.1 用可复现的 grep 定义计数（`make check-audit-swallow` 门禁模式），
  契约计数改为实测值或改为门禁语义（"全仓禁止吞审计"）而非精确计数

## FB-002：清单遗漏第 9 个审计 Record 点（来源：aero-id B2 提案）
- 实施清单列了 8 个 governance 调用点（account.go/callback.go/audit_hold.go/
  legacy_cutover.go）
- 实测发现第 9 点：`account_provision.go:168`（live 树）
- 处置建议：v2.1 补入 B2-1 范围（6+1 写路径）

## FB-003：B3-3 与 B2-4 的 event_id 派生公式在外部契约仓（来源：aero-id B2）
- 实施清单引用 §1.9 公式，但本仓无正文
- 处置建议：v2.1 将公式正文纳入契约包（或标注外部引用 + 实施前拉取核对）

## 流程
- 每份批次提案完成后，实施者核对清单路径/计数/遗漏 → 追加本节
- v2.1 修订流水线（audit-revision-pipeline 复用）合并本节全部条目 → gate 重裁
