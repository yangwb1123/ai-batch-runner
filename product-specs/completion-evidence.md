# Completion Evidence Spec — 完成证据报告（Definition of Done）

Agent 完成任务的唯一合法形式是提交**证据报告**，不是一句"已完成"。

## 1. completion_report 结构（必须输出）

```yaml
completion_report:
  summary: ""
  changed_files: []
  requirements_covered: []
  tests_added: []
  commands_executed:
    - command: ""
      result: passed          # passed | failed | not_executed
  architecture_checks: ""      # passed | failed | not_executed
  security_checks: ""
  compatibility:
    breaking_change: false
  migration:
    required: false
    rollback_verified: false
  residual_risks: []
  assumptions: []
```

## 2. 诚实规则（禁止伪造验证结果）

- 未运行的验证必须明确写 `not_executed: [{check, reason}]`
  （如 e2e 缺浏览器环境），并列出未验证风险
- 禁止把"理论上应该通过"写成"已通过"
- 禁止编造：命令输出、测试结果、覆盖率、基准测试数据
- 结果分级：已运行并通过 / 已运行但失败 / 未运行 / 无法运行 /
  人工检查 / 推测正确——必须区分

## 3. 完成条件

任务只有在以下全部满足时才允许声明完成：

- 功能符合需求（含已确认的推演维度）
- Format / Lint / Typecheck / Test / Build 实际运行且通过
- 架构与安全检查（如适用）通过
- 无未解释警告；Git diff 无无关修改
- 数据库变更含迁移/兼容/回滚说明
- 外部副作用操作说明幂等性
- residual_risks 与 assumptions 如实列出
