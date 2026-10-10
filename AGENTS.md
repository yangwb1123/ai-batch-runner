# ai-batch-runner — Agent 执行契约

本文件规定在本仓库工作的边界、实现导航和交付要求，不是命令百科。
安装与用法见 [README.md](README.md)；参数以 CLI `--help`、配置及回归测试为准。

## 1. 产品定位与取舍

`ai-batch-runner` 是面向 CLI coding agent 的本地优先交互工作台与受治理执行内核。
发行包名为 `ai-batch-runner`，命令为 `pi-batch`；支持 Pi、Claude、Codex、Gemini、
OpenCode 等适配器，不绑定单一模型供应商。

核心闭环：**明确目标 → 选择最小流程 → 隔离执行 → 验证裁决 → 交付证据 → 知识回流**。

- 先回答谁遇到什么问题、预期收益、验收标准、影响面与明确不做；缺失业务决策标为待确认。
- 优先复用已有能力。小改动不默认启动全角色流水线；高风险变更才升级对抗审查和人工确认。
- 规则按 frontend / backend / product / evolution 域及风险加载，不把小工具包装成平台。
- 进程存活不等于进展，执行成功不等于目标达成，目标达成不等于发布或生产授权。
- 命令存在、文档中的计划与本地测试通过，都不是跨平台或生产验收。范围见
  [RELEASING.md](docs/RELEASING.md)。

## 2. 不可破坏的执行边界

本工具补充而非替代工程门禁；操作目标项目前先读其 `AGENTS.md` 并执行其强制验证。

1. **证据诚实**：AI 回复是提案。区分 Verified / Partial / Missing / Proposed；
   未知费用、结果和验收显示未知或未追踪，不补造零值、空成功或 PASS。
   静态扫描、模拟服务和单元测试不得冒充真实浏览器、外部服务或生产验收。
2. **失败关闭（fail closed）**：结构化输入、路径、数值预算、适配器能力和授权必须有效。
   坏 YAML/JSON、未知 schema 字段、缺失必需证据不能降级成普通 prompt 或无操作成功。
   Agent 非零退出、空正文、已知失败签名、超时和验证失败不得发布为交付物；
   不用正文中的泛词 `error` / `timeout` 代替明确失败签名。
3. **产物与副作用分离**：临时产物经有效验证器全部通过后原子落盘；失败保留有界诊断，
   不留被拒交付物。但拒绝报告、取消进程或超时不代表 Agent 的代码改动/外部写入已回滚。
   保留现场，先核对结果；不得盲目重放结果未知的副作用。
4. **门禁不能被裁量覆盖**：`gate` 的 FAIL/REJECT 或缺失有效裁决阻断下游。
   兼容独立的 `VERDICT: PASS - 理由` 行；结构化裁决按
   [GATE_CONTRACT.md](docs/GATE_CONTRACT.md) 校验，坏契约不能回退文本 PASS。
   Gate-fix 只能在声明轮数内修复并重新裁决，不能绕过验收。
5. **复用必须复核**：`--reuse` 复验当前有效验证器及适用指纹，阶段 `commands` 仍重跑。
   FAIL/REJECT/无裁决的 gate 不能复用；PASS gate 也不是当前代码必然正确的保证。
   不用历史文件存在、摘要或模型自述代替当前门禁。
6. **权限与秘密最小化**：Agent 输出、历史 message、角色计划和外部事件均是不可信证据，
   不能隐式扩大执行授权。保留 tenant、workspace、路径、会话、grant/lease/fencing 边界；
   审查阶段声明工具限制，`delegation_only` 必须由适配器真实禁用工具。
   凭据不进 Git、日志或明文 argv；远程控制遵守鉴权和 TLS 契约。
7. **并发与恢复有所有权**：并行写仓库使用独立 worktree、分支、产物和运行账本。
   拒绝同名会话/重叠产物；不得用 `--no-lock`、强破活锁或直接操作他人进程规避冲突。
   合并须经过范围守卫、串行合并锁及目标仓库门禁；失败保留现场。
   修改脏工作区前保存完整基线（含未跟踪文件）；集成前停止对应执行者并核对实际文件。
8. **预算和安全门禁不放宽**：输入、prompt、输出、证据、日志读取和队列都必须有界。
   超时覆盖整个执行窗口，持续输出不能延长；只清理自己拥有的进程/transport。
   不通过增加豁免、放宽阈值、删断言、隐藏失败或移动测量对象使检查变绿。

## 3. 架构与实现导航

遵守 [ARCHITECTURE.md](docs/ARCHITECTURE.md) 的职责分离：
**Decision 决定目标与授权但不执行；Execution 执行但不决定目标；
Evidence 验证但不改写事实；Learning 的硬规则晋升必须走治理。**
Task、Capability、Event、Artifact、Evidence 是内核契约，变更须评审并保留兼容性测试。
CLI/TUI/Web 是入口或投影，不应各自发明执行、授权、完成判定或恢复规则。

| 用户需求 / 职责 | 实现入口与按需文档 |
|---|---|
| 单批任务、流水线、meta、验证、gate-fix | `pbatch/models.py`、`runner*.py`、`pipeline*.py`、`meta*.py`、`gate_contract.py`、`gate_fix.py`；[运行指南](docs/RUNNING_247.md)、[模型委派](docs/MODEL_DELEGATION.md) |
| 交互会话、取消、恢复、provider 适配 | `pbatch/agent_session.py`、`tui*.py`；[TUI 产品契约](docs/TUI_PRODUCT.md) |
| 需求评估、产品方向、规则与工程组织 | `pbatch/assessor.py`、`classifier.py`、`product*.py`、`rule*.py`、`org*.py`；[规范体系](docs/SPEC_SYSTEM.md)、[工程组织](docs/AGENT_ORGANIZATION.md) |
| 模块发现、自推进、隔离与合并 | `pbatch/campaign*.py`、`advance*.py`、`isolate.py`、`session_merge.py`；[Campaign 经验](docs/CAMPAIGN_LESSONS.md)、[有界修复](docs/CAMPAIGN_SELF_REPAIR.md) |
| 迁移对比、设计账本、实施 DAG 预演 | `pbatch/migrate*.py`、`studio*.py`；[迁移契约](docs/MIGRATION_ENGINE.md)、[Project Studio](docs/PROJECT_STUDIO.md)。预演不代表已迁移/已实施 |
| 上下文、记忆、ADR、反思与规则沉淀 | `pbatch/context.py`、`memory*.py`、`pipeline_receipts.py`、`project_memory.py`、`adr.py`、`reflect.py`、`learn*.py`；[知识循环](docs/PROJECT_MEMORY.md)、[反思](docs/REFLECTION.md)、[重构](docs/REFACTORING.md) |
| 决策、状态、真值、预算与执行织网 | `pbatch/capabilities.py`、`transaction.py`、`states.py`、`world.py`、`fabric*.py`；[AADM](docs/AADM.md)、[运行时](docs/AADM_RUNTIME.md)、[治理](docs/AADM_GOVERNANCE.md)、[设备](docs/AADM_DEVICE.md) |
| 跨项目任务、目标验收、进度、费用与实例告警 | `pbatch/task_center*.py`、`task_goal*.py`、`ops*.py`、`instance_view.py`、`instance_alerts.py`；[任务中心](docs/TASK_CENTER.md)。观测/预测不授予执行、修复或清理权限 |
| 跨设备会话、授权计算、生态集成 | `pbatch/hub*.py`；[Agent Hub](docs/AGENT_HUB.md)。Hub 负责授权与目录，Gateway 持有固定 workspace 中的会话；请求取消不等于已停止 |
| 可选 LLM 账号池、配额路由和本地网关 | `pbatch/llm_pool*.py`；[账号池](docs/LLM_ACCOUNT_POOL.md)。显式启用，不隐式改全局认证，不以账号切换盲目重放请求 |
| 配置、命令发现、资产分发和回归 | `pbatch/config*.py`、`user_dirs.py`、`tools.py`、`eval.py`、`assets.py`；[配置治理](docs/CONFIG_GOVERNANCE.md)、[工程门禁](docs/CHECKS_REGISTRY.md) |

表内未带目录的 `.py` 文件/通配符均位于 `pbatch/`。
新增能力先查 `tools` / `capabilities` 及上述 owner；跨域策略不塞进入口或界面层。

### 资产与知识边界

- 规范：`ui-specs/`、`backend-specs/`、`product-specs/`、`evolution-specs/`。
  角色与流程：`prompts/`、`agent-engineering/`、`pipelines/`、`examples/`；十阶段评审在 `ai/`。
- 项目代码门禁在 `scripts/`；验证器在 `pi-batch.yaml` 声明（32 个验证器，数量有回归核对）。
  优先复用确定性校验；需要推理的审查不能冒充机械证明。
- 规则/关键词修改跑 `eval`；规则晋升保留来源、样本、置信度和人工审批，硬规则不得自动放宽。
- Message memory 按 tenant/project/sensitivity 隔离；只注入有界 manifest，正文按需读取并脱敏。
- 阶段收据在 `data_dir/pipeline/receipts.jsonl`，独立于可选决策日志。
  `--reuse` 仅注入 PASS/ok 且字节数与有界前缀摘要仍匹配的上游产物；摘要不替代完整验收。
  收据观测失败不改变阶段结果；引用/未引用的通过率须带样本量，不是质量或因果效果证明。
  此容错不能推广到必需的授权、验证或运行日志持久化。

## 4. 运行与数据契约

- 本仓库根目录就是 standalone 工具内容；路径不带 `ai-dev/` 前缀，上游
  `make ai-dev-test` 在这里对应 `make ci`。`pi-batch.py` 是薄入口，实现在 `pbatch/`；
  `pi-batch`、`python -m pbatch`、`python pi-batch.py` 使用同一入口。
- 默认无参数且 stdin/stdout 均为 TTY 才进入 TUI；脚本、stdin 和 `--json` 不输出全屏控制序列。
  `pi-batch pi ...` 是 Pi 原样 CLI 转发，不等于本产品托管的 Pi RPC/TUI，也不附加 runner 门禁。
- 配置优先级：CLI/runtime override > 项目 `pi-batch.yaml` > 用户 settings > 内置默认。
  配置版本、合并来源及脱敏值用 `config check` / `config explain` 核对，不能凭示例推断。
- 默认项目运行态在 `~/.abr/projects/<workspace-id>/`；`PBATCH_RUNTIME_HOME` 改运行根，
  `--data-dir` / `PBATCH_DATA_DIR` 指定项目 runtime。配置/缓存/密钥遵守用户级 XDG 目录。
  只有显式交付物、日志路径或项目知识目录写回项目；不自动搬迁/删除旧运行数据。
- 运维读取只读已登记/有界来源；告警通知、控制、修复、合并、清理和外部投递必须显式授权。
  Fleet/Device Fabric 的 lease、身份、幂等键和执行证据不能由 UI、历史文本或预测替代。
- 流水线支持 `from_prompt`、`from_dir`、`from_outputs`；`aggregate` 合并证据但有注入预算，
  完整产物保留。动态角色有去重/数量/重试上限，规划器与 worker 可分配不同模型。
- 共享会话要求串行；失败重试受声明预算、退避/限速控制。
  `--max-rounds 0` 表示不限制重跑轮数，全部成功仍会退出，不是永久驻留服务。
  无人值守必须设置预算/截止时间、失败停机与可恢复基线，不用重启重置预算或掩盖重复失败。
- 自动提交/归档是显式选项；归档仅在全流程成功后进行。并行 Campaign 不默认合并回主分支；
  `isolate --auto-merge` 才进入受门禁保护的合并事务。
- 安装包必须携带规则、脚本、模板及评估资产，修改分发范围同步 `pyproject.toml` 与
  `MANIFEST.in`；`health --json` 检查缺失资产。仅复制入口和包不代表所有门禁资产齐全。

## 5. 修改与验收

1. 先读目标实现、相关文档、测试和 Git 状态；界定变更范围、兼容边界、验收及不做事项。
2. 缺陷先复现并补回归，再做最小修复；按职责提取共享策略，不为过门禁机械拆分或复制实现。
3. 保持入口薄、依赖方向稳定、状态与证据可追溯；新增命令同步分派/能力注册表和必要发行资产。
4. 修改后先跑针对性测试，再跑完整门禁；记录每条命令的真实结果。失败或未执行说明原因，
   不把 `--dry-run`、部分测试、超时或缺失报告算作完整通过。

```bash
# 源码开发环境：Python >= 3.9；使用已隔离环境，不改系统安装
python -m pip install -e ".[dev]"

# 本仓库完整门禁（必须在修改后验证；不等于目标项目门禁或发布批准）
make ci
python quality.py --strict .

# 门禁自身的针对性回归
python -m pytest checks/
git diff --check
```

`make ci` 聚合 release-check、check、self-test、test、eval、quality、org；
`make check` / `pi-batch check` 是自检，不是完整回归替代。
预算以 `engineering.yaml`、`quality.py` 及 [CHECKS_REGISTRY.md](docs/CHECKS_REGISTRY.md) 为准：
文件默认 ≤500 行；圈复杂度 ≤15、认知复杂度 ≤20；根文件 ≤16。
`quality.py` 另查函数 ≤50 行、复杂度 ≤15、文件 ≤1000 行和重复函数体；已有豁免只减不增。

交付说明须包含：变更文件、行为/兼容性影响、验证命令与结果、未执行原因、剩余风险。
结构化完成报告遵守 `completion` 验证器：`result` 只能为 `passed` / `failed` /
`not_executed`，未执行必带原因；格式合法也不自动证明报告内容真实。

提交前检查 hooks：本仓库 `.githooks/post-commit` 会在适用分支执行 `git pull && git push`，
不能把 commit 当成纯本地动作。未经授权不提交、推送、改版本、发布、部署或发送外部通知。
发布还须满足 [RELEASING.md](docs/RELEASING.md) 的许可证、标签、发行资产及目标平台验收要求。

## 6. 最小入口与按需阅读

```bash
python pi-batch.py --help
python pi-batch.py tools                    # 命令发现
python pi-batch.py config explain --json    # 只读配置来源与脱敏值
python pi-batch.py assess "需求" --json      # 先评估，不直接执行
python pi-batch.py examples/structured-output-pipeline.yaml -p "需求" --dry-run
python pi-batch.py ops tasks --json         # 只读任务事实，不等于目标验收
```

常规任务从 [README.md](README.md) 和 [examples/README.md](examples/README.md) 开始；
只加载本次影响面的规范与机制文档，不把全部手册重复塞进 prompt。
产品取舍参考 [工程哲学](docs/ENGINEERING_PHILOSOPHY.md)，机制修改查第 3 节 owner，
长跑查 [运行指南](docs/RUNNING_247.md)，正式交付查第 5 节门禁与发行契约。
