# Agent OS 架构与内核冻结（Architecture & Kernel Contract）

> **阶段定位**：从"工具集合"（Stage 3.5）向"运行时平台"（Stage 4）演化。
> 本阶段的工程纪律：**能力收敛、边界治理、运行时稳定性、内核冻结**——
> 不再以增加命令数量为目标，而是让 90 模块 / 61 能力 / 40 命令在稳定
> ABI 之上自由发展（Linux 的 Kernel ABI 稳定 + User Space 自由模式）。

## 1. 分层架构

```
                User Layer（CLI / 未来 Web / API / Agent-to-Agent）
                              │
                      Capability Layer（能力注册表）
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
  Decision Kernel                           Execution Fabric
  规则 / 超图 / 政策                           设备 / Runner / 调度
  决定：做什么 / 为什么 / 是否允许 / 选哪个方案     决定：在哪里 / 怎么执行 / 资源 / 失败
        │                                           │
        └─────────────────────┬─────────────────────┘
                              │
                       Evidence System（真的完成了吗 / 证据 / 可信度）
                              │
                       Learning System（规则晋升 / 影子 / 软权重）
```

**边界纪律（防自我欺骗）**：

| 层 | 职责 | 禁止 |
|---|---|---|
| Decision Kernel | 目标、约束、方案选择、授权 | 不能执行 |
| Execution Fabric | 放置、执行、资源、失败恢复 | 不能决定目标 |
| Evidence System | 验证、证据、可信度 | 不能修改事实 |
| Learning System | 软规则调整、晋升 | 硬规则必须走治理 |

## 2. 内核契约（Kernel Contract — ABI，冻结不轻易变更）

### 2.1 Capability Interface

```yaml
# pi-batch capabilities list|get|check（能力注册表）
capability:
  id: graph.extract
  domain: reasoning | planning | execution | verification | governance | device | ops
  entry: 命令入口（Capability 的一种 interface，未来 API/Agent 共享）
  owner: { module, target }        # 实现归属
  input / output                   # 契约摘要
  risk: low | medium | high
  requires / produces
  tests: [test_*.py]               # 回归关联
  module_dependencies: ≤ 15        # 模块级质量预算
```

### 2.2 Task Contract

```python
Task = { goal, prompt, constraints(硬约束先行), output, cwd,
         validate(验证义务), mobility, effect_class, checkpoint }
```

### 2.3 Event Schema（不可变追加式）

```json
{ "at": "...", "actor": "...", "verb": "submit|evidence|migrate|halt|revoke|register",
  "target": "task|device", "object_id": "...", "source": "control_plane" }
```

### 2.4 Artifact Interface

```python
Artifact = { path, sha256, size }   # 内容寻址：哈希是身份，路径只是定位
```

### 2.5 证据契约

```python
Evidence = { result: passed|failed|inconclusive, exit_code, digests,
             environment_digest, runner_version, timed_out, output_overflow }
```

## 3. 复杂度治理预算（不随能力增长而放宽）

| 预算 | 值 | 门禁 |
|---|---|---|
| 函数行数 | ≤ 50 | quality.py |
| 认知复杂度 | ≤ 15 | quality.py |
| 文件行数 | ≤ 500（pbatch/）| cli.py check |
| **模块级依赖数** | **≤ 15** | `capabilities check` |
| 测试关联 | 能力应有回归测试 | `capabilities check`（覆盖率统计） |
| 模块级循环 | 0 | `graph extract` |
| 命令可分派 | 全绿 | `tools --check` |

## 4. 演进阶段声明

| 阶段 | 状态 | 说明 |
|---|---|---|
| Kernel Contract 冻结 | ✅ 本文件 + pipeline_schema + Event/Evidence 格式 | 变更须评审 |
| Capability Registry | ✅ `pi-batch capabilities` | 命令驱动 → 能力驱动 |
| Controller/Reconciliation | ✅ `/desired/reconcile`（服务端 Desired-State Controller：声明期望→观察→补/撤直到 converged）+ runner /reconcile + 失联重排队 | 从执行工具 → 自治系统 |
| Device Fabric | ✅ D0-D6 最小件 | 对应 Kubernetes Pod/Node/Scheduler 概念 |
| Replay + Evaluation | ✅ `replay --capsule`（版本对比 + eval 回归）+ `probe`（Dry-run 标准输出） | 最有价值的数据资产 |
| 插件生态 / Marketplace | ⏸ 内核冻结后才宜开放 | 参照 VSCode Extension / K8s Operator |

## 5. 健康评分（Agent OS Health Score）

`pi-batch health` 输出 0-100 分：

| 组件 | 满分 | 扣分项 |
|---|---|---|
| code_quality | 25 | quality.py 违规 ×5 |
| gates | 25 | 门禁违规 ×8 |
| architecture | 20 | graph extract 模块级循环 ×5 |
| governance_budget | 15 | 模块依赖超预算 ×3 |
| test_scale | 15 | 收集测试数 /800 封顶 |

## 6. 下一步（按优先级，非堆命令）

1. ✅ **Desired-State Controller**（`devices desired apply|status`）：
   声明期望 → 服务端观察 → 补/撤直到 converged（幂等、marker 隔离）
2. ✅ **信任等级授权矩阵**（`capabilities trust --agent-level N
   [--device-zone Z]`）：Agent × 能力 × 设备信任区（分析 L1 / 验证 L3 /
   设备 L4；不可信区拒绝高等级）
3. ✅ **Critic 角色**（prompts/critic.md）：Reflection 的 LLM 化身，
   与确定性 reflect 共享证据纪律
4. ✅ **retro 升级**（`--actions`）：失败模式 → 自动改进动作 +
   reflections.jsonl 聚合（重复发现 → learn draft 建议）
5. ✅ **测试分类**：pytest.ini markers（unit/contract/property/scenario/chaos）+ 代表性打标 + `pi-batch test-report` 统计
6. ✅ **Replay 平台**：`pi-batch replay --capsule`（胶囊版本对比 + eval 回归）+ capsule_diff
