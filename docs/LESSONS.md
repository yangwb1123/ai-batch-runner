# 实战校准记录（6 仓库组合维护场景）

> 2026-08-05 真实运行记录：用 ai-batch-runner 维护 snaplink 平台 6 仓库
> （决策流水线端到端 + 分仓实施 + 协作者并行）。每条 = 观察到的真实
> 缺陷/模式 → 根因 → 处置（已修/待修/工作流纪律）。

## A. 工具缺陷（已修复）

| # | 缺陷 | 根因 | 修复 |
|---|---|---|---|
| A1 | `from_prompt` 无法注入外部需求；我发明的 `{user_prompt}` 占位符不存在，模型诚实拒绝但 runner 判 PASS → 整链虚假通过 | from_prompt 不走 `resolve_prompt`；runner 只检查非空输出 | from_prompt 支持 @file 引用；`scripts/check-no-refusal.py` 把"拒绝工作"识别为失败 |
| A2 | 事件名 `vault.file.deleted@1.1` 的 `@1.1` 被 @file 正则误捕 + `models.py` 引用未导入的 `log` | 正则 `@(\S+)` 过宽；漏导入 | 正则收紧为带扩展名的路径；补导入；读取失败降级警告 |
| A3 | judge/gate 的 VERDICT 正则不认 markdown 加粗 `**VERDICT: PASS**` | 正则需要行首 `VERDICT:` | 容忍可选星号（runner + pipeline 两处，测试覆盖） |
| A4 | quality.py 无排除机制，仓库内 `ai-dev/` 旧工具目录污染门禁 | rglob 全树扫描 | `--exclude PATH`（空格/等号两种形式） |
| A5 | 提案类任务挂 `completion` 验证器必然失败 | DoD 门禁语义 = 实现类任务 | 验证器选型纪律（见 C2） |
| A6 | 锁被占用时只能 exit 5，多协作者共享机器需手工排队 | 无排队能力 | `--wait-lock MINUTES`（轮询 30s，预算耗尽才退出；测试覆盖） |

## B. 工作流纪律（工具无法代劳）

| # | 教训 | 规则 |
|---|---|---|
| B1 | 两次 `git add -A` 误收协作者未提交工作（judge.py、rate-limiting 功能） | **共享工作区禁止 git add -A**；只 add 自己的产物路径；pi-batch 的自动提交本来只 add outputs（安全） |
| B2 | 测试依赖外部可变仓库（`test_advance` 扫真实 snaplink-console），外部漂移 → CI 红 | 外部环境依赖测试应隔离或标注；用 stash 实验证明非回归后再汇报 |
| B3 | 协作者并行占用 4 仓库，我们的任务只能排队 | 单实例锁是正确行为；排队用 `--wait-lock`；**绝不 `--no-lock` 并发改同一仓库** |
| B4 | 决策流水线 8 阶段中 5 阶段"虚假 PASS" | 每个阶段都要有验证器；上游拒绝必须显式失败（A1） |

## C. 产品/架构原则（对多项目 AI 编码的通用借鉴）

| # | 原则 | 实战证据 |
|---|---|---|
| C1 | **机械强制 > Prompt 劝说**：能校验的字段绝不依赖 LLM 自觉 | 决策门 4 次真实拦截（JSON 结构/项目引用/失败策略/命名规范） |
| C2 | **验证器与任务类型匹配**：实现类 → completion（DoD）；提案类 → check-no-refusal（拒绝检测）；结构类 → 自定义 JSON 校验 | A5 一次误配的教训 |
| C3 | **对模型宽容的解析层 + 对语义严格的校验层**：VERDICT 提取容忍 markdown 加粗（A3），但权限/事件命名强制 `<domain>.<resource>.<action>[@vN]` 白名单 | 模型两次产出 `file.file.delete` 漂移被拦 |
| C4 | **LLM 会诚实拒绝——拒绝必须被识别为失败** | A1：模型在缺输入时输出"输入缺失 fail closed"文档，若当 PASS 传播则整链虚假 |
| C5 | **结构化产物（JSON）可机械校验，markdown 不行**：决策摘要要求纯 JSON + schema 校验；markdown 阶段只做拒绝检测 | 决策门全部能力建立在 JSON 产物上 |
| C6 | **上游聚合必须完整**：摘要阶段只聚合 change_design 导致模型看不到主责任信息（诚实留空） | 聚合 composition + change 两阶段后通过 |
| C7 | **领域语言符号冲突**：`@` 既是事件版本号又是文件引用前缀 | A2：语法设计需考虑领域内符号占用 |
| C8 | **跨仓库协作 = 契约冻结 + 排队执行**：单实例锁保护仓库；套件（suite）仓库承载组合验证 | 锁/排队/组合仓库三者缺一不可 |

## D. 对产品化的直接启发（下一步候选）

1. `--wait-lock` 已内建 → `wait-for-run.sh` 可退役（保留为无锁工具的参考实现）
2. "决策摘要 + 机械门禁"模式可推广为**通用结构化产物门禁**（任何任务可声明 JSON schema + 校验器）
3. ~~运行可见性 gap~~ **已实现**：`pi-batch ps` + 全局注册表
   （`~/.pi-batch/runs/<pid>.json`，心跳 60s，退出注销，死进程标记/清理）——
   谁在哪个仓库跑什么（mode/repo/session/status/age/argv），排队中显示 queued
4. 环境依赖测试标注（B2）：建议为 test_advance 类测试加 `SKIP_IF_ENV_UNSTABLE` 机制或标记为 xfail-on-drift
