# Migration Engine：项目对比、迁移计划与升级预演

`pi-batch migrate` 将 source-first 迁移、两个项目对比和旧项目升级统一为一个**有证据的迁移契约**。

## 命令

```bash
# 只做结构化对比，不修改任何项目
pi-batch migrate compare \
  --source /path/to/reference \
  --target /path/to/target \
  --manifest migration.yaml \
  --json

# 根据证据差异生成依赖批次
pi-batch migrate plan \
  --source /path/to/reference \
  --target /path/to/target \
  --manifest migration.yaml \
  --json

# 预演计划、波次、工时、预算；默认不执行 agent、不修改代码
pi-batch migrate rehearse \
  --source /path/to/reference \
  --target /path/to/target \
  --manifest migration.yaml \
  --budget-minutes 180 \
  --json

# 旧版本/基线升级到当前项目，语义与 compare 相同
pi-batch migrate upgrade \
  --baseline /path/to/old \
  --target /path/to/current \
  --manifest migration.yaml \
  --strict
```

`--run-gates` 只运行 manifest 中明确声明的验收命令；仍然不会启动 agent 或编辑代码。
`--strict` 在仍有缺失、未验证、失败门禁或项目门禁失败时返回 1。

没有 `--manifest` 时只能得到文件库存报告，`ready=false`，不会把“没有配置检查”判为通过。

## migration/v1 契约

```yaml
schema_version: migration/v1
name: order-migration
scope:
  include: ["apps/**", "packages/**"]
  exclude: [node_modules, dist, .git, .pi-batch]
  max_files: 10000

# 可选：目标项目级门禁。只有 --run-gates 才执行。
gates:
  - id: target-tests
    description: target tests
    command: pnpm test
    timeout: 900

capabilities:
  - id: order-allocation
    title: 订单数量分配
    priority: P0
    risk: high
    effort: 90
    depends_on: []
    owner: target
    disposition: migrate
    source:
      evidence:
        - path: src/order/allocate.ts
          symbol: allocateOrder
          claim: reference allocation rule
    target:
      evidence:
        - path: apps/api/src/order/allocation.ts
          symbol: allocateOrder
          claim: target implementation
    acceptance:
      - id: allocation-invariant
        description: allocation cannot exceed remaining order quantity
        command: pnpm test -- order/allocation
        timeout: 300
```

### 证据规则

- source 和 target 的 `path` 必须是相应项目根目录下的相对路径；路径穿越 fail closed；
- `symbol` 存在时还会检查文件中是否出现该符号；只有文件存在不等于功能存在；
- source 证据缺失是 `source_invalid`，不是迁移成功；
- target 证据缺失是 `missing`；
- target 证据存在但没有通过声明的 acceptance command 是 `present_unverified`；
- acceptance command 全部通过后才是 `verified`；
- `retain_noop` 明确表示源能力保留为无操作，不得伪装成已迁移；
- 未写入 manifest 的源能力不参与 parity 分母，报告会明确提示 manifest 只是已登记能力集合。

## 量化报告

报告包含：

```text
source/target 文件数量和字节数
能力总数
source evidence coverage
目标 evidence coverage
verified / present_unverified / missing / gate_failed
weighted_completion
acceptance executed/passed/coverage
依赖波次数
serial_minutes
critical_path_minutes
budget_status
strict_failures
```

权重不是“业务真相”：

```text
verified = 1.0
present_unverified = 0.5
missing/gate_failed/source_invalid = 0
```

它用于排序、停止和比较迭代，不用于凭空声称完整迁移。

## 与已有能力的关系

```text
assess/profile       需求风险、规模、权限和工作流路由
context              加载源项目/目标项目契约和 ADR
migrate compare     source/target 证据差异
migrate plan        依赖图、优先级和实现波次
migrate rehearse    预算、工时、门禁和推进预演
campaign             取得批准后执行单个目标方向
advance              目标项目代码质量和规范问题的增量修复
isolate              多方向 worktree 隔离
check/eval           runner 自身和规则系统回归
```

因此 `migrate` 不替代 campaign，而是把“应该迁移什么、证据在哪里、为什么能开始、预计几波完成”结构化；通过人工审阅或独立设计 gate 后，才把计划项交给 campaign 执行。

## 推荐状态机

```text
UNMAPPED
  → EVIDENCE_MAPPED
    → PLANNED
      → DESIGN_APPROVED
        → IMPLEMENTED
          → ACCEPTANCE_VERIFIED
            → PROMOTED
```

失败和边界状态必须保留：

```text
SOURCE_INVALID
TARGET_MISSING
GATE_FAILED
BLOCKED_EXTERNAL_SOURCE
RETAINED_NOOP
NEEDS_HUMAN_DECISION
```

## 不做的事情

当前引擎不会：

- 根据文件数量声称两个项目功能等价；
- 把 LLM 的自由文本当作迁移证据；
- 自动把源项目代码复制到目标项目；
- 自动修改代码或自动提交；
- 把不存在的生产数据源当作 0；
- 把未执行的测试报告为通过；
- 强迫所有项目使用同一套目录或技术栈。

真正的语义比较仍需要 source/target 证据、契约、验收命令和人工/对抗审查共同完成。
