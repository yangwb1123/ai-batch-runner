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

## FB-004：B1-8 清单描述已过时（来源：sink B1 提案，工作树实测）
- 清单称"service.go:389 静默重标 tenant"——实测 `service.go:397-399` 已判
  mismatch→422（:401 仅为空 envelope 盖章），HTTP 422 + gRPC
  FailedPrecondition + 三层测试已钉
- 处置：v2.1 修正描述；剩余缺口 = OpenAPI 未同步（仍写 "Body tenant_id
  is ignored"）→ B1-6 契约声明合并

## FB-005：kafka replay 孤儿原型破坏工程门禁（来源：sink B1 提案）
- `internal/kafka/replay.go:205` 未提交且 `python3 cli.py quality` FAIL
  （go vet 格式串 %s 绑 int）
- 处置：实施第一动作 = 修复或移除该原型；接线决策进 B1-6

## FB-006：G1 联合门禁跨仓依赖显式化（来源：sink B1 提案）
- G1（B1-1+B4-1+B1-7）依赖 IdP 部署仓 B4-1，非 sink 仓可控
- 处置：v2.1 门禁表增加"跨仓依赖跟踪"列（B4-1 状态 → G1 状态）
