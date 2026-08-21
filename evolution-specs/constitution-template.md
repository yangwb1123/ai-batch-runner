# 项目宪法模板（Constitution Template）

> 由 `pi-batch evolution init` 生成（L1 baseline-first / L10 decision-memory
> 的载体）。宪法是项目自己的规则，不是本工具的规则——本工具只负责在
> 后续任务中**引用**它并校验一致性。字段缺省时门禁只给警告不阻断
> （除 security_invariants 与 explicit_non_goals 之外）。

## 1. 产品基线（baseline-first, L1）

- 产品定位：_（一句话：是什么、不是什么——参考 snaplink-console
  "控制面不是身份服务"式定位句）_
- 用户与核心任务：_（角色 × 目标 × 入口表）_
- 领域边界：_（哪些职责在前端/适配层，哪些在服务端/核心）_
- 权威源清单：_（OpenAPI 目录 / backend-contracts.json / manifest.json /
  服务端运行时清单——按实际填写，没有就写"无（纯前端/独立项目）"）_

## 2. 明确不做（non-goal-restraint, L6 — 硬边界）

```yaml
explicit_non_goals:
  - <例如：不在浏览器内重新实现 Token/签名/协议执行面>
  - <例如：未经维护者授权不执行首次 npm 发布>
```

> 方向撞此清单 → `pi-batch evolution check` 与 campaign 选择器 fail closed
> 拒绝（必须带理由豁免，豁免留痕 90 天复查）。

## 3. 安全不变量（machine-gates, L3 — 硬底线）

```yaml
security_invariants:
  - <例如：秘密最小驻留、写操作不自动重放、服务端权限权威>
```

> 任何任务不得违反；涉及时由 adversarial-gate（L5）强制对抗审查。

## 4. 变更预算（change-budget, L9）

```yaml
change_budget:
  max_files: 5        # 单迭代最多改动文件数（0 = 不限）
  max_lines: 300      # 单迭代核心逻辑行数预算
  allowlist: []       # 允许触碰的文件 glob（白名单模式：非空时只许改这些）
  denylist: []        # 禁止触碰的文件 glob
```

## 5. 决策记忆（decision-memory, L10）

- 每条决策日志条目必须含：决策 / 原因 / 影响 / **被否定方案与理由**。
- `pi-batch evolution suggest` 会引用历史决策缓存，避免重复建议被拒方向。

## 6. 门禁注册（machine-gates, L3）

在 `pi-batch.yaml` 注册与项目匹配的验证器（基线/权威源/证据/预算/
明确不做），代码类阶段无验证器时 `pi-batch doctor` 显式警告。
