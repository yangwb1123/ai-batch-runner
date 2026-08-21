# 克制与豁免（Restraint & Exemption）

> 规律的目的是让新项目不必重新踩坑，**不是**让新项目长成样本项目。
> 本文件定义规律的适用边界与豁免通道——规律是记忆，不是宪法。

## 1. 规律分级（level）

| level | 含义 | 执行方式 | 豁免 |
|---|---|---|---|
| invariant | 普适安全底线（不伪造证据/不发明接口/尊重明确不做） | 硬门禁 | 不可豁免 |
| contract | 特定情境强约束（契约驱动项目的权威源分层） | 默认强制 | 项目宪法显式覆盖 + 理由 |
| policy | 组织策略（变更预算） | 默认强制 | 正式例外流程（learn exception，90 天复查） |
| heuristic / suggestion | 有价值但可被更好方案替代（管线形状/验证实验/基线） | 只注入 prompt | 无需豁免，agent 可论证跳过 |

## 2. 适用域与反例信号（scope / counter_signals）

- `scope`：规律只在匹配的项目形态加载（契约驱动/平台/通用）。
- `counter_signals`：**反例信号命中 → 规律自动降级**，不需要人想起来关闭：
  - `exploration`（探索/原型/调研/spike）：contextual/advisory 全降级，
    只留 invariant 安全底线——探索期不需要契约出处与验证实验行囊。
  - `prototype`（无契约）：authority-layering 无对象，跳过。
  - `quick`（一次性/临时）：verify-by-doing 降为建议。

## 3. 豁免通道（怎么让项目说"这条规律不适合我们"）

```bash
# 项目宪法显式覆盖（留痕，可审计）
pi-batch evolution override --rule authority-layering \
  --reason "本项目是独立演示 demo，无后端契约" \
  --write-constitution

# 软规则例外（learn 既有机制，90 天复查）
pi-batch learn exception --rule-id authority-layering --reason ...
```

豁免必须留痕：`docs/evolution/constitution.yaml` 记录 override + 理由 +
时间戳；`evolution check` 报告当前豁免清单。**工具给建议，人做裁决。**

## 4. 规律生命周期（learn 闭环）

```
draft（实战事故草案）→ shadow（影子模式：只记录命中率不拦截）
→ promote（并入 evolution-specs/rules.yaml，带 samples/confidence）
→ exception（项目级豁免，90 天复查）→ deprecate（反例累积/样本失效）
```

- promote 时必须携带 `samples`（归纳出处）与 `confidence`（归纳强度）；
  低置信度规律默认只以 suggestion 形态生效。
- 长期未命中或被豁免的规律在 `evolution report` 中标 `stale`，
  由维护者决定降级或废除（记忆可衰减）。

## 5. 规律自身可被挑战

- `pi-batch evolution report --self-review`：审查规律本身——
  命中率下降？规律外的好方案被拒过？样本是否过时？
- 探索任务的产出是**新规律的来源**（自举）：spike 结论 → `learn draft`
  → 评估后并入。规律库随项目演化，不冻结。
