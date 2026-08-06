# 双项目持续开发工作流契约（sverpweb-stable × iris-ui）

> 本契约是 ai-batch-runner 驱动 `~/workspace/job/sverpweb-stable` 与
> `~/workspace/job/iris-ui` 持续开发时必须遵守的**硬约束**。所有流水线
> 提示词、验证器门禁与发布脚本都围绕本契约设计。

## 1. 依赖方向（单向）

```
iris-ui（上游组件库） ── npm publish ──► npm registry ──► sverpweb-stable（下游应用）
   packages/vue/src/** 源码                 @iris-ui-kit/vue（精确版本）
```

- sverpweb-stable 的 `apps/web` 通过 npm registry 以**精确版本**
  （当前 `@iris-ui-kit/vue: 0.2.18`、`@iris-ui-kit/core: 0.2.0`）消费 iris-ui。
- 依赖只能从上游流向下游。sverpweb-stable 的代码**永远不能反向影响**
  iris-ui 的运行时行为。

## 2. Source-first 红线（禁止事项）

iris-ui 有问题时，**禁止**：

1. 直接修改 sverpweb-stable 中 `node_modules/@iris-ui-kit/*` 的任何文件
   （dist、d.ts、package.json 一律禁止）。
2. 把 iris-ui 的 dist 产物复制进 sverpweb-stable 当作依赖或补丁。
3. 手工编辑 sverpweb-stable 的 `pnpm-lock.yaml` 中 iris-ui 相关条目。
4. 在 sverpweb-stable 里为 iris-ui 缺陷写 workaround（绕过组件直接操作
   DOM、样式覆盖去对抗组件结构等）。如确需临时 workaround，必须经
   gate 明确裁决并写入 `docs/auto/DECISIONS.md` 记录原因与回退计划。
5. 在 iris-ui 仓库修改或提交 `dist/`、`*.tgz`（构建产物，永不提交）。

**唯一合法路径**（发布循环）：

```
iris-ui 源码修复（packages/vue/src/**）
  → 门禁：typecheck + test + build + 无 dist 产物（check-iris-vue + check-no-dist-edit）
  → 对抗审查 + VERDICT 裁决门
  → iris-publish.sh：版本递增 → npm publish → sverpweb-stable 升级依赖
    → pnpm install → sverpweb 门禁（check-sverpweb）→ 回归验收
```

## 3. 问题归属路由（先判定再动手）

| 现象 | 归属 | 处理 |
|---|---|---|
| 组件行为/视觉/交互缺陷（NavMenu、Table、Modal、Form、布局、token） | iris-ui | `iris-fix-publish-pipeline.yaml`（iris-ui 仓库） |
| 页面组装、业务逻辑、状态管理、路由、API 层 | sverpweb-stable | campaign-sverpweb 或 sverpweb 侧流水线 |
| 不确定 | 先最小复现 | 在 iris-ui playground 复现 → 复现成功归 iris-ui；否则归 sverpweb |

campaign 分析阶段会自动执行该路由：sverpwebweb 侧分析发现 iris-ui 缺陷时
输出 `IRIS-UI-FINDINGS` 而非实现方向；iris-ui 侧分析只允许
`packages/vue/src/**` 范围内的方向。

## 4. 版本与发布纪律

- sverpweb-stable 锁精确版本（无 `^`/`~`），升级必须显式发生。
- 版本递增默认 patch（`0.2.18 → 0.2.19`）；破坏性变更必须 bump minor 并
  在 changeset 中说明迁移。
- 发布只允许通过 `scripts/sverp/iris-publish.sh`：
  - `--dry-run`：打包检查 + 预览将发生的依赖变更，**不发布不安装**。
  - 默认：门禁 → 版本递增 → `npm publish` → sverpweb-stable 升级 →
    `pnpm install` → sverpweb 全门禁。
- 发布完成后在 sverpweb-stable 手动冒烟受影响页面。

## 5. 门禁清单（验证器）

| 门禁 | 脚本 | 作用 |
|---|---|---|
| `check-iris-vue` | `scripts/sverp/check-iris-vue.sh` | iris-ui vue 包 typecheck+test+build |
| `check-sverpweb` | `scripts/sverp/check-sverpweb.sh` | sverpweb-stable apps/web typecheck+test+build |
| `check-no-dist-edit` | `scripts/sverp/check-no-dist-edit.sh` | 工作树无 dist/ 与 *.tgz（source-first 红线） |

## 6. 持续开发节奏（建议）

```
每轮（人工或 7×24 轮循环）：
1. run-campaign.sh sverpweb --reuse --max-directions 2   # 应用侧改进
2. run-campaign.sh iris --reuse --max-directions 2       # 组件库改进
3. 两个仓库各自的 gate FAIL / IRIS-UI-FINDINGS → 路由处理
4. iris-ui 侧 PASS 的方向 → iris-publish.sh 发布+升级
5. sverpweb-stable 回归验证 → 人工评审 → 提交
```

所有运行日志入 `logs/`，决策记录入各仓库 `docs/auto/DECISIONS.md`。
