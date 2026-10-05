# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- `campaign-eval`：多维 campaign 评估。完成不是结论——一次窗口必须按正确性、
  产出、效率、可靠性、卫生、集成风险、可观测性、可持续性八个维度打分，每个指标
  都有显式阈值，越界即 breach，可 `--fail-on-breach` 作为窗口收尾闸门。
  入口是独立模块 CLI `python3 -m pbatch.campaign_evaluation_report`（尚未挂成
  `pi-batch` 子命令）。实现为 `pbatch/campaign_evaluation.py`（原语、计数、
  派生比率与阈值）、`campaign_evaluation_dimensions.py`（八个维度与 flag）、
  `campaign_evaluation_facts.py`（state/日志/git/工作树事实采集）、
  `campaign_evaluation_report.py`（Markdown 渲染与 CLI）。
  两处语义修正：名额配额丢弃（`selection quota`）不算质量否决，
  `agent binary not found` 这类基础设施失败不算模型失败。
  无遥测的指标记为 `unavailable` 而非 0。
- `ops tasks` / `ops sources`：全局任务中心的只读切片。跨所有已登记工作空间
  投影目标、状态、墙钟、token、产物与进程活动——只读既有事实，不含 LLM 推测、
  不含 ETA 与归属推断、不授予执行或清理权限。未追踪的量记「未追踪(unavailable)」
  而非 0；进程存在只记为 activity，不等同于任务有进展。`docs/TASK_CENTER.md`
  记录了分批实现计划与登记边界。

- 隔离实现的交付物现在会被合并门禁看到。方向流水线在子进程里运行，父进程拿不到
  阶段结果，原先证据只有 worktree、pipeline.yaml 和运行日志；而子进程成功后会把
  交付物移入带时间戳的 `archive/`，于是自动合并要求的 `VERDICT: PASS` 工件永远
  不可达——每个通过的隔离方向都被判为 `no bounded VERDICT: PASS artifact`，
  没有任何一次真正合并（iris-grid-core-24h-v4 现场：9 个方向全部被拦）。
  现在按 `artifacts/**` 与 `archive/*/` 有界、确定性地收集交付物证据。
- 自动合并失败不再把仓库留在半合并状态。`git merge --squash` 成功但随后的 commit
  被拒绝时，已暂存的合并结果会留在仓库里，之后每个方向都被
  `repository root is dirty` 拒绝，整个 campaign 直到人工介入才恢复。现在失败
  时执行 `merge --abort` + `reset --hard HEAD`（合并前已校验仓库干净）。
- 自动合并提交跳过仓库 hook（`--no-verify`）。方向自身的提交已在它的工作树里跑过
  hook，合并门禁又会重新校验 scope、framework-free、禁用路径、typecheck/build 和
  测试；再次对若干独立方向的 squash 结果跑 lint-staged 只会重写已合并文件的格式，
  并以与本方向无关的原因阻断 campaign。
- Linux x86_64 独立二进制发布入口，复用平台验证并绑定明确的标签提交，
  不依赖 PyPI 发布；同版本资产禁止覆盖，授权未决仍阻断正式发布。
- 二进制构建清单绑定源码输入、构建环境、可执行文件摘要与完整 LICENSE；
  构建期间源码变化会失败，重建先使旧成功清单失效，支持发布前重新核验。
- 有界真实 provider 二进制验收：实际回答、会话连续性、生成中取消和新会话恢复，
  显式调用模型并保留对应二进制的证据，失败返回非零退出码。
- TUI `--startup-timeout` 可独立配置 Codex App Server 初始化期限；默认启动预算
  15 秒，普通 RPC 仍为 5 秒，任务期限继续由 `--timeout` 控制。
- 独立跨平台二进制验证工作流支持 PR 与手动运行，正式发布复用同一矩阵；
  冒烟 `--report` 记录二进制摘要、通过/跳过/失败与耗时，失败保留诊断证据。
- `pool`：用户级 OpenCode Go 账号池、事务化共享租约、多窗口冷却和明确额度拒绝后的
  请求级切换；带令牌的本地流式网关、私有凭据管理与只读 MCP 状态/事件接口。
- `pool usage`：Go 三窗口实时额度查询、跨进程缓存/排他刷新和可选的请求前额度预检；
  缺失或过期状态不放行，MCP `pool_usage` 只读脱敏缓存，v1 状态事务升级保留原控制记录。
- `pool init` / `pool account`：配置初始化、账号列举/增改/移除，支持零写入预览、
  revision 校验、跨进程锁、原始配置私有备份和原子发布；保留凭据/运行状态，变更需重启网关。
- `pool doctor`：逐账号脱敏的本地接入诊断、只读配置/凭据检查及可选的有界网关探测；
  不调用模型、不查询供应商、不修改运行状态，显式区分本地检查与未执行的外部验收。
- `pool doctor --gateway`：在原有单次本地探测内核验启动时的完整配置和启用凭据快照，
  识别模型 ID 未变时的账号/协议变更与密钥轮换；旧网关缺少校验信息时明确失败，
  不展示快照校验值或密钥摘要，不自动热更新配置、重启网关或清除冷却状态。
- `pool run`：保留已有内联配置，为一次性客户端进程接入账号池，支持主/轻量模型、
  脱敏预览和冲突拒绝；POSIX exec 原样传递参数、标准输入输出、退出码和信号，
  不读取密钥、不改全局环境、不自动启动网关；共享的令牌文件引用校验防止路径展开歧义。
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
- Linux 二进制同进程 TUI 故障恢复验收脚本，记录实际运行时间、进程/资源和 journal
  证据；支持固定轮数或持续时间，本地模拟 provider 不调用模型。
- `ops serve --control` 手机会话列表、服务端会话创建、有界指令提交、状态轮询和本地停止。

### 修复

- 流水线启动预检按阶段、任务和 gate-fix 实际使用的 agent 检查依赖；各阶段已指定
  可用程序时不再要求未使用的默认 agent，缺失的必需程序仍在日志、锁和执行前拒绝。
- `gofmt` 门禁保留工具退出码，缺少工具或解析失败时拒绝产物；只有工具成功且
  没有待格式化文件时才通过，避免空 stdout 被误判为格式正确。
- TUI 的 Codex App Server 模式使用 `--agent-bin` 指定的可执行文件，支持不在
  PATH 中或路径带空格的安装位置；未指定时保持默认 Codex 启动方式。
- 配置读取拒绝 FIFO 等特殊文件及打开时替换竞态，避免正常启动、`config check`
  和 `doctor` 无期限等待；普通文件符号链接仍可用，诊断不回显配置正文。
- 批处理收到 SIGTERM/SIGINT 后停止新进程准入，先回收 agent、阶段命令与验证器
  进程组，再释放实例锁；覆盖 spawn 窗口和排队验证器，拒绝中断后的假成功。
- 已持久化 UNKNOWN 的 TUI 会话重启后保持只读，即使旧任务已从有界投影中淘汰；
  显式 `/new` 可继续工作，已确认成功的会话仍可续接，原 journal 不被改写。
- Linux 持续验收工具通过有界目录通知发现新会话，避免大量历史目录拖延恢复判定；
  最终检查仍完整遍历所有 journal，不依赖实时发现清单。
- 任务与流水线 YAML（含自动路由、锁预扫描）在构造合并引用前检查深度、节点数和
  展开字节预算，拒绝循环、非有限数值和非 JSON 类型；解析错误不回显原始输入，
  正常锚点、合并模板和顶层任务列表保持可用。
- TUI 收到 SIGTERM/SIGHUP 时走正常取消和退出清理，回收活动 provider；
  未确认终态的本地任务保留 UNKNOWN，退出码为 128 加信号值，退出后恢复原处理器。
- 同名二进制构建互斥，每次编译使用独立目录；拒绝编译器未生成新产物的假成功，
  发布前复核源码、二进制与许可证，最后发布成功清单，防止并发构建错绑来源。
- stdin prompt 按 UTF-8 字节预算读取，超限和损坏编码在日志、锁与执行前拒绝；
  任务锁预扫描共用有界读取，prompt 文件拒绝 FIFO 等特殊文件及打开时的替换竞态。
- TUI 活动日志和 UNKNOWN 补记均写入失败后，恢复存储再执行 `/new` 会先补记
  旧任务的未确认终态，避免 legacy 模式被晚到完成事件持续阻塞；失败仍可显式重试。
- TUI 任务和 App Server 审查在启动 RPC 前记录本地提交意图；确认前失败、取消或
  超时直接收尾为 UNKNOWN，不补造 provider started。运行中日志故障不再遗留忙碌状态，
  补记无法确认时明确阻断后续执行；预备写入失败保留普通任务和 `/review` 输入。
- TUI 空闲事件写入会话日志失败不再让提交任务时的界面崩溃；保留有界待写入事件和
  普通任务输入，在持久化预备成功前拒绝执行与会话切换，恢复存储后可显式重试且不重复记录。
- POSIX Pi RPC / App Server 使用可中止的非阻塞管道读写，关闭后即使仍有脱离进程组的
  后代持有管道，旧读写操作也会结束；初始化管道失败回收已启动的进程和全部标准管道。
- 配置在构造 YAML 合并引用前检查循环、深度与展开量；拒绝非有限数值、非文本键、
  非 JSON 类型和过大整数，非法显式类型标记的诊断不回显原值。正常共享引用与合并仍可用，
  配置读取始终受 2 MiB 硬上限约束；数值设置转换溢出或非有限字符串按既有约定警告并回退。
- Pi RPC / App Server 关闭时立即释放三条标准管道的本地文件描述符，避免保留 transport
  对象时累计泄漏；正在读写的缓冲锁不再阻塞关闭，并发和重复关闭不会误关复用的描述符。
- Pi RPC / App Server 在空闲时或上一轮完成后断连，`/new` 可重建连接；重连
  暂时失败可再次显式尝试，旧会话保留故障证据，取消/断连任务仍为 UNKNOWN，不自动重放。
- App Server 协议按字节分块读取，单条记录最多 8 MiB；无换行、超长或损坏
  UTF-8 不再造成无界等待和内存增长。stderr 增量保留有限尾部，退出前也可诊断。
- 产物预览拒绝 FIFO 等特殊文件时不再阻塞界面；命令产物分配或权限设置失败
  会关闭已打开的目录/临时文件描述符并清理本次临时文件，后续任务可重试。
- 二进制的打包默认配置不再覆盖用户设置；配置读取/解析失败在普通执行前拒绝，
  `config check` 和 `doctor` 可输出定位信息，无需通过 Python traceback 查错。
- 实例锁的检查、死进程回收、发布与释放共用有期限的系统锁，消除竞争回收误删新锁；
  每个获准并行实例保留独立记录，后续实例也检查其会话与输出路径（含父子目录）冲突。
  排队遵守截止时间，失败释放保留所有权以便重试；坏记录和非本机记录不能自动回收。
- PID 存活探测使用跨平台只读接口，权限不足或未知错误不再视为死亡；Windows
  不再用会终止进程的 `os.kill(pid, 0)` 探测。运行登记、triage 与 watchdog 共用该判断。
- 二进制冒烟默认启用实例锁，实际验证冲突拒绝、`ps` 无副作用、排队交接与释放后复用。
- 核心文件锁统一走可移植入口：POSIX 仍使用 `flock`，Windows 使用原生字节范围锁，
  避免导入 `fcntl` 阻断 CLI；运行登记和熔断器不再依赖 Windows 缺失的系统 API。
  这不代表 Windows TUI、进程树回收或全部命令已完成移植验收。
- 冻结解释器显式启用 UTF-8，避免 Windows 默认编码使中文门禁输出在重定向时崩溃。
- App Server 初始化期限覆盖 transport 创建、写入、响应和最终通知；超时关闭连接，
  迟到结果不能恢复可用状态。初始化/请求/审批期限拒绝非法及非有限值。
- 验证器与任务拒绝非有限超时，双流输出超限立即停止，损坏的 agent 正文拒绝落盘；
  正常父进程先退出时也回收同组残留进程，并保留分块 Codex 会话绑定。
- 二进制内置 checker 的字面参数直接传递，保留路径中的空格/引号；复合命令继续
  使用 shell，命令匹配不再因长路径与 shell 后缀触发指数级回溯。
- 二进制内部自调用不再把可执行文件误当 Python 解释器；内置验证器使用包内运行时，
  质量/能力/依赖图检查及 Campaign 工具指纹读取随包源码快照。发布冒烟覆盖空 PATH
  与含空格解包目录下的门禁、任务、schema 拒绝、流水线、隔离子任务及 POSIX TUI 启停。
- Pi RPC / App Server TUI 任务与审查增加完整执行期限；审查支持取消和退出清理，
  transport 阻塞写入及父进程先退出时仍回收任务进程组，未确认终态保持 fail closed。
- Pi RPC / App Server 首次连接保留启动通知的会话身份，避免正常启动事件被当作
  跨会话事件拒绝；终态队列读取超时后复查已完成的入队，避免误判终态缺失。
- 首次运行没有可选用户 settings.yaml 时，`config explain/check` 正常使用默认配置；
  显式指定的缺失配置和现存坏配置仍然拒绝。
- 设备清单中的重复 ID 在投影前拒绝，避免字典汇总静默覆盖异常记录并错误派发任务。
- 供应商熔断器的等待预算不再小于单段冷却。原先 `cooldown_failure` 硬编码
  300s 预算，而默认 `max_cooldown` 是 900s：冷却时长落在 (300s, 900s] 时
  `wait_or_fail` 一次都不等待就以「exceeded wait budget」失败——等待闸门退化为
  立即烧掉任务，campaign 在 provider 退避期持续空转。预算现默认等于
  `max_cooldown`（可选 `limits.provider_breaker.wait_budget` 覆盖，并以
  `max_cooldown` 封顶），quota 级冷却（默认 24h）仍超预算快速失败；
  等待期间若 provider 被再次拉长冷却，仍按预算明确失败而不假装可执行。
- 供应商熔断器现在识别 opencode-go / Go 网关的额度耗尽签名。实际报文为
  `429: {"type":"GoUsageLimitError","message":"Go usage limit exceeded"}`——
  camelCase 类名 + 空格分隔短语，既有的 `quota_?exceeded` / `rate_?limit_?error`
  均不匹配，因此共享熔断器始终不触发。2026-09-19 的 iris-ui 24h campaign 据此
  在额度墙后空转约 8 小时 / 1412 次供应商错误（每 ~2 分钟一个零进展轮次）。
  签名已加入内置默认表，无需任何按 agent 配置。
- campaign 轮次级停滞熔断不再与可选的自修复（stash 回滚）耦合。原先
  `update_round_recovery` 在 `recovery.enabled` 为假（默认值）时直接返回
  `(0, "")`，导致「连续零进展轮次即 fail-closed 停止」这条安全闸在默认配置下
  完全不可达——即上述 123 个零进展轮次持续 8 小时的直接原因。现在停滞计数与
  停止判定始终生效，仅破坏性的 restore/self-repair 保持 opt-in；
  `recovery.stop_after_stalled_rounds: 0` 仍可显式关闭。
- 只读阶段不再要求工作树干净。原先 `read_only_violations` 在基线非空时直接返回
  `read-only stage started with existing changes`，导致任何已脏的共享工作树都
  无法通过 requirements/design/review/gate 阶段——而这与 campaign 管线模板自己
  “preserve unrelated dirty changes”的指令相矛盾。2026-09-18/19 日志中该错误出现
  **43 次**，每次都在 requirements 阶段终止管线（98 个 PIPELINE_FAILED、 0 次提交的
  主因之一）。现在改为与基线**比较**：新增的脏文件仍为违规，且基线携带逐文件内容
  摘要，因此对已脏文件的**改写**同样会被发现（比原先仅比对路径集合更严），
  声明的产出文件仍被允许。
- campaign 不再因 `gate: true` 把**会提交**的阶段误判为只读。
  `_scope_pipeline_outputs` 原先用 `meta || gate || name in {requirements, design}`
  推断只读；当模板把审查闸挂在实施阶段上（iris-24h 的
  `implement: gate: true` + `git_commit: true`）时，刚通过闸的 diff 反而被
  只读校验判为违规，管线在 implement 阶段终止——这是该 campaign
  **98 个 PIPELINE_FAILED、 0 次提交**的结构性原因。现在：带 `git_commit`
  的阶段不再被推断为只读（显式 `read_only: true` 仍然优先），而独立审查闸
  （无提交）与 requirements/design 仍保持只读。runner 自带的模板本就是
  “实施阶段 + 独立 code-review 阶段”的形状，行为不变。

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
