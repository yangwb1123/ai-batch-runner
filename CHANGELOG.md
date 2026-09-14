# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- `fleet` Agent Hub：持久实例/会话目录、prompt 幂等路由、事件游标与取消；
  出站 Gateway 拥有本地 AgentSession，支持 CLI/TUI 和 Flutter Console 接入。
  Snaplink JWT 按 tenant、scope 和实例授权过滤，Gateway 换代拒绝旧进程写入。
- `fleet compute`：授权设备目录、CPU/内存调度、持久幂等计算任务与运行中取消；
  CLI/TUI/Console 可操作，Gateway 可为当前轮次提供受限调度工具。
  可选 Aero Vault 固定版本证据归档；工作区传输仍待后续实现。
- NVIDIA CUDA 物理 GPU 探测、逐卡显存选机、持久 UUID 分配、Runner 执行前复核
  和证据绑定；CLI/TUI/Console 支持 GPU 输入和展示，旧 CPU 请求保持幂等兼容。
- Aero ID 成员投影门禁、Aero IM 终态通知和 Audit Governance 入账回执；
  持久 outbox 支持有界重试、断点恢复、固定接收人和租户/实例隔离的 `fleet integrations` 运维入口。
- Hub 本地开发配置初始化、生产配置样例和 OpenAPI 契约，见 `docs/AGENT_HUB.md`。
- GitHub Release 单文件二进制构建与跨平台冒烟流程。
- `ops serve --control` 手机会话列表、服务端会话创建、有界指令提交、状态轮询和本地停止。

## [0.3.0] - 2026-08-20（交互式 TUI）

### 新增

- 无参数、真实终端下默认启动的 Textual 工作台；显式入口为 `pi-batch tui`。
- 对话式 Markdown 界面、流式 stdout/stderr、状态侧栏、命令面板、稳定会话，
  以及 `/help`、`/status`、`/new`、`/clear`、`/model`、`/provider`、
  `/agent`、`/memory`、`/doctor`、`/quit`。
- TUI `/sessions` 与 `/resume <session-id>`：在当前 cwd/memory scope 内有界发现
  journal、重建可信投影，并仅在 runtime 能力和 provider handle 匹配时续跑；
  未决/损坏/不兼容历史只读，显式续跑失败不会静默开启新会话。
- `/sessions` 已升级为不污染 transcript 的键盘 picker：Enter 精确恢复，`f` 按
  `session_fork` capability 准备 provider fork，`a` 以锁内可逆本地标记
  archive/restore；archive 不删除事件，也不伪装 provider archive。
- `/sessions` 新增 projection-commit 后写入、精确 journal byte size 绑定的有界
  `summary.json`，显示 runtime/branch/last turn/完成消息；缺失、陈旧或损坏缓存只降级
  展示。App Server `thread/list` 现在使用 typed page、固定 cwd/source kind、严格字段
  校验并保留 `nextCursor`，在独立 worker 中按 provider handle 精确 advisory 合并；
  remote-only thread 和列表状态都不能替代动作时 journal 恢复与 `thread/read`。
- TUI `/plan` 与 `/changes` 独立 Inspector：Plan 与 Timeline 共用结构化投影并实时
  刷新；Changes 支持 all/unstaged/staged/untracked/last-turn、base/commit ref、
  文件选择与 hunk 导航。两者均保持有界只读，不向 conversation transcript 注入检查内容。
- TUI `/agents [agent]`（`/runs` 兼容别名）升级为独立 Agents Inspector：展示当前
  coordinator operation 与 scoped shared ledger 的最近终态，支持 agent/status 过滤、
  手动刷新、产物引用和损坏/截断诊断；不把运行摘要写入 conversation transcript，
  也不在缺少 runtime capability 时伪造 actor stop/steer。
- TUI `/completion` 独立 Completion Inspector：从 canonical typed events 与权威
  workspace snapshot 汇总当前 turn 的终态、命令、变化、未完成 Plan、validator/gate、
  tool、artifact、风险和 usage；活动 turn 中原位刷新，缺少验证事件时不声称通过，
  Assistant 正文不能伪造 completion facts。
- TUI `/artifacts [N]`（`/artifact` 别名）独立 Artifact Inspector：只消费当前 turn
  的 typed `artifact.created`，支持 Completion 内按 `a` 跳转、列表选择与实时刷新；
  workspace 内 UTF-8 普通文件通过逐级 no-follow 打开并有界预览，远程 URI、越界路径、
  符号链接、目录和二进制内容不读取，也不启动外部 viewer 或污染 transcript。
- Codex Structured Exec/App Server 的命令大输出自动提升为 data-scoped ArtifactRef：
  event/Timeline 继续只保留短预览，aggregate 或 output delta 进入私有 data_dir 存储；
  默认单文件 2 MiB、每 workspace/session 64 MiB/256 文件滚动保留，身份只入哈希路径，
  目录/文件权限为 0700/0600。存储失败发 typed warning，不篡改 command/turn 终态。
- Codex App Server adapter 对齐当前官方协议：默认直启 stdio server、完成
  `initialize`→`initialized` 握手；显式 daemon socket 才走 proxy。provider terminal
  status 现在精确区分 completed/failed/interrupted，未知状态失败关闭，不再把失败或
  中断误报为成功。
- 新增 canonical `pi-batch pi [PI_ARGS...]` 原样透传，以及长期 Pi JSONL RPC
  runtime / Python `AgentSession` facade；`PBATCH_PI_BIN` 在透传、TUI 与 Python
  默认选择中保持同一 executable-token 语义。
- Pi typed controls 新增 `/session`、后台 `/compact`、动态 `/model` 与
  `/thinking`。adapter 对同一 executable 做有界 stable SemVer 探测，仅在
  Pi 0.84.3+ 自动开放 session-only 设置；旧版或未知版本零写入、fail closed。
  模型/思考选择在 mutation 前校验 provider 当前快照，之后以 `get_state` 权威回读。
- TUI slash 命令面收敛为 immutable registry，同源生成 help、dispatch、
  command palette 与 busy allowlist；composer 支持零 RPC 的本地命令名
  Tab/Right/Enter 补全，palette 补齐 session/model/thinking 入口。`/model QUERY`
  与 `/thinking QUERY` 参数补全按 adapter/session/kind context 最多读取一次只读
  available snapshot，再在本地 fuzzy；键入参数自动打开，粘贴上下文可用 Tab 显式
  打开，picker 的 Tab/Enter 都只填入 canonical 命令文本，随后 Enter 才提交并经 fresh
  catalog/scope/version 门禁。旧/global scope
  仍可只读补全但不能调用 setter。RPC available catalog 不冒充 `scopedModels`；
  completion-only 排序复刻 Pi token fuzzy 与 model 搜索字段，thinking 仅搜索 level；
  `/scoped-models` 与 cycle 在缺少权威 scope 协议时保持关闭。
- App Server session resume 新增 `thread/read` 预对账：读取不加载、不订阅 provider
  thread，只有 `notLoaded`/`idle` 状态才允许 `thread/resume`；active、systemError、
  畸形或不可用状态保留本地可信历史但禁止执行。`turn/steer` 也会校验响应 turnId，
  未精确确认时不发布已接受事件。
- 活动 turn 期间普通输入进入有界、可查看/编辑/删除的进程内 follow-up queue；成功
  终态自动续发，失败或取消时保留并暂停。`/steer` 仅在活动 runtime 明确声明
  `turn_steer` 且存在匹配 turn handle 时发送，不把本地队列冒充 provider 能力。
- `pi-batch product`、`assess --product-brief` 与 TUI `/product`：按 L0-L3
  生成有界、确定性的产品方向提案，覆盖用户故事、旅程、异常状态机、
  Epic→Feature→Story→Task、业务规则和待确认问题；L0 不生成产品结构。
- `Ctrl+P/N/L/C/Q` 键盘工作流；内部 turn 防重入，取消会终止 agent 进程组。
- TUI turn 复用既有 prompt/session/memory/timeout/失败签名机制，并遵守项目
  单实例锁；缺少 agent 时首屏仍可打开并展示诊断。

### 兼容性

- stdin/stdout 任一被重定向时不进入 TUI；pipe prompt、`-p`、pipeline、所有
  子命令和 JSON 输出保持原有无头契约。
- wheel 将 `textual>=8.2,<9` 声明为核心依赖，并包含 TUI/backend 模块。

### 发布

- PyPI distribution 固定为 `ai-batch-runner`，安装后的产品命令固定为
  `pi-batch`。
- `vX.Y.Z` 标签触发独立发布流水线：版本与 changelog 校验、完整 `make ci`、
  wheel/sdist 构建、Twine 检查、隔离安装冒烟后，通过 OIDC Trusted Publishing
  上传 PyPI；`pypi` Environment 保留人工批准点。
- 当前 `LICENSE` 中的上游授权未决说明会阻断标签发布；确认版权/许可证证据并
  更新归属后才允许首次公开上传。

## [0.2.0] - 2026-08-08（产品化里程碑）

### 新增（产品 CLI 规整）
- **用户级工作目录（XDG 兼容）**：`~/.config/pbatch/`（配置）、
  `~/.cache/pbatch/`（缓存）、`~/.local/share/pbatch/`（数据：设备密钥/全局日志）；
  支持 `PBATCH_HOME=~/.pbatch` 统一收拢模式。
- **配置分层**：CLI 参数 > 项目 `pi-batch.yaml` > `~/.config/pbatch/settings.yaml`
  > 内置默认（节内键级合并）。
- `pi-batch init`：创建用户级目录骨架 + settings 模板。
- `pi-batch version [--json]`：版本（pyproject 单事实源）+ 环境信息。
- `pi-batch clean [--all]`：清空用户级缓存（--all 连配置/密钥，需确认）。
- `pi-batch completion bash|zsh`：shell 补全（注册表驱动，新命令自动同步）。
- `pi-batch doctor [--json]`：环境诊断（安装/目录/配置分层/agent/密钥/锁）。
- `pi-batch devices keymigrate`：旧项目内设备密钥迁移到用户级（跨项目身份复用）。

### 变更
- 设备密钥默认目录：项目内 `.pi-batch/devices/keys/` → 用户级
  （显式 `--state-dir` 仍用项目内，测试隔离不受影响）。
- 项目相关状态（会话/记忆索引/被拒产物）**保留项目内 `.pi-batch/`**——
  团队共享；用户级目录只放跨项目数据（边界文档化）。

### 新增（2026-08 campaign 实战内化）
- `org gate --log`：`{log}` 占位符注入；`_execute_check` 残留未知占位符
  **fail closed**（不再静默假通过）。
- campaign 模块发现排除打包产物（egg-info/__pycache__/dist/build/
  node_modules/.venv）。
- `repository-campaign-pipeline.yaml`：`meta_role_retries: 2` +
  `meta_max_failed_roles: 1`（provider 抖动韧性）。
- `check-knowledge-freshness`：设备清单配置假阳性修复。
- 新增命令：`probe`（Dry-run 标准输出，区间+置信度）、`replay`
  （决策重放：胶囊版本对比 + eval 回归）、`test-report`（测试分类统计）。
- `devices clock-skew`（时钟纪律）、`serve --device-quota`（per-device 并发上限）。
- `pi-batch check` 聚合 8 门禁（quality/registry/org/capabilities/graph/
  tools/knowledge/eval）。
- pytest markers（unit/contract/property/scenario/chaos）+ 代表性打标。

### 修复
- merge origin/main（使用反馈 P1-P7）：P2 容忍语义移植 cli_batch、
  P3 `_record_fatal_exit` 移植拆分版 cli.py、P1 from_dir aggregate。
- cli.py 拆分版恢复（392 行 re-export）+ 行数预算归位。
- `project verify` 索引不一致修复（ADR 库重索引）。

## [0.1.0] - 2026-08-06（能力里程碑）

- 74 命令（49 顶层 + 25 嵌套）、1086+ 回归测试、42 eval 用例。
- 理论层（P1-P10）/ 运行时层 / 治理层（truth/causal/temporal/…）/
  设备织网（D0-D6：SSH 受控执行、Runner、控制平面、调度/迁移/检查点/投机执行）。
- 工程知识循环（impact/adr/project memory）、Reflection 引擎、
  17 节点 AI 软件工程组织、41 角色模板。
- 规则系统（双域注册表 + 双向校验 + learn 生命周期 + eval 回归）。
