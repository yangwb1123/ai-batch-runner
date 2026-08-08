# Campaign 实战经验内化（2026-08-08 两轮 repository-improvements）

> 本文记录用本仓库自身 campaign 能力跑真实改进循环（10 模块 × 2 轮）暴露的
> 经验与已内化的修复。原则：**实战发现的机制缺口 → 确定性修复 + 回归测试 +
> 文档**，不靠"下次注意"。

## 1. 运行事实

| 项 | 首轮 | 续跑（--retry-failed --reuse） |
|---|---|---|
| 时长 | 5h52m（01:00–05:52） | 4h48m（06:05–10:53） |
| 结果 | 2 passed / 8 failed | 2 passed / 8 failed |
| 工具指纹 | 变更（pbatch 修改）→ 分析失效重做 | 同左 |

4 个方向完整通过（design_gate + acceptance 双门 PASS，代码真实落盘）：
package-pbatch-data-files、execute-expect-only-eval-cases、cli-json-stdout、
make-uicheck-uigeometry-dir-configurable。16 个方向被拦截。

## 2. 失败分布与根因

| 失败类别 | 数量 | 根因 | 内化动作 |
|---|---|---|---|
| GATE_REJECTED（设计门禁） | 10 | AI 设计常缺关键证据：行数预算分析（org.py 488+20 超 500）、产物完整性（17 行自称 425 行）、owner 决策未记录 | **不改 gate**（fail-closed 是设计原则）；设计侧由 gate 裁决反馈驱动重生成 |
| PIPELINE_FAILED（adversarial_review） | 6 | meta 角色 agent exited 1（provider 抖动/rate limit），重试 1 次不足 | ✅ `repository-campaign-pipeline.yaml` 加 `meta_role_retries: 2` + `meta_max_failed_roles: 1`（缺失审查由 design_gate 兜底） |
| provider 不可用 | 8 次 | "Model is unavailable" 上游抖动 | 失败签名已识别；重试退避已有 |

## 3. 已内化的确定性修复（均有回归测试）

| # | 实战发现 | 修复 |
|---|---|---|
| 1 | `evolution.yaml` 的 `{log}` 占位符不被 `run_gate` 替换 → **静默假通过**（agent-engineering 分析 score=10 方向；方向被 gate 拒但 bug 真实） | `org.run_gate` 增加 `{log}` 注入（`--log` 参数）；`_execute_check` 检测**残留未知占位符 → failed**（配置错误显式暴露，永不静默） |
| 2 | `ai_batch_runner.egg-info` 被当作模块分析（浪费一轮流水线，方向全是打包噪音） | `campaign_analysis._excluded` 增加打包产物目录（egg-info/__pycache__/dist/build/node_modules/.venv）——模块发现 10→9 |
| 3 | repo-internal 规则 PY-GODFILE-SPLIT 错位在 backend 产品注册表（backend-specs 是发给外部后端项目的规范资产） | 从 `backend-specs/rules.yaml` 移除；实践保留在 `docs/rules/promoted/`（本仓库内部知识） |
| 4 | product-specs 方向产出的 `product-specs/rules.yaml`（声明式注册表，克制设计：signals 空） | 保留并验证：schema 通过、rule_matcher product 域已映射（L0 任务永不触达） |

## 4. 运营经验（非代码）

- **串行 10 方向 ≈ 5-6 小时**：多方向并行用 `--parallel-pipelines N`（要求干净
  工作区 + 独立 worktree）。本次因工作区有历史未提交改动无法并行。
- **`--retry-failed` 语义**：工具指纹（TOOL_VERSION）变更后分析失效 → 重新分析
  → 可能选**新方向**（不是原失败方向）。想重试原方向须保持工具不变。
- **GATE_REJECTED 是成本信号**：设计阶段高拒绝率说明"设计文档证据标准"是
  当前瓶颈；重试不会改善（同设计重审仍拒），应人工补证据或换方向。
- **Provider 抖动期**：观察 `logs/campaign-*.log` 的 `META retry` 与
  `server_error` 计数；连续失败可停跑（`kill <pid>` 进程组）等恢复后
  `--retry-failed --reuse` 续跑，已有产物指纹复用不浪费。
- **归档**：PASS 方向产物自动归档 `run/archive/`；FAIL 方向保留现场
  （artifacts + DECISIONS.md）供人工排查。

## 5. 后续建议

- 观察 gate 裁决模式：若 GATE_REJECTED 集中于同一类缺失（如行数预算），
  在设计 prompt 中前置该证据要求（模板层改进，不改 gate 强度）。
- 对 16 个被拦截方向：人工按 gate 裁决补证据后逐方向重试（或直接按
  analyses/*.json 中已发现的问题手工实现——分析本身质量高，多是真实问题）。

---

# Campaign 实战经验内化（2026-08-08 外部目标仓库 snaplink-console B6-advance）

> 用本工具驱动**外部 Flutter 仓库**（snaplink-console）两轮 campaign（每轮
> 5 模块分析 + 方向 SDLC 管线，约 5 小时），全部 6 条管线被 design_gate/
> implement 拦截，但工作树最终落下全部功能（人工验证后分批提交，901/901
> 测试绿）。本节记录外部仓库场景暴露的机制缺口与已内化修复；原则不变：
> **实战发现的机制缺口 → 确定性修复 + 回归测试 + 文档**。

## 1. 运行事实

| 项 | 首轮 | 续跑（--retry-failed） |
|---|---|---|
| 时长 | 23:41–02:26（约 3h，6 条管线） | 07:13–08:21（约 1.2h，2 条管线） |
| 结果 | 0 passed / 5 failed | 0 passed / 5 failed |
| 落地 | 工作树 31 个未提交文件（常量、守卫、drill、提案） | 工作树 +26 文件 |

两轮全部失败但工作树持续产出**真实、已验证的代码**：gate 拦截的是"设计文档
证据纪律"，不是 agent 的实现能力。人工按 gate 裁决 + 规格落地后全部需求点
完成（B6-1a/1b/1c、B6-2 全 lens、portal session-expiry rollback pin）。

## 2. 失败分布与根因

| 失败类别 | 数量 | 根因 | 内化动作 |
|---|---|---|---|
| implement 校验失败（exit 1/127） | 2 | **目标仓库 quality.py 基线 14 个存量违规**：校验器在基线上 fail-closed，agent 质量与结果无关 | ✅ **campaign 前 validator 基线预检**（`_preflight_validators` + `--preflight-strict`）：管线启动前对目标仓库跑一次 repo-scope 校验器，基线红时警告表/中止 |
| GATE_REJECTED（design 文档未修订） | 4 | 对抗审查后 design 文档保持 pre-review 状态，无 amendment/拒绝记录 | 不改 gate；作为运营教训：设计阶段拒绝率高时应人工补证据（见 §4） |
| 方向级 PIPELINE_FAILED | 2 | meta 角色 agent 退出 / 设计校验失败 | 已有 meta_role_retries；外部仓库场景确认有效 |

## 3. 已内化的确定性修复（均有回归测试）

| # | 实战发现 | 修复 |
|---|---|---|
| 1 | **校验器基线 fail-closed 吞掉整轮**：pyquality 在目标仓库 14 个存量违规上 exit 1 → 全部 implement 阶段失败，与 agent 质量无关；两轮 5h 后才由人工清理基线 | `pbatch/campaign_exec.py` 新增 `_preflight_validators`：管线启动前解析模板中 repo-scope 校验器（{cwd} 替换、跳过 judge/file-scope），逐个对目标仓库运行并输出基线表；`--preflight-strict` 时基线红即 SystemExit(4)。测试：`tests/test_campaign_preflight.py`（8 用例：基线失败报告、strict 中止、file/judge 跳过、{cwd} 替换、空模板 noop） |
| 2 | **被 gate 的管线在工作树留下 20-30 个未提交文件**（真实代码 + 试验性编辑混合），campaign 结束零提示，人工盘点两轮 | `_report_working_tree_residue`：campaign 结束输出 `git status --short` 计数（modified/untracked/staged + 前 15 行文件清单）。测试：residue 报告、clean 静默、非 git 目录静默 |
| 3 | **目标仓库自身的 SKIP 假 PASS 模式**：drill exit-0 的 [proposed] 腿被 run_all.py 计为无条件 PASS（gate 发现 3 复现） | 工具侧教训：exit-code 校验器无法区分 SKIP/PASS；已在文档约定目标仓库 harness 用 `skip_markers` 机制（snaplink-console 已实现） |

## 4. 运营经验（外部仓库场景）

- **管线 gate 失败 ≠ 无产出**：requirements/design/验证阶段的 agent 常在目标
  仓库留下可用代码（常量、守卫测试、drill 扩展）。campaign 结束后先
  `git status` 盘点工作树再决定重跑或人工落地——residue 报告现已自动输出。
- **重跑前先查校验器基线**：目标仓库 `quality.py`/`cli.py check` 基线红时，
  任何重试都重复同一失败；先人工清理基线（或加豁免）再 `--retry-failed`。
- **GATE_REJECTED 是设计证据问题，重试不解决**：裁决文本具体可执行（死断言、
  不可测声明、未处置审查项）；按裁决修设计或直接按规格手工落地（本场景后者
  更快：portal rollback pin 按 spec 直接实现，33/33 绿）。
- **--retry-failed 会选新方向**：目标仓库代码变化 → 分析指纹变化 → 重分析
  选新方向；想重试原方向须保持仓库与工具不变。
- **工具自身状态也会坏**：本轮发现工具仓库带有一个已提交的坏 merge
  （pbatch/cli.py 冲突标记残留在工作树，导入即 SyntaxError）——自迭代轮次
  的 round-commit 把合并状态提交了。教训：`--round-commit` 前必须确认
  工作树无冲突标记（`grep -r '<<<<<<<'` 门禁）。
- **正面证据**：无 role_dir 模板时 meta 编排的 ad-hoc 角色质量高（probe/
  mutation/security 审查抓到死断言、假 PASS、wire-count 不可测声明）；
  analysis 方向选择精准命中 pending 提案。
