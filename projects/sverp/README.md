# sverp（sverpweb-stable × iris-ui 双项目持续开发工作区）

用 ai-batch-runner 对 `~/workspace/job/sverpweb-stable`（ERP 前端，Vue 3.5）
与 `~/workspace/job/iris-ui`（组件库，@iris-ui-kit/vue）做持续优化、修正与
开发。**核心纪律（Source-first）**：iris-ui 有问题必须在 iris-ui 源码修复
→ 发布 npm 包 → sverpweb-stable 升级依赖再 import；禁止改
sverpweb-stable 里 iris-ui 的 dist。详见 [WORKFLOW.md](WORKFLOW.md)。

## 文件清单

| 文件 | 用途 |
|---|---|
| `WORKFLOW.md` | 双项目工作流契约（硬约束） |
| `campaign-sverpweb.yaml` | sverpweb-stable 模块扫描 campaign |
| `campaign-iris-vue.yaml` | iris-ui vue 包模块扫描 campaign |
| `sverpweb-campaign-pipeline.yaml` | sverpweb 侧实现流水线模板（source-first 门禁） |
| `iris-campaign-pipeline.yaml` | iris-ui 侧实现流水线模板（无 dist 门禁） |
| `iris-fix-publish-pipeline.yaml` | 单问题修复流水线（诊断→源码修复→审查→裁决） |

配套脚本在 `../../scripts/sverp/`（check-iris-vue / check-sverpweb /
check-no-dist-edit / iris-publish / run-campaign）。

## 命令速查

```bash
# 1) sverpweb-stable 持续改进（先 dry-run 看方向）
bash scripts/sverp/run-campaign.sh sverpweb --dry-run
bash scripts/sverp/run-campaign.sh sverpweb --max-directions 2 --reuse

# 2) iris-ui 组件库持续改进
bash scripts/sverp/run-campaign.sh iris --dry-run
bash scripts/sverp/run-campaign.sh iris --max-directions 2 --reuse

# 3) 单个 iris-ui 缺陷修复（在 iris-ui 仓库内执行）
cd ~/workspace/job/iris-ui
python /home/dwp/ai-batch-runner/pi-batch.py \
  /home/dwp/ai-batch-runner/projects/sverp/iris-fix-publish-pipeline.yaml \
  -p "NavMenu 在 x 场景下交互异常…" --retries 3 --log-file logs/iris-fix.log

# 4) 发布循环（gate PASS 后；--dry-run 先预览）
bash scripts/sverp/iris-publish.sh --dry-run
bash scripts/sverp/iris-publish.sh

# 5) 门禁单独运行
bash scripts/sverp/check-iris-vue.sh ~/workspace/job/iris-ui
bash scripts/sverp/check-sverpweb.sh ~/workspace/job/sverpweb-stable
bash scripts/sverp/check-no-dist-edit.sh ~/workspace/job/iris-ui
```

## 当前状态（2025-08 基线）

- iris-ui：`@iris-ui-kit/vue` 已发布至 0.2.18；工作树有未提交的 NavMenu
  交互改进（版本已 bump 至 0.2.18）。遗留的 `iris-ui-kit-vue-0.2.16.tgz`
  已移出仓库至 `~/workspace/job/backup-iris-tgz/`（无任何引用，且会拦截
  `check-no-dist-edit` 门禁）。`.pi-batch/` 已加入两仓库 .gitignore。
- sverpweb-stable：`apps/web` 依赖 `@iris-ui-kit/vue@0.2.18` + `core@0.2.0`；
  typecheck 通过。无 git remote（本地仓库）。
- 双仓库基线门禁均通过（见上方命令 5）。

## 冒烟验证（2025-08 已跑通）

- 端到端分析链路：对 iris-ui `packages/vue/src/undo` 模块执行 campaign
  分析提示词 → LLM 返回结构化 JSON → `parse_directions` 解析出 3 个
  可实施方向（含 evidence + acceptance）：
  1. `push(undefined)` 合法快照被 sync 守卫当 no-op → state 失步（价值 7）
  2. 消费者直接改返回的 raw `stack` 时响应式 state 失步（价值 7）
  3. 推入 live/reactive 对象时的快照别名风险（proposed，价值 6）
- 该分析同时验证了双仓库门禁脚本与流水线 schema 全部通过。

## 首个完整修复（2025-08，iris-fix-publish-pipeline）

对冒烟分析方向 1（useUndoStack 状态失步）跑完整修复闭环
（diagnose → fix → review → gate，约 6 分钟，全部 PASS）：

- **根因**：`packages/vue/src/undo/useUndoStack.ts:73/79` 用返回值
  是否为 undefined 判断 no-op，但泛型 T 允许 `undefined` 作为合法快照。
- **修复**：`undo()/redo()` 无条件 `sync()`（no-op 时写入相同值无害），
  API 完全兼容；回归测试用真实 DOM 绑定验证 canUndo/canRedo/index。
- **门禁**：typecheck + 1326 tests（134 文件）+ build 全绿；
  无 dist 产物（source-first 红线守住）。
- **待跟进**：react/solid/svelte 三个 bridge 的 `useUndoStack` 存在
  同类守卫缺陷（诊断阶段发现），后续按同一流程修复。
- **发布**：gate PASS 后运行 `iris-publish.sh` 即可发布 0.2.19 并升级
  sverpweb-stable（当前修复仍留在 iris-ui 工作树，未提交）。
