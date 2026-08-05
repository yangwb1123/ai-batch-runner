# ai-dev — AI-SDLC 工具集说明（Agent 使用指南）

> **本仓库（ai-batch-runner standalone）适配**：本仓库根目录就是上游 `ai-dev`
> 内容本身，所有命令与路径均**不带 `ai-dev/` 前缀**（如 `python pi-batch.py`、
> `prompts/`、`scripts/`）；上游的 `make ai-dev-test` 对应本仓库 `make ci`
> （工程门禁见 `docs/CHECKS_REGISTRY.md`）；`pi-batch.yaml` 的
> `validators.pyquality` 路径已适配为 `python {cwd}/quality.py {cwd}`。
> 其余内容与上游同步。

`` 是 Snaplink 仓库内的可选 AI 编排框架：用 CLI agent（pi，可换成
claude/codex/gemini 等）驱动软件开发流程——分析任意项目/想法、按角色产出
交付物、自优化增删角色、交叉对抗验证、7×24 无人值守。它**补充而非替代**
仓库的工程门禁（`make ci`、`python cli.py check` 等）；AI 产出是**提案**，
须按证据标准核对后提升到维护文档。

## 1. 组件清单

| 组件 | 用途 |
|---|---|
| `pi-batch.py` | 入口薄壳（24 行）：原命令全部不变，实现见 `pbatch/` 包；`pip install .` 后提供全局 `pi-batch` 命令（`python -m pbatch` 同入口） |
| `pbatch/config.py` | 声明式配置解析（pi-batch.yaml）、agent 默认值、会话标志、验证器注册表 |
| `pbatch/models.py` | 数据模型：Task / TaskResult / Stage / Pipeline |
| `pbatch/runner.py` | 任务执行：硬超时、失败签名拒绝、重试、验证门禁、摘要 |
| `pbatch/pipeline.py` | 流水线：阶段构建、meta 角色编排、gate 裁决、决策日志、归档 |
| `pbatch/pipeline_schema.py` / `evidence.py` | Pipeline fail-closed schema 与有界证据注入 |
| `pbatch/relevance.py` | meta 角色关键词评分、命中证据、去重与运行时数量上限 |
| `pbatch/classifier.py` / `task_keywords.yaml` | 任务类型判断器：执行前双语关键词判定，前端→前端实现流水线、后端→后端实现流水线（`classifier.frontend_pipeline` / `backend_pipeline`，均为 plan→implement+门禁→对抗审查→gate 结构），`--classify` / `classify` 子命令，零 LLM 成本 |
| `pbatch/rule_matcher.py` / `ui-specs/rules.yaml` | 规则匹配器：按 scale（demo/standard/production）× page_type × risk 三轴算法式筛选适用规范；**双向校验**——算法 manifest 与 LLM 独立选择对比裁决（`rules --summary/--llm-json/--llm-prompt`），必选规则不可被 LLM 跳过（fail closed） |
| `pbatch/assessor.py` | 需求评估器（`assess` 子命令）：资深经验式评审——8 维需求完整性检查、规模信号（S/M/L）克制处方（"企业登录页"不会全量上规则）、缺失维度建议、刻意未选用清单、**产品化分级评估**（L0-L3）、**工作流风险分级**（L0 直改 / L1 标准 / L2 高风险对抗 / L3 平台产品） |
| `pbatch/product.py` / `product-specs/` | 产品思维层：productization_level（L0 小工具不推演 / L1 模块 / L2 平台 / L3 产品）→ 场景推演链（登录/上传/审批/聊天/ERP/搜索，产出待确认问题非功能）、开源要素、商业要素（低成本预留 vs 高成本实现红线）、完成证据报告 |
| `pbatch/learn.py` | 自演进闭环（Evolution Engineering）：`learn` 子命令把事故/RCA 沉淀为统一 Rule Schema 草案（docs/rules/drafts/），人工批准后并入域注册表 |
| `pbatch/context.py` / `docs/agent-context/` | 上下文路由（Context Engineering）：按文件路径 glob + 任务关键词加载项目文档（ADR/契约/安全基线），`context` 子命令，缺失文档显式警告 |
| `pbatch/eval.py` / `evals/` | 规则系统回归套件（Evaluation Engineering）：`eval` 子命令跑 23 个断言用例（classify/rules/assess/product/workflow），改关键词或注册表后验证行为不退化（make ci 内自动执行） |
| `scripts/check-completion-report.py` | Definition-of-Done 门禁（`completion` 验证器）：产物必须携带诚实完成报告——result 枚举/not_executed 必带原因/禁止伪造通过表述 |
| `backend-specs/` | 后端规范资产（按 classify 的 backend 域自动选用）：architecture、design-patterns、domain-modeling、evolution、oop-and-di、ddd、algorithms-data-structures、complexity-and-scale、**persistence-modeling**（四种模型分离/主键/金额/索引/唯一约束/审计历史/迁移，编码前强制关卡）、**production-readiness**（生产就绪门禁 11 项）、**agent-guardrails**（防幻觉/变更控制/DRY 边界）、**architecture-constitution**（最高层架构宪法：变化管理/可逆决策/耦合八型/爆炸半径/重试预算/一致性显式/15 问）、**testing**（五层测试/复现优先/属性模糊测试）、**system-engineering**（消息/缓存/后台任务/限流/分布式锁）、**network-engineering**（超时分层/重试矩阵/背压）+ `rules.yaml` 后端注册表 |
| `scripts/check-backend-quality.py` | 后端工程门禁（纯 stdlib）：过度设计信号（单实现接口/Base 类/模糊命名）、上帝服务、状态直接赋值、domain→infra 反向依赖（`backendquality` 验证器） |
| `ui-specs/` | AI UI 生成规范资产：8pt 间距 token、组件/布局/反例规范、业务风格 profile（ERP/CMS/OA…） |
| `scripts/check-ui-spec.py` | 确定性 UI 门禁（纯标准库）：魔法间距/硬编码颜色/inline style 机械拦截（`uispacing`/`uicolor`/`uistyle` 产物验证器）；`--dir` 项目级扫描（`uicheck` 门禁，gofmt -l 风格，JSON 报告） |
| `scripts/check-frontend-quality.py` | 前端工程门禁（纯标准库启发式）：console.log/any/不安全 html/空 catch 吞异常/测试 skip 恒失败，`--strict` 下上帝文件/复杂度/useEffect 泄漏/循环 await（N+1）失败（`uiquality` 验证器）；需推理的交给工程审查角色 |
| `ui-specs/engineering/` | 前端工程规范资产：异步/防抖竞态/幂等决策表、表单表格状态、交互模式库（操作分级/反馈闭环/批量报告）、错误恢复与权限、架构预算、缺陷模式库（12 种事故档案） |
| `pbatch/memory*.py` / `index_io.py` | 渐进式 message memory：稳定会话、有界索引、按需 recent/find/read |
| `pbatch/campaign*.py` | 项目级模块发现、方向选择、状态/指纹、流水线与 worktree 隔离 |
| `pbatch/cli.py` | 命令行：参数解析、main、轮循环、单批/流水线分发 |
| `quality.py` | 代码组织质量扫描器（纯标准库）：函数 ≤50 行、复杂度 ≤15、文件 ≤1000 行 |
| `ai/run-review.py` | 十阶段评审 runner（`--all`/`--resume`/共享会话/验证门禁） |
| `ai/prompts/00-09` | 评审阶段模板（产品发现→CTO 决策） |
| `ai/prompts-shared/` | 评审共享片段（工程原则、输出格式、检查清单、角色定义） |
| `prompts/` | 31 个角色模板（architect、security_engineer、qa_lead、backend_engineer、product_thinker…）+ 30 个关键词注册，meta 编排动态挑选 |
| `pipelines/` | SDLC 角色图与代码实现流水线（设计图） |
| `examples/` | 可用案例：提案、SDLC、meta 分析、完整闭环与 repository campaign |
| `tests/` | 387 个回归测试（`make ci`，含 25 个 eval 规则回归用例） |
| `docs/SPEC_SYSTEM.md` | 规范体系总览：资产分层/注册表/决策链/门禁/角色/沉淀闭环/克制原则 |
| `docs/` | 机制文档（`RUNNING_247.md`、`AUTOMATION_WORKFLOW_SUMMARY.md` 等） |
| `pi-batch.yaml` | 声明式配置：agent 二进制、会话标志、6 个验证器（含 `pyquality`） |

**可移植性**：把 `pi-batch.py`（薄壳）+ `pbatch/` 目录 + `pi-batch.yaml`
复制到其他项目即可用；配置查找顺序为入口脚本旁 → 包目录 → 工作目录。

## 2. 能力机制

### 2.1 任务与输入方式

| 起点 | 用法 |
|---|---|
| 一句话 | pipeline 阶段 `from_prompt: "..."` + `output`（无需任何输入文件） |
| 目录文档 | 阶段 `from_dir: docs/requirements` |
| 阶段串联 | `from_outputs: <阶段名>` 或阶段名列表（`aggregate: true` 把多个上游全部产出合并为一份证据） |
| 单批 | `-p "prompt"` / stdin / YAML `tasks:` 列表 |

结构化 task/pipeline 文件启动前执行 fail-closed schema 检查：阶段名和上游引用、
输入源、任务/命令类型、正数 worker/timeout/meta 上限、task validator 以及
prompt template 路径必须有效；坏 YAML/JSON 不会降级成普通 prompt 交给 agent。
CLI 的负数/零值和 NaN/Inf 延迟同样在创建日志、锁或子进程之前以退出码 2 拒绝。

阶段证据采用“产物完整保留、prompt 有界注入”：`pi-batch.yaml`
`evidence.max_bytes`（默认 64KiB）是一次 aggregate/meta prompt 的来源内容总预算，
`evidence.max_sources`（默认 64）独立限制路径遍历、fence 和 `{input_path}` 清单。
选中的多个来源公平分配预算；截断标记保留完整产物路径，省略标记保留计数，
LLM 可在确有需要时按需读取，
meta 每轮从产物清单重建上下文而不是递归复制旧 prompt。
最终 prompt 还受 `prompt.max_bytes`（默认 120KiB）硬门约束，以低于 Linux 常见
128KiB 单 argv 上限；超限在启动 agent 前明确失败，避免不可诊断的 `E2BIG`。

### 2.2 结果校验（错误处理）

agent 回复**不是正确输出时一律不落盘**：

- 退出码非 0、空输出 → 拒绝
- 失败签名（即使退出码 0）→ 拒绝：
  - 额度/速率/计费/认证：`insufficient_quota`、`rate_limit_error`、
    `quota_exceeded`、`429 Too Many Requests`、`authentication_error`…
  - 网络/断网：`network is unreachable`、`no route to host`、DNS 失败、
    `connection refused/reset`、`curl: (N)`、`ECONNREFUSED`…
  - CLI 横幅：行首 `ERROR:` / `fatal:`
- 防误判：不匹配宽泛词（`error`/`timeout`/`401`），评审正文不会被误杀
- `--timeout`（默认 900s，配置 `default_timeout` 可覆盖）超时 → 进程组
  SIGKILL，拒绝并零孤儿进程。硬超时按绝对截止时间执行（管道线程不延长
  窗口）；pi 侧 HTTP 空闲超时 `httpIdleTimeoutMs` 默认 300s（项目级
  `.pi/settings.json` 已调大到 900s，两者对齐）
- stdout/stderr 以固定 8KiB 字符块排空；即使 agent 输出超长无换行单行，
  也不会在 `OUTPUT_MAX_BYTES` 截断判断前由 `readline()` 全量载入内存
- 拒绝 = 无产物文件；任务失败 → 可自动重试/下轮重跑

### 2.3 工程质量验证

声明式验证器注册表（像 `engineering.yaml` 之于 `cli.py`）：

```yaml
# pi-batch.yaml（6 个已注册）
validators:
  quick: "python cli.py check"
  gofmt: 'test -z "$(gofmt -l {output})"'
  build: "go build ./... && go vet ./..."
  config: "python cli.py config-validate"
  root: "python cli.py check-root"
  pyquality: "python {cwd}/quality.py {cwd}"
```

- `--validate quick,gofmt`：命名引用，AND 语义（全部通过才落盘）
- **每阶段可选**：任务 `validate` > 阶段 `validate_cmd` > CLI > 无；
  空字符串显式禁用（分析类任务跳过，代码生成类保留）
- 机制：输出写临时文件（`{output}` 占位符）→ 验证命令 → 通过则原子
  rename，失败则删除零残留
- 外部验证器 stdout/stderr 分别受 `validation.output_max_bytes`（默认 256KiB）
  约束；超限按验证失败关闭，避免 noisy gate 在超时前耗尽 runner 内存
- stage `commands` 同样使用进程组硬截止与有界诊断；全局默认由
  `commands.timeout/output_max_bytes` 控制，阶段可用
  `command_timeout/command_output_max_bytes` 覆盖

### 2.4 断点续跑与 7×24

| 参数 | 作用 |
|---|---|
| `--reuse` | 当前 validator 复验已有输出；通过才复用，stage `commands` 仍重跑 |
| `--retries N` | 任务失败自动重试，指数退避；速率/网络类至少等 30s |
| `--max-rounds N`（0=无限） | 轮循环直到全部通过，`--round-delay` 轮间休息 |
| `--min-interval` | 成功任务间节流 |
| `--log-file` | 时间戳日志落盘（配合 `tail -f`/systemd 监控） |
| `--stream-output auto|full|none` | 模型正文实时展示策略；默认仅 TTY 展示 |

配套：`run-review.py --all --resume` 从磁盘输出续跑并接力链上下文；已有阶段会先
经过本次命令指定的 validator，失效产物自动重新生成。

运行日志与 message 分层管理：`--log-file` 只记录生命周期、重试、验证和
Gate 等运营元数据；完整模型正文保留在任务产物和 agent 原生会话中。
`auto` 在 nohup/CI/systemd 等非 TTY 场景不实时复制正文，`full`/`none` 可显式
覆盖。无 `output` 的单问答仍会在任务结束时把最终答案输出一次。Campaign
隔离流水线由子进程的轮转 handler 单独写日志，避免同一路径双写和轮转竞争。

### 2.5 会话模式

```bash
--session-mode new        # 默认：每次调用新会话
--session-mode shared     # 整个批次/流水线一个会话（后续步骤延续上下文）
--session-mode per-stage  # 流水线每阶段一个会话
```

- 标志来自 `pi-batch.yaml` `agent.session_flags`（pi 风格默认，可适配其他 CLI）
- 共享会话要求串行（并行会乱序，runner 拒绝该组合）
- 会话 id 由 `--session-name` 派生（可重现）→ 断点续跑延续同一会话

### 2.5.1 渐进式 Message Memory

Pi 原始 JSONL message 保持不变；runner 为新任务分配可追踪的 session id，并把
任务、验证器、Gate、Campaign、失败、产物和归档路径写入追加式轻量索引：
`.pi-batch/memory/sessions.index.jsonl`。索引只保存脱敏摘要、哈希和路径，不复制
完整 message；Manifest/recent/read 从文件尾按块读取，find 流式扫描且只保留
Top-K，均不会先把全部历史装入内存；异常超长索引记录由
`memory.index_line_max_bytes`（默认 256KiB）拒绝或跳过。

成本计量按 session 文件 inode/offset 增量折叠新增记录，不在每个任务结束后重扫
全部历史；`session.line_max_bytes` 只限制协调器读取单条异常 JSONL 的内存，绝不
截断或改写 Pi 原始文件。事件中的 usage 是本次调用前后快照差值。

每次 prompt 只注入有大小上限的 Memory Manifest。初步问题默认不展开历史；
execute 请求只在最近 `memory.manifest_scan` 条元数据的固定窗口内选择领域匹配项，
resume 请求保留时间连续性；当前 LLM 再判断历史是否会实质改善回答并按需调用：

```bash
python pi-batch.py memory ingest                    # 增量导入当前项目已有 Pi sessions
python pi-batch.py memory recent --limit 5
python pi-batch.py memory find "MFA gate failure" --limit 5
python pi-batch.py memory read SESSION_ID --max-bytes 65536
```

`ingest` 可重复执行，只追加新增或已变化的 session 元数据；从旧 message 中静态
提取领域、角色/message 计数、token/cost、观察到的 VERDICT 以及 Pi 的
`errorMessage/stopReason` 失败原因。历史 PASS 只标为 `GATE_PASS_OBSERVED`，失败会标为
`FAILED_OBSERVED`，两者都不冒充已由当前工程门禁验证。

- `--memory-mode auto`（默认）：只对配置中的兼容 agent（默认 `pi`）启用
- `--memory-mode on|off`：显式开启或关闭
- 历史 message 是不可信证据，不作为指令；`read` 限制在 Pi session 目录并有
  字节上限，默认脱敏疑似 secret（人工审计时才显式 `--raw`）；读取和导入
  都使用有限行缓冲，异常超长 JSONL 记录不会被整体载入内存
- shared/per-stage 沿用稳定会话；new/meta/并发角色使用独立、可索引的命名会话
- prompt/task/template 源文件由 `input.max_bytes`（默认 2MiB）有界读取；超限或非 UTF-8
  在启动 agent 前 fail closed，不会被当作空阶段或写入运营日志

### 2.6 自优化（meta 动态角色编排）

```yaml
stages:
  - name: kickoff
    from_prompt: "Analyze the idea: offline-first sync."   # 一句话起点
    output: docs/reviews/kickoff.md
  - name: review
    from_outputs: kickoff
    meta: true                       # 编排者动态挑角色
    role_dir: prompts         # 命名角色模板目录（可缺省）
    role_keywords: pbatch/role_keywords.yaml
    output_dir: docs/reviews
    max_iterations: 3
    max_roles_per_iteration: 3       # 运行时硬限制，不只依赖 prompt
    relevance_min_score: 2           # 命名角色最低相关性；ad-hoc 角色不受此限
```

编排循环：**分析当前交付物 → 输出 JSON 角色计划 → 执行 → 产出折回证据 →
再问 → 收敛**。角色计划两种形式：

```json
["security_engineer", "qa_lead"]                       // 命名角色：role_dir 模板
[{"role": "perf_reviewer", "task": "分析性能瓶颈"}]      // ad-hoc 角色：任务描述+当前语境，无需模板
```

- 选中角色**并发执行**，各自独立 agent 会话
- `--reuse` 时编排器仍基于当前证据重新规划；被再次选中的角色只有在当前
  validator（以及可选 fingerprint）仍匹配时才复用，避免无条件重复模型调用
- 证据折叠：下一轮编排者/角色能看到前几轮结论（自优化闭环）
- 安全：编排输出不可信——命名角色模板查找限制在 role_dir 内（防路径
  穿越），ad-hoc 角色名 sanitize 为安全文件名；`role_dir` 可完全缺省
  （纯 ad-hoc 角色即可运转）
- 相关性：双语关键词按命中评分并把命中证据注入编排 prompt；返回计划会在
  运行时再次去重、按最低分过滤并截断到 `max_roles_per_iteration`

### 2.7 裁决门（交叉对抗验证的阶段目标确认）

```yaml
stages:
  - name: adversarial_review   # 编排者挑多个角色并发交叉审查（对抗视角）
    from_outputs: design
    meta: true
    role_dir: prompts
    output_dir: docs/proposals
    max_iterations: 2
  - name: gate                  # 独立 gatekeeper 裁决：VERDICT: PASS/FAIL
    from_outputs: adversarial_review
    aggregate: true
    gate: true
    tasks:
      - prompt: "... output VERDICT: PASS - <reasons> or VERDICT: FAIL - <reasons>"
        output: docs/proposals/gate.md
```

- `gate: true` 阶段读取产出中的 `VERDICT: PASS|FAIL|REJECT` 行；
  FAIL/REJECT **阻断后续所有阶段**（流水线停止并报告 GATE REJECTED）；
  裸 `VERDICT: PASS`（无理由）或无 VERDICT 行都视为 FAIL（fail closed）
- 交叉对抗由 meta 阶段提供：security_engineer 挑战架构、qa_lead 找缺陷、
  protocol_expert 验线协议……并发执行、证据折叠；gatekeeper 独立裁决

### 2.8 决策日志（每个决策的思考点与理由）

```yaml
# pipeline 顶层，或 CLI --decision-log FILE 覆盖（单批同样支持）
decision_log: docs/DECISIONS.md
```

每个阶段/单批轮次完成后自动追加一条结构化记录：时间、阶段、PASS/FAIL、
裁决结果、决策点摘录（交付物的 markdown 标题 + 首句，完整理由在产物文件
里）、证据路径。追加式保留全程历史——包括被否定的方案和 gate 裁决。
滚动分析（`for i in ...` 循环）每轮追加一条，历史不因覆盖丢失。

### 2.9 git 提交与归档（完成即处理，保持目录整洁）

```yaml
# pipeline 顶层（单批用 --git-commit / --archive-dir）
git_commit: true            # 每阶段完成后提交产物
archive_dir: docs/archive   # 全部成功完成后，把交付物移入 docs/archive/<name>-<时间戳>/
```

- 阶段/任务完成后 git commit（`git_commit: true` 或 `--git-commit`），
  交付物进入版本历史
- **全部成功（无失败阶段、gate 全部 PASS）后才归档**：中间 md 移入
  `archive_dir` 时间戳子目录，工作区只剩决策日志与归档；git 历史保留
  一切，可随时找回
- 滚动分析（`for i in ...` 单批循环）：每轮成功后自动归档
  `--archive-dir docs/archive`，输出目录只留最新一轮
- gate FAIL / 任务失败：不归档（阶段目标未达成，保留现场供排查）

### 2.10 代码组织质量门禁（dogfooding）

```bash
python quality.py           # 组织质量扫描（纯标准库）
python quality.py --strict  # 严格模式（含文件行数预算）
# 注册为 validator 后可在流水线中调用：--validate pyquality
```

- 预算与仓库 Go 门禁对齐：函数 ≤50 行、复杂度 ≤15、文件 ≤1000 行、
  重复函数体检测（测试文件预算 ×2）
- `pi-batch.yaml` 的 `validators.pyquality` 已注册，可在流水线阶段当
  工程门禁使用；重构本工具自身时用它对标验收（当前全树严格模式达标）

### 2.11 项目级 Campaign（自动发现 → Top N → 完整实现）

```bash
# dry-run 不创建目录、日志或状态文件
python pi-batch.py campaign --dry-run

# 限定模块；4 路分析；每模块实施评分最高的 2 个方向
python pi-batch.py campaign --modules "domains/mfa,protocols" \
  --jobs 4 --max-directions 2 --reuse --retry-failed

# 兼容入口，参数原样转交 Python coordinator
bash scripts/full-auto.sh --skip-passed --max-directions 2
```

- 分析输出必须是结构化 JSON：问题、价值/风险降低/成本/置信度、现有
  文件或符号证据、可测试验收标准；旧 Markdown 标题仅作为 Proposed 展示，
  不直接进入实现
- `docs/auto/state.jsonl` 是追加式事实源；`SUMMARY.md` 每次从状态重建，
  不参与恢复判断；状态读取使用有限行缓冲，复用判断缓存每个方向的最新事件，
  单条记录受 Campaign `state.line_max_bytes`（默认 256KiB）约束
- fingerprint 包含模块内容、仓库快照、分析 prompt、方向、pipeline、角色
  模板、runner 配置和 model/provider；只有 fingerprint 相同的 PASS 才复用
- 模块分析在持有一个仓库锁的 Python 进程内并发；实现默认串行。设置
  `--parallel-pipelines N` 时，运行前要求 Git 工作区干净，并为每个方向
  建立独立 worktree、分支、输出目录、决策日志和运行日志
- 并行方向不会自动合并回主分支；状态事件记录 branch/commit/worktree 证据，
  gate 通过后仍需按仓库正常评审与合并流程处理，避免自动冲突合并
- 每个隔离方向流水线受 `implementation.timeout`（默认 14400 秒）总截止约束；
  超时杀死完整进程组并记录 `PIPELINE_FAILED`，不会永久占用并发槽
- `--dry-run` 使用历史 JSONL 耗时的中位数估算；无历史数据时回退到 Campaign
  YAML 的 `estimates` 默认值

配置见 `examples/repository-campaign.yaml`，完整实现模板见
`examples/repository-campaign-pipeline.yaml`。

### 2.12 特性与流程环节映射（整套流程 = 全部特性）

| 流程环节 | 使用的特性 |
|---|---|
| 发现问题 | Campaign 模块扫描/结构化方向评分、滚动分析或 meta 编排 |
| 需求分析 | `from_prompt` 一句话起点 |
| 阶段串联 | 单/多 `from_outputs` + `aggregate`（完整 DAG 证据合并） |
| 交叉对抗 | meta 动态角色：命名/ad-hoc、并发独立会话、证据折叠 |
| 阶段目标确认 | `gate` 裁决门：VERDICT PASS/FAIL，fail closed 阻断 |
| 真实实现 | agent 直接改仓库代码（`full-sdlc-implement.yaml`），`validate: build` 工程门禁 |
| 验收 | 第二个 gate（QA 核对验收标准覆盖） |
| 决策记录 | `decision_log`（每阶段思考点+理由+证据，追加式） |
| 完成即处理 | `git_commit`（每阶段）+ `archive_dir`（全成功后归档） |
| 韧性 | 失败签名拒绝落盘、`--reuse` 断点续跑、`--retries`、7×24 轮循环 |

## 3. 命令速查

```bash
# 一键自检（安装/改规则后验证环境）：quality + 注册表 schema + eval 套件
pi-batch check
# 安装为全局 CLI 命令（像 pi/codex 一样使用，任意目录可用）
pip install -e .          # 或 pipx install . / uv tool install .
pi-batch --help
python -m pbatch ...      # 同一入口（免安装）
python pi-batch.py ...    # 仓库根目录零安装回退

# 一句话 → 动态角色分析（任意项目，无需输入文件）
python pi-batch.py examples/meta-review-pipeline.yaml

# 任务类型判断：执行前判定是否前端任务（前端→自动路由 UI 生成流水线）
python pi-batch.py classify "用 Flutter 实现 ERP 排程列表页"
python pi-batch.py classify tasks.yaml --json
# 需求评估：像资深评审一样先评估需求，再开克制的最小处方
pi-batch.py assess "企业订单管理：新建订单表单，含支付审批流程"
pi-batch.py assess "企业登录页面"   # 一句话需求 → 处方档降为 demo，只给 2 条
pi-batch.py assess --file requirements.md   # 从文件评估需求
pi-batch.py assess "做一个开源上传产品，断点续传，商业化"  # 产品化 L3：推演链+开源/商业要素
# 产品思维层：product-specs/（product-thinking 分级/推演链/克制红线、
# commercial-readiness 低成本预留vs高成本实现、open-source-readiness、
# completion-evidence 完成证据报告——禁止伪造验证结果）
# 审查角色：product_thinker（分级→推演→克制审查，隐含需求标"待确认"）
pi-batch.py assess "写一个订单创建接口，含支付审批，生产，go"  # 后端域自动选用 backend-specs
# 后端架构/设计模式/领域建模/演进规范：backend-specs/（architecture、
# design-patterns 决策表+过度设计红线、domain-modeling、evolution）
# 底层能力：oop-and-di（对象vs函数/DI 生命周期/AOP 边界）、ddd（分级使用）、
# algorithms-data-structures（触发式：搜索/TopK/图/缓存等场景才加载）、
# complexity-and-scale（五类成本/数据规模×100 推理）
# 强制关卡：persistence-modeling（编码前先输出 Persistence Design 报告——
# 四种模型分离/主键/金额/状态/快照/索引/唯一约束/审计历史/迁移）
# 生产就绪：production-readiness（需求正确性/契约/错误模型/可靠性/容量/
# 多租户/安全/可观测性/测试验证/发布恢复）、agent-guardrails（防幻觉/变更控制）
# 门禁：backendquality（单实现接口/Base 类/状态散落/依赖方向/N+1/全排 TopK/
# 浮点金额/SELECT */无租户查询/LIKE 全扫）
# 审查角色：backend_engineer、design_pattern_reviewer、algorithm_reviewer
# 审计流水线：pi-batch.py examples/backend-engineering-review.yaml
# 评估内容：8 维完整性检查（缺失维度给建议）/ 规模信号 S/M/L /
# 处方档 = min(关键词档, 规模档) / 刻意未选用清单（克制原则）/ 双向校验
pi-batch.py assess "..." --json
pi-batch.py assess "..." --prompt   # 编译成可直接执行/喂给 agent 的指令块
pi-batch.py assess "..." --llm-json '{"apply":[],"skip":[]}'
# 规则匹配：算法式筛选适用规范（小 demo 只加载视觉核心，生产级自动加全套）
pi-batch.py rules "生成一个登录页 demo"
pi-batch.py rules "企业订单+支付审批，生产" --json
# 双向校验：压缩需求给 LLM 独立选择 → 与算法对比裁决
pi-batch.py rules "企业支付审批，生产" --summary          # 压缩后的需求文本
pi-batch.py rules --check                                  # 注册表 schema 校验（双域）
pi-batch.py rules "企业支付审批，生产" --llm-prompt p.txt  # 生成 LLM 检查 prompt
pi-batch.py rules "企业支付审批，生产" --llm-json '{"apply":[],"skip":[]}'
# 裁决规则：两边一致→保留；必选/风险强制规则 LLM 跳过→算法否决；
# LLM 增补→采纳并标记 provenance；坏 JSON→fail closed（退出码 2）
# 分级：demo（视觉核心）< standard（+异步/表单/错误）< production（+架构预算）
python pi-batch.py -p "生成一个登录页面 tsx" --classify \
  --log-file logs/ui-gen.log
# 前端实现流水线（真实改项目代码，三门禁）：
# plan（assess+布局/组件树/状态/交互链）→ implement（uiquality+
# uicheck+completion）→ 对抗审查 → VERDICT 裁决
python pi-batch.py examples/frontend-implementation-pipeline.yaml --retries 3
# 后端任务自动路由到 backend-implementation-pipeline
# （plan含强制持久化设计报告 → 实现+backendquality/completion 双门禁 →
#  对抗审查 → VERDICT 裁决）
python pi-batch.py -p "写一个订单创建接口，含支付审批，生产，go" --classify
# UI 流水线：场景细化 → 生成（uispacing/uicolor/uistyle 门禁）→ meta
# 对抗审查（ui_reviewer/visual_reviewer）→ VERDICT 裁决门 → 截图评审
# 规范资产：ui-specs/（8pt token、组件/布局/反例、业务 profile）

# 已有项目前端代码的 UI 规范体检（gofmt -l 风格，只检查不改）
python scripts/check-ui-spec.py --dir src --all --json -o docs/ui/refactor/report.json
pi-batch --validate uicheck ...   # 项目级 UI 规范门禁（--dir 按项目配置）
# 自动重构：不符合 ui-specs 的代码被 agent 修复到 0 违规（门禁反馈循环）
pi-batch examples/ui-refactor-pipeline.yaml --retries 5 --max-rounds 0 \
  --log-file logs/ui-refactor.log

# 前端工程审计：异步/竞态/防抖/上帝文件/复杂度（决策表+缺陷模式+门禁）
python scripts/check-frontend-quality.py --dir src --strict --json
pi-batch examples/frontend-engineering-review.yaml --retries 3 \
  --log-file logs/fe-review.log
# 工程规范资产：ui-specs/engineering/（async-data 决策表、form-table-state、
# error-recovery、architecture-budgets、defect-patterns 缺陷模式库）
# 审查角色：prompts/frontend_engineer.md、async_reviewer.md、
# code_architecture_reviewer.md（meta 编排动态挑选）

# 完整闭环：一句话 → 需求 → 设计 → 对抗审查 → 裁决门 → 实现+工程门禁 → 验收门
#          → git 提交 → 归档 → 决策日志
python pi-batch.py examples/quickstart-full-sdlc.yaml \
  --log-file logs/full-sdlc.log

# 全流程真实实现版：同一闭环，但 implement 阶段 agent 直接修改仓库代码
# （go build/vet 门禁 + QA 验收门把关），建议先 --dry-run 预览再跑
python pi-batch.py examples/full-sdlc-implement.yaml \
  --log-file logs/full-impl.log

# 一体式入口：滚动分析发现方向 → 选择方向 → 自动跑完整 SDLC（含真实实现）
#   full-flow.sh [轮数] [间隔秒] [方向]
# 交互模式：不给方向参数，展示候选后输入；非交互：直接传方向
bash scripts/full-flow.sh 3 300            # 3 轮分析 + 交互选方向
bash scripts/full-flow.sh 3 300 "设备信任"  # 3 轮分析 + 直接实现该方向

# 全自动：按项目架构模块逐个分析 → 自动提取每个方向 → 每个方向跑完整实现
#   full-auto.sh [--modules m1,m2] [--max-directions N] [--jobs N]
#                [--skip-passed] [--parallel-pipelines N] [--dry-run]
# 默认扫描 domains/interfaces/infrastructure/platform/protocols/shared 全部
# 模块；gate FAIL 等失败记录到 SUMMARY 并继续下一项，无人值守
bash scripts/full-auto.sh --dry-run                     # 先看计划
bash scripts/full-auto.sh --max-directions 3 --reuse    # 全模块自动跑/指纹续跑
bash scripts/full-auto.sh --modules "domains/mfa"       # 限定模块

# MFA 子系统分析（一句话起点 + 动态角色审查）
python pi-batch.py examples/quickstart-snaplink-analysis.yaml

# SDLC 角色流水线（需求→架构→安全→质量，6 角色）
python pi-batch.py examples/sdlc-mini-pipeline.yaml \
  --log-file logs/sdlc.log

# 功能需求提案（同会话两任务）
python pi-batch.py examples/snaplink-proposals.yaml \
  --mode serial --session-mode shared --session-name logout-proposals

# 滚动分析（替代 for i in {1..200} 裸循环：会话延续 + 决策日志 + 归档）
python pi-batch.py -p "基于当前代码库分析扩展方向" \
  --output "docs/requirements/runs/run-$(date +%Y%m%d-%H%M%S).md" \
  --session-mode shared --session-name ext-rolling \
  --min-interval 60 --retries 3 --log-file logs/ext.log \
  --decision-log docs/DECISIONS.md --git-commit --archive-dir docs/archive

# 7×24 无人值守
python pi-batch.py tasks.yaml --mode serial --reuse --retries 3 \
  --max-rounds 0 --round-delay 300 --min-interval 5 --log-file logs/run.log

# 评审闭环（十阶段，共享会话，可续跑）
python ai/run-review.py --all --context ctx.yaml \
  --session-mode shared --resume --validate quick

# 代码组织质量检查（重构后用）
python quality.py --strict 
```

## 4. 测试与门禁

```bash
make ci                     # 387 个回归测试 + 25 eval 用例 + 34 harness 自测
python -m pytest checks/    # 仓库工程门禁自测（147+ 个）
python quality.py --strict .   # 组织质量门禁（当前全绿）
```

修改本工具后：跑 `make ci` + `quality.py --strict .` + 与改动相称的仓库
门禁；涉及 Go 代码时按仓库 `AGENTS.md` 强制验证流程执行。

## 5. 安全与信任边界

- **不可信输入**：agent 输出、编排者 JSON——失败签名拒绝落盘、角色模板
  查找防穿越、角色名 sanitize、gate 缺失裁决 fail closed
- **Campaign 输入**：模块/输出/pipeline 路径限制在仓库内；候选方向必须有
  真实仓库证据和验收标准；并行实现必须使用独立 Git worktree
- **失败零产物**：拒绝/验证失败的结果不留任何文件（临时文件删除）；
  阶段未完成（gate FAIL）不归档，保留现场
- **AI 产出是提案**：按证据标准（Verified/Partial/Missing/Proposed）核对后
  提升到 `docs/feature-matrix.md`、`docs/deferred-backlog.md` 或正式
  feature spec，不直接作为需求/测试结果/发布批准
- **仓库硬门禁不变**：本工具集不豁免 `make ci`、`cli.py` 检查、AGENTS.md
  的安全/线协议不变量

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| 本文件 | 能力总览与使用指南 |
| `docs/RUNNING_247.md` | 7×24 运营、验证门禁、会话、meta 编排细节 |
| `docs/AUTOMATION_WORKFLOW_SUMMARY.md` | runner 状态与机制速览 |
| `docs/ROLES_SUMMARY.md` | 角色 prompt 边界说明 |
| `ai/README.md` | 十阶段评审框架用法 |
| `examples/README.md` / `SDLC_CASE.md` | 提案工作流与角色流水线案例 |
| 仓库根 `AGENTS.md` | 项目执行契约（本工具集必须遵守的硬门禁） |
