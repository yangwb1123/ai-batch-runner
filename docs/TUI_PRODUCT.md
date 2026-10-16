# pi-batch TUI 产品契约

`pi-batch` 的交互界面是现有执行内核的产品入口，不是第二套 runner。
批处理、流水线、验证器、会话、记忆、超时和进程组清理仍由 `pbatch`
现有模块负责；TUI 只负责输入、状态呈现和用户控制。

## 启动契约

- `pi-batch`：仅当没有参数且 stdin/stdout 都是 TTY 时进入交互界面。
- `pi-batch tui`：显式进入交互界面；非 TTY 环境 fail closed（exit 2）。
- `pi-batch pi ...`：参数原样交给 canonical
  [`earendil-works/pi`](https://github.com/earendil-works/pi)，覆盖官方
  interactive/print/JSON/RPC/auth/package/export 调用面；不经过本 TUI 的参数解析。
- `pi-batch tui --runtime pi-rpc`：通过长期本地 `pi --mode rpc` 进程运行产品
  TUI；默认 `auto` 在 agent basename 为 `pi` 时选择此 runtime。
- `pi-batch tui --runtime app-server`：显式建立 Codex App Server stdio 连接；默认
  `auto` 在 agent 为 Codex 时保持 Structured Exec 路由；其他 CLI 使用 Legacy。
- `pi-batch -p ...`、stdin pipe、所有子命令和 `--json` 输出保持脚本兼容，
  不输出全屏控制序列。
- 首屏是只读的。未安装或未配置 agent 时仍能打开，第一次提交时给出可操作诊断。

这些入口不是同一层 API。`pi-batch pi` 是原样 CLI 子进程，不增加本地 journal、
inspector、validator 或 retry；`pi-batch tui --runtime pi-rpc` 才是由本产品托管
Pi RPC 的 TUI。`pbatch.create_agent_session()` 是同步的 provider-neutral Python
封装，并非 Pi 官方 SDK 的 Python 移植或 wire-compatible binding；Node/TypeScript
应直接使用官方 `@earendil-works/pi-coding-agent` 的 `createAgentSession()`，需要
new/resume/fork/import 等 session replacement 时使用 `createAgentSessionRuntime()`。

参数命名也按 namespace 隔离：pi-batch 顶层 `-p PROMPT` 与 `--mode
serial|parallel` 属于批处理 runner；`pi-batch pi` 之后 `-p/--print` 是 Pi 的布尔
one-shot flag，prompt 是 positional message，Pi 自己解析 `--mode
text|json|rpc`。其中 raw `--mode rpc` 独占 stdin/stdout 传 LF-delimited JSONL，
不能当成人类交互 TUI；产品 TUI 请使用 `--runtime pi-rpc`。

## MVP 用户旅程

1. 打开当前 workspace，查看 Git、agent、model/provider、memory 和 session 状态。
2. 在底部 composer 输入自然语言任务；一个会话内连续提问复用同一 agent session。
3. 模型正文实时进入对话区，stderr 和生命周期信息以独立状态呈现。
4. 运行中的直接 turn 启动仍防重入，但 composer 保持可输入：普通文本进入当前
   session 的进程内 follow-up queue，用户也可显式 steer 或取消当前 turn。backend
   接受取消时必须终止对应 agent 进程组；提交前取消不调用尚未启动的 provider hook，
   hook 异常可重试；若 backend 拒绝，界面告警并保留真实完成/失败终态。
5. 完成或失败后回到可输入状态，可继续、切换新会话或查看诊断。

Pi RPC 与 App Server 直接任务也遵守 `--timeout`（未设置时采用配置默认值）：
截止时间从界面接受任务开始计算，覆盖启动 RPC，持续输出不会延长执行窗口。
超时或取消会按当前 handle 尝试中断，并关闭对应本地 transport、回收进程组。
无法确认 provider 终态时保留未知状态，阻止继续使用该 runtime；关闭完成后通过
`/new` 重新连接，不把本地停止伪装为 provider 成功完成。
普通任务与 App Server 审查在调用启动 RPC 前先持久化本地 QUEUED，绑定本次
workspace/session/run/turn；该记录只表示提交意图，不证明 provider 已接受任务。
启动确认前失败、取消或超时可以直接从 QUEUED 收尾为 UNKNOWN，不补造 started。
日志写入预备失败时不调用 provider，并保留普通输入或 `/review` 命令供显式重试。
默认启用 journal；显式禁用后，这些本地记录只存在于当前进程的投影中。
空闲期间或上一轮完成后断连同样可以用 `/new` 重连；若 provider 暂不可用，
可再次显式尝试。重连保留旧会话的故障证据，开启新会话且不会自动重放任务。

必需 runtime 折叠先于终态完成：执行消费者只在 journal/controller 与适用的审查
capture 成功返回后，按原绝对 deadline 接受完成/失败/中断。重复/提交过滤的
终态不能只凭 kind 或 handle.done 作为新的完成；空闲/恢复去重仍为幂等无操作。
读入回执的 True 表示必需折叠准入，不是呈现成功、独立验收或执行授权；显式
False 的非终态不进入 Structured TaskResult 正文、stderr 或 message-item 投影，
未准入终态阻断完成。旧 void sink 的 None 与无 sink 回调路径保留，不重新定义
其他历史返回值；presentation RuntimeError 丢失显示不抹去已经折叠的 provider 事实。
App Server 审查同样不捕获未准入正文/finding，capture 异常或耗尽期限不得提前
解除 watchdog。同步呈现目前仍可能占用消费窗口，不因此延长或重置任务期限。

Structured Exec backend 复用既有 `RuntimeOperation`：adapter 构造完成后、初始化
之前启动原 timeout，检查初始化、会话交接、handle 绑定、消费与必需折叠后的原
期限；及时确认的终态不因协调线程/过期 timer 迟醒反转。stream 关闭或 done 本身
不是终态证据。构造、任意阻塞第三方调用/文件 I/O 不新增硬实时保证；原 stop、
grace/join 与输出/item 上限不放宽，没有自动 provider 重试或账户/模型切换。
provider 终态已保存但本地 deadline/准入失败时保留原事实，不补造矛盾的 provider
失败；界面明确显示 Failed (local acceptance)，而非 Ready/succeeded。此显示不是
副作用回滚，也不证明真实 provider 失败根因或设备/部署验收。

关闭 transport 会显式释放本地 stdin/stdout/stderr 描述符，不依赖对象回收。
POSIX 管道使用可中止的非阻塞读写：即使脱离进程组的后代仍持有管道，关闭也会
结束本地阻塞的读写操作，专用读取线程退出，线程池中的执行线程可以继续复用；
反复重连不会累积等待读写的线程，大数据分段写入仍须完整发送。
关闭与每次读写共用所有权检查，避免误读写已被复用的描述符。进程清理覆盖拥有的
进程组，自行脱离该组的后代不会因此被自动终止。

提交任务或切换会话前，空闲事件必须先写入会话日志。磁盘满或目录权限错误时，
界面保留待写入事件并拒绝继续启动任务；普通输入也会留在输入框中。恢复存储访问后
显式重试原动作或 `/new`，已经确认写入的事件不会重复记录，也不会自动重放任务。
未保存的事件只保存在当前应用进程内，请在关闭窗口前处理日志错误。
运行中的日志写入失败也会结束界面忙碌状态；如果连 UNKNOWN 补记都无法确认，
界面会明确报告结果未确认、阻断后续任务，并要求恢复存储后通过 `/new` 重连。
日志仍可写时，`/new` 会先重试保存这条 UNKNOWN 记录，再处理旧会话的晚到事件；
再次写入失败时保留恢复记录供显式重试。已判定只读的旧日志保留可信前缀，并隔离
晚到事件。晚到的成功结果不能覆盖 UNKNOWN，也不会触发任务重放。
这不保证故障期间的事件全部落盘，恢复时仍以日志的可信前缀为准。

`/review` 支持 Ctrl+C 和退出取消。独立审查进程会回收自身进程组，取消请求在
启动前后保持有效；App Server 审查同样使用截止时间与有界关闭。审查未显式设置
`--timeout` 时保留既有审查时间预算，取消和超时均不会被记录为成功审查。

Codex App Server 的初始化默认独立使用 15 秒预算，覆盖创建连接、发送握手、等待
响应和确认通知；超时关闭连接，迟到的响应不能将连接标为可用。普通 RPC 仍使用
5 秒预算，`--timeout` 继续只控制任务。可用 `--runtime app-server --startup-timeout 30`
为较慢的启动环境指定初始化期限；该参数必须为有限正数，其他 runtime 不接受它。
Python adapter 的显式 `request_timeout` 仍控制启动与普通请求，除非另外指定
`startup_timeout`；省略两者时才采用默认的 15/5 秒组合。

App Server 协议按字节分块读取，每条 UTF-8 JSONL 记录（含换行）最多 8 MiB；
超长、损坏编码或缺失换行的尾记录会关闭本次连接，不把部分输出当作完成结果。
stderr 持续排空，只保留最近 64 KiB，断连诊断展示末尾 8 KiB；没有换行也不会
无限累积内存或隐藏最近的错误内容。

任务与验证器在启动前拒绝 NaN/Infinity 等非有限超时。底层进程按可用字节增量读取，
stdout 或 stderr 超限即停止，无换行输出也受约束；损坏的 agent 正文拒绝落盘，
验证器诊断以替换字符保留。POSIX 上，父进程正常退出后仍回收它的同组残留进程。

Slash 命令提供帮助、状态、typed `/session`、后台 `/compact`、新会话、会话 picker、follow-up queue、steer、typed
approval、清屏、runtime/agent/model/provider/memory 设置、诊断和退出；命令面板提供
new/clear/status/plan/changes/queue/approvals/agents/breaker/files/sessions/refresh/doctor/help
等高频动作，并补齐 typed session/model/thinking 入口。`/help`、dispatch、palette 和
busy allowlist 共享一个 immutable command registry；alias 不再维护独立分发表。
composer 中输入命令名前缀后可用 Tab/Right 补全，部分名称上的第一次 Enter 只填入
完整命令，精确名称仍直接提交。静态补全完全本地运行，不触发 RPC、runtime rebind
或任何 setter。键入 `/model QUERY` 或 `/thinking QUERY` 精确参数上下文时，最多读取
一次绑定 adapter identity、local session id、kind 与 generation 的只读快照；粘贴只
登记上下文，显式 Tab 才开始读取。后续在冻结快照上本地 fuzzy，同一上下文内继续输入
不会逐键调用 RPC。picker 内的 Tab/Enter 只把选中项填回 canonical slash 文本，不提交
也不调用 setter；回到 composer 后第二次
Enter 才进入正常 handler，并重新执行 fresh catalog/levels、busy、版本与 scope 门禁。
completion-only 路径复刻 Pi 的 token fuzzy 计分与 model 搜索字段；thinking 只搜索
provider 返回的 level 本身，不借用设置 picker 的说明文字扩大匹配面。
未知 slash 命令不会作为 prompt 发送给 agent。空闲时切换 `/agent`
会重绑运行时并开启 runtime-local 新会话；Pi RPC 的 `/model` 与 `/thinking` 在
0.84.3+ 经版本证明后直接修改当前 session，并以 state readback 为准，provider 变化
仍通过重绑长驻进程生效。Codex Structured Exec 的 `/model` 会传入下一轮执行，但
`/provider` 覆盖会在切换或执行前明确拒绝。

Pi RPC 下，不带参数的 `/model` 打开 fuzzy picker；`/model <query>` 仅在大小写
不敏感地精确命中 canonical `provider/model-id` 或唯一裸 ID 时直接执行 session-only
切换，其他 query 进入 picker 模糊过滤。不带参数的 `/thinking` 打开当前模型动态
levels picker；带参数时只接受动态列表中大小写不敏感的精确 level，并只修改当前
session。两者均以 mutation 后的 state readback 为准，model 变化后重取 levels。
托管 JSONL RPC 没有 `persist` wire，因此不提供或模拟官方原生 TUI 的 Ctrl+S
持久化；Ctrl+S 只提示不可用并保持 picker 打开。旧版 Pi 或
`settings_scope=global` 的 picker 仅可浏览；动态补全仍可读取并填入候选，但提交不能
越过 session-only setter 门禁，Enter 不修改状态或关闭设置 picker。
Pi 官方 `/scoped-models` 没有对应的 JSONL RPC scope-list 读写协议，因此当前不注册
该命令；RPC `get_available_models` 是 full available catalog，不等于 interactive
`session.scopedModels`，也不能推导 cycle membership/order。上游的区别见固定提交中的
[interactive autocomplete](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/modes/interactive/interactive-mode.ts#L646-L683)
与 [RPC catalog contract](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/rpc.md#L255-L327)；
cycle 快捷键继续等待独立 typed mutation/readback 能力。

Pi RPC 声明对应 capability 后，`/session` 在独立 worker 中执行 `get_state` 与
`get_session_stats`，只展示本地 session、model/thinking、streaming/compacting、
message/queue、token/context/cost，不读取完整 messages，也不展示 provider session
文件路径；活动 turn 中仍可只读查询。`/compact [instructions]` 仅在 idle session
启动，立即返回界面并在后台等待同一个 non-cancellable handle。等待期间不复用 turn
的 `_busy`/cancel/steer 语义，但持有独立逻辑锁，普通 turn、session replacement 和
runtime/model/provider 设置都会 fail-before-call。Pi RPC 没有 `abort_compaction`，
Ctrl+C 只提示不可安全取消；adapter/session 边界变化后，旧 worker 的结果静默丢弃。

## 活动 turn 控制

- agent turn 运行时，普通输入默认加入 FIFO follow-up queue；队列上限为 16 条、
  总计 64 KiB、单条 16 KiB。`/queue` 在独立 overlay 查看，`drop`、`edit`、
  `clear` 和 `run` 用于管理，不向 conversation transcript 注入检查内容。
- 队列属于当前 TUI 进程，不写入 journal，也不声明 runtime 的 `message_queue`
  capability；提示只有真正启动为下一 turn 后才进入 transcript。切换 session/runtime
  会清空旧队列并告警，避免把提示带入错误上下文。
- 当前 turn 成功后自动启动队首；失败或取消时保留并暂停。只有 `_start_turn` 已接受
  新 turn 才移除队首，因此启动失败不会丢消息。
- `/steer <text>` 只接受活动 agent turn。界面从已初始化 runtime 读取
  `turn_steer` capability，并要求匹配的未终结 turn handle；检查不会重初始化 runtime，
  请求在后台发送。Codex Structured Exec 与 Legacy 当前均不声明该能力；显式
  `--runtime app-server` 已接入主 TUI，并按 provider `expectedTurnId` 发送。
- `turn.steered` 只有作为可信 RuntimeEvent 才进入 Timeline；Assistant 正文中的同名
  文本不能改变控制状态或伪造 steer 成功。

## Typed approval

`--runtime app-server` 握手成功后才声明 `typed_approvals`。服务端 JSON-RPC request
形成 `approval.requested` 时，当前快照进入 `WAITING_APPROVAL` 并自动打开卡片；
`/approvals` 可在活动 turn 中重新打开。`a` 允许一次、`s` 允许当前 session（仅当
request 提供）、`d` 或 Enter 拒绝、`i` 中断，Esc 只关闭卡片而不作决定。

decision 同时绑定 workspace/session/run/turn/actor、approval type 以及保留 JSON
类型的 request id；跨 scope、错类型、重复、过期或恢复历史中的 decision 均 fail
closed。pending 默认最多 32 个，超限和 TTL 到期自动向 App Server 返回 decline；
断线清空，TUI 正常退出会在关闭 transport 前尽力拒绝 outstanding requests。
request/resolved/expired 是 append-only journal 事实，检查和按键不会进入 conversation
transcript。Assistant 正文中的 `approval.requested`、按钮文字或 request id 只是文本。
详细合同见 [`t26-appserver-approvals.md`](tui-phase0/t26-appserver-approvals.md)。

App Server 的 initialize、thread/start、turn/start 及对应 started 通知中若明确携带
有效 `approvalPolicy`、`sandboxPolicy` 或 `networkAccess`，状态区会显示服务端观测值；
缺失值显示 unknown，结构化值只显示 present。状态区不从本地默认值推导权限，也不替代
服务端审批或授权决策。合同见 [`t31-appserver-policy.md`](tui-phase0/t31-appserver-policy.md)。

`/sessions` 从当前 resolved cwd 与 memory scope 对应的 journal 目录派生一个有界索引，
并在独立键盘 overlay 中显示时间、大小和 candidate/read-only/archived/current 状态；
不会把索引写入 conversation transcript。只显示通过 metadata 边界校验的精确 session；
符号链接、跨 scope 和无效 metadata 只会被跳过。Projection 成功提交后会更新一个与
精确 journal byte size 绑定的小型 `summary.json`，显示 runtime、可信 branch、最后 turn
状态和有界完成消息；旧/缺失/损坏缓存仅标为 unavailable，不会触发列表时批量重放。
Enter 或
`/resume <session-id>` 先重建可信投影，再检查 runtime 的
`session_resume` capability、runtime 身份和 provider handle。全部匹配时 Codex
Structured Exec 在下一 turn 续跑原 provider thread；显式 resume 失败会 fail closed，
不会退化成无上下文的新会话。损坏/未决 journal、缺失 handle、Legacy 或 runtime
不匹配时仍可查看可信历史，但 composer 被阻断，需 `/new` 后才能执行。
显式 App Server 还会在 `thread/resume` 前调用稳定的 `thread/read` 摘要接口：只允许
`notLoaded`/`idle`，provider 报告 `active`、`systemError`、未知结构或读取失败时不会
采用远端 thread，仍以只读方式显示本地 journal。Picker 另以 worker 调用一次有界
`thread/list`，固定 cwd/source kind 并保留分页信号；name/preview/modelProvider/status
只作带观测时间的 advisory 展示，remote-only thread 不进入本地列表。点击 resume 时仍
重新恢复 journal 并执行 `thread/read`，列表状态没有授权或否决权。

Picker 的 `f` 或 `/fork [session-id]` 只在来源 journal 可写、可信 provider handle、
runtime 身份和 `session_fork` capability 全部匹配时建立新本地 session。Codex
Structured Exec 将 fork 标为 required，并在新 session 的下一 turn 执行真实 provider
fork；失败不会降级成 resume 或 fresh session。Picker 的 `a`、`/archive <id>` 与
`/unarchive <id>` 管理本地可逆 archive 标记：事件历史不移动、不删除，archive 后
append fail closed；活动 session 和含未决 turn 的 session 不能 archive。远端 provider
归档必须显式使用 `/provider-archive <id>` / `/provider-unarchive <id>`，仅 App Server
在 `session_archive` capability 和可信 provider handle 均满足时执行；本地 marker 与
远端状态始终分开呈现。

`/product [requirement]` 在本地生成确定性的产品方向简报，不启动 agent，也不写入
工作区。省略 requirement 时仅使用当前会话最近一次实际启动的普通任务；`/clear`
只清理可见内容并保留该任务，`/new` 清空该引用，禁止跨会话带入历史上下文。

工作区审阅保持只读：`/files` 展示 staged/unstaged/untracked 文件，
`/diff [path]` 展示有界 unified diff（包括未跟踪文本文件），`/refresh` 刷新
Git 状态；`/changes [all|unstaged|staged|untracked|last-turn] [path]` 与 **Ctrl+G**
打开专用 overlay，数字键 1-5 切换本地 scope，文件列表按 Enter 载入，`n`/`p`
切换 unified diff hunk，`r` 刷新当前快照。`/findings` 打开结构化 review finding
列表；Enter 仅把经过 workspace-relative path/line 校验的 finding 作为只读 Changes
定位目标，并选择最接近的 unified diff hunk。`/changes base REF [path]` 和
`/changes commit REF [path]` 先把 ref 验证并解析为 commit，再执行参数化 Git 比较；
以 `-` 开头、含控制字符、超长或不存在的 ref 会 fail closed。所有 path 继续限制在
resolved Git root 内，输出、文件数和 hunk 数均有上限，操作不写入 transcript，也不
修改 index/worktree。active turn 期间允许打开 all/unstaged/staged/untracked 的
LIVE 采样视图，但明确它不是原子快照；last-turn 与 ref 比较等待 turn 结束，避免把
变化中的 worktree 冒充稳定证据。每轮结束后若 HEAD 或工作区发生变化，
对话区会提示脏文件数和可见路径。输入框支持 Up/Down 浏览当前进程内最近 100 条
输入，并在回到末尾时恢复尚未提交的草稿。

`/plan` 打开独立只读 Plan overlay。它直接读取当前 Timeline 条目携带的结构化
`PlanSnapshot`，不从 Assistant 正文推断控制语义，也不维护第二份计划状态；同一
`item_id` 的更新继续原位替换。Timeline 保持 2 KiB 预览预算，Inspector 可展示同一
投影中最多 64 个、单项 256 bytes 的完整有界步骤，并在打开期间随可信事件刷新。
validator、gate、review 与 child actor 的可信事件也以独立 bounded Timeline 条目展示；
child actor 必须携带匹配的 `parent_actor_id`，不会改写 root turn actor。Native Pipeline
的 stage/task 聚合由 opt-in `run_pipeline(event_sink=...)` observer 提供；显式
`app.attach_native_pipeline(workspace_id)` 可将同一只读事件流接入 TUI Timeline/Completion，
且仅接受同 workspace 的 `pbatch-native`/`read_only` 事件。用户也可通过
`/pipeline FILE [--reuse]` 或 `tui --pipeline FILE` 显式启动既有 Pipeline runner；approval stage 通过单独的 exact-run-scoped
`NativePipelineControlChannel` 进入 TUI approval modal；whole-pipeline cancel 只做
本地 cooperative stop，不发送 provider stop，未建立 channel 时仍 fail closed。
通过 `--orchestrator-only --persistent-pipelines` 可改由 detached worker 执行：
流水线在 TUI 关闭后继续，UI 存活时轮询进度，空闲时用 Ctrl+C 请求 cooperative cancel，
完成后仍回到同一 Pi 控制器会话。任务账本只存有界状态、产物路径和最多 8 KiB stdout，
不存 prompt/产物正文；每个任务最多执行 8 次（含首次执行），恢复前校验 workspace、流水线和控制器指纹。
该模式拒绝包含交互 approval 的 pipeline。detached supervisor 会在最多 8 次尝试内重试
锁延期和 worker 崩溃；supervisor 自身退出后，需下次以 `--persistent-pipelines` 启动 TUI 恢复。
它不是 OS service，主机重启后需外部 service manager 再启动 TUI。

`/agents [agent]` 打开独立只读 Agents overlay，`/runs [agent]` 是兼容别名。
当前活动 turn/review 只作为当前 TUI 进程已知的 coordinator operation 展示，并明确
标为 `current`；它不是终态账本记录，也不从 Assistant 正文猜测 agent 状态。runtime
projection 另从可信 `actor.spawned/status/completed` 事件建立当前 turn 的有界 Actor
Tree，并按 retained projection 记录聚合历史 turn 的 provider/meta/Pipeline child 树；
Native Pipeline 的 stage/task/meta-role 也沿用同一 parent 校验；parent、actor、run
和 turn 不匹配时拒绝，截断会显式告知。显式 provider `actorAuthority` 只作为非权威
bounded metadata 展示，继承声明必须匹配 exact parent，不是执行授权。Agents overlay 用
`h` 在当前树与历史树之间切换。最近终态来自当前
resolved cwd/data_dir 对应的 `<data_dir>/agents/ledger.jsonl`，按新到旧最多展示 100
条；账本总量为 16 MiB，读取单行上限 4 KiB。损坏、超长或缺少必要 agent 字段的记录会
跳过并在 heading/detail 显示诊断，结果被截断时也会显式告知。`a`、`s` 分别轮换
agent/status 过滤，`r` 重新读取共享账本；详情只展示有界任务摘要、run/session、耗时、
退出码和最多 8 个产物路径引用，不读取产物正文。该 Inspector 不写 conversation
transcript，也不把运行历史冒充 provider actor tree；历史 actor 树仍只来自 retained
trusted RuntimeEvent projection。没有对应 runtime capability 和 actor identity 时不提供
单 actor stop/steer。

`/review` 完成后可用 `/findings` 打开独立只读 finding picker；它只消费结构化
review payload 或 canonical `review.finding`，不从报告/Assistant 正文推断路径。
Finding 仍是审查意见，不等同于工程门禁结果；Enter 跳转只定位只读 diff，不等同于已修复。
`f` 仅在用户明确选择后把该 finding 编译为一个有界的普通 agent turn：review 字段被
标记为不可信数据，agent 必须先检查当前代码、最小化修改、不自动提交，并报告实际
执行的检查。该动作复用普通 runtime 权限/validator，不是自动 fixer，也不会把 finding
标记为 resolved。普通 fix turn 结束后，若存在 exact terminal RuntimeEvent，会生成
用户级 runtime `reviews/review-fix-<id>.md` 有界证据报告，关联源 review/finding、fix turn、validation、gate
和 authoritative workspace facts；缺少完整 typed scope 时明确标为 unavailable，不把 agent
正文或退出提示升级为通过。后续必须显式重新 `/review` 才能重新评估 finding。
`/completion` 打开独立只读 Completion overlay，并在活动 turn 中随 immutable
`TUIViewSnapshot` 原位刷新。该投影只消费 canonical event kind、显式 payload、当前
run 的有界 usage 统计、最新结构化 Plan 和 authoritative `workspace.snapshot`：展示
终态/耗时/return code、命令退出状态、权威变化路径、未完成 Plan 步骤、tool、
validation、gate、artifact 引用及 runtime warning/error。Assistant 正文即使包含
`validation.completed`、`VERDICT: PASS` 或类似文本，也不能生成对应事实。没有
validation/gate event 时界面明确写“无验证证据/不声称通过”；provider `file.changed`
只作为 observed Timeline 信息，不能替代 after-turn Git delta。

Completion 各动态集合独立有界：command/tool/validation/gate/artifact/plan 各最多 32，
runtime diagnostics 16，权威路径 64，usage key 32；非有限 usage 丢弃，任一截断在
Evidence limits 中点名。Completion 本身只展示 artifact 引用，不读取正文；按 `a` 或
执行 `/artifacts [N]` 可进入独立 Artifact Inspector。终态 journal 恢复会重建同一投影，
callback-only backend 仅在显式
`last_result` 终结后使用本地 snapshot 回退，活动 turn 不复用上一轮结果。

Artifact Inspector 只读取当前 run/turn 的 canonical `artifact.created`，最多保留最新
64 个引用；Assistant 正文、command 文本与普通 `file.changed` 均不能生成 ArtifactRef。
引用列表和选中项会随 typed event 原位刷新，Completion 中按 `a` 可叠加打开；检查内容
不写入 conversation transcript。默认 `scope=workspace`；只有 Adapter 显式声明
`scope=data` 时，Inspector 才使用当前进程根据同一 workspace 解析出的 `config.data_dir`
作为第二个授权根，事件不能提供或替换根路径。两个授权根内的 UTF-8 普通文件可以显示
前 64 KiB。读取使用目录 fd 逐级打开并要求 `O_NOFOLLOW`，所以越界路径、
符号链接（含中间组件）、目录、设备、缺失/不可读文件均显式停止；远程 URI 不联网，
二进制或非 UTF-8 内容只显示元数据。界面不执行 shell、不启动外部 viewer，也不把
声明的 media type/size 当作本地文件真值；实际文件大小来自已打开 fd。
最终文件以非阻塞方式打开后再检查类型，无写入者 FIFO 也会立即拒绝；缺少这些
安全打开能力的平台仍拒绝预览。命令产物创建失败会释放本次文件描述符和临时文件。

Codex Structured Exec 和 App Server 把 command output 与正文严格分层：事件 payload
继续只保留约 48 KiB 的有界预览，Timeline 只保留 2 KiB；只有可信 command item 的
aggregate 超出事件预算，或 App Server output delta 累计超出预算时，Adapter 才创建
`kind=command_output, scope=data` 的 `artifact.created`。Assistant 文本、stderr 和普通
callback 不能触发该路径。文件写入 `<data_dir>/tui/artifacts/<workspace-hash>/<session-hash>/`，
run/turn/item 只进入不可逆文件名摘要；目录 0700、文件和 retention lock 0600，临时文件
完成 fsync 后原子替换。默认单 artifact 最多 2 MiB（截断时写入明确 marker），每个
workspace/session 最多 64 MiB 或 256 个完成文件，锁内淘汰最旧项；恢复后被 retention
淘汰的旧引用诚实显示 missing。存储失败只发一次 `runtime.warning(category=artifact_store)`，
绝不把成功 command 或 turn 改判为失败。

当前进程内的可见 transcript 最多保留 400 条、512 KiB 文本，单条最多
128 KiB；淘汰旧内容时显示省略提示，并保护正在流式写入的 assistant/error
条目及其索引。当前 turn 的 assistant/error 事件投影各使用 64 KiB 滑动尾窗，
界面仍按绝对窗口位置持续追加最新 delta；多个 assistant message 按 `item_id`
原位完成并共享这一个预算，不会因后一条 completed 而覆盖前一条。该可见容量治理
不冒充完整聊天归档；跨进程只持久化下面定义的有界 RuntimeEvent 投影日志。

provider item id 在进入投影前使用 `raw:` / `sha256:` 命名域，超长值携带原值摘要，
不能因相同截断前缀或摘要伪装而合并；没有 raw event sink 的兼容 callback 使用
`stdout-replace` 表达非前缀 canonical 完成，而不是继续展示过期 draft。

## 持久恢复合同

- 可信事件持久化完成后才对界面可见；写入或恢复边界存在歧义时，session 进入共享
  只读状态，不猜测成功。
- 恢复只采用完整、有序的可信前缀；未决 turn 显示 UNKNOWN，已确认终态不回退，
  容量耗尽不会静默覆盖。
- 数据继续受 data_dir、tenant/project/sensitivity、session、cwd 与大小上限约束。
- `/sessions` 可在当前 scope 内发现最近 journal，`/resume <session-id>` 可重建界面
  投影；具备可信 provider handle 的终态 Codex session 可执行续跑。
- overlay picker 可执行精确 resume、能力驱动 fork 和可逆本地 archive/restore；归档
  标记与 append 共用 journal 锁，并以 compare-and-mark 拒绝检查后发生变化的日志。
- 当前不会在启动时自动选择旧 session。每个 journal 还有有界 `checkpoint.json`，绑定
  精确 byte size、最后 event id/seq 与 projection digest；它只验证 replay 后的一致性，
  不跳过可信 replay，也不授予执行权限。provider 持续状态订阅仍未完成；App Server
  provider archive 仅通过显式命令和 exact capability/handle
  执行。`thread/list` 瞬时摘要与 resume 前 `thread/read` 预对账不冒充持续状态，journal
  仍不是完整聊天归档。

内部 append/finalize 与 fencing 协议见
[`t17-journal.md`](tui-phase0/t17-journal.md)；Picker、fork 与 archive 合同见
[`t19-session-picker.md`](tui-phase0/t19-session-picker.md)；活动 turn 队列与 steer
合同见 [`t20-followups.md`](tui-phase0/t20-followups.md)；Changes scope 与导航合同见
[`t21-changes-navigation.md`](tui-phase0/t21-changes-navigation.md)；Agents Inspector
合同见 [`t22-agents-inspector.md`](tui-phase0/t22-agents-inspector.md)，typed Actor Tree
合同见 [`t34-actor-tree.md`](tui-phase0/t34-actor-tree.md)；Completion
合同见 [`t23-completion-inspector.md`](tui-phase0/t23-completion-inspector.md)；Artifact
合同见 [`t24-artifact-inspector.md`](tui-phase0/t24-artifact-inspector.md)；command output
提升合同见 [`t25-command-artifacts.md`](tui-phase0/t25-command-artifacts.md)；App Server
主 TUI 与 typed approval 合同见
 [`t26-appserver-approvals.md`](tui-phase0/t26-appserver-approvals.md)；动作前 provider
 对账见 [`t27-provider-reconcile.md`](tui-phase0/t27-provider-reconcile.md)，session
 摘要与分页 advisory 合同见
 [`t28-session-summary.md`](tui-phase0/t28-session-summary.md)；Changes 活跃 turn 的有界
异步采样见 [`t30-changes-live.md`](tui-phase0/t30-changes-live.md)；Native Pipeline
observer 见 [`t33-native-pipeline-observer.md`](tui-phase0/t33-native-pipeline-observer.md)；
Actor Tree 见 [`t34-actor-tree.md`](tui-phase0/t34-actor-tree.md)。

## 信任边界

- TUI 不自动批准 pipeline approval，不设置绕过确认的环境变量。
- 不自动 commit、merge、创建 worktree 或调用远程设备；这些能力仍通过显式
  CLI/pipeline 契约启用。
- agent 输出、stderr 和历史都是不可信内容；界面只渲染，不把它们解释成命令。
  Legacy 与 Structured Exec 的控制语义只来自版本化 `RuntimeEvent`，共同经过
  有界 EventBus/Projection 后形成不可变界面快照。LegacyAdapter 将旧 stdout/stderr
  callback 翻译成 typed event；raw-event 路径抑制已被事件覆盖的 stdout/item
  callback，独立 status/stderr 运营诊断仍显示，不可替换 callback 的 backend
  保留文本兼容回退。
- provider `file.changed` 只表示观察到的路径；本轮“workspace changed”提示采用
  backend 在 turn 边界捕获且带 `extensions.authoritative=true` 的 Git delta，
  不用正文或 provider 自报替代。
- 已接受的取消、超时和退出必须恢复终端，并清理当前 agent 进程组；拒绝取消不能
  被伪报为 `INTERRUPTED`。
- 运行时数据继续遵循 `--data-dir` / `PBATCH_DATA_DIR` 和 memory scope，不新建
  TUI 私有的无边界历史库；持久 payload/extensions 做 best-effort 模式脱敏，所有
  item/message/request identity（包括数字 request id）使用统一的固定宽度摘要域，
  疑似含 secret 的 envelope identity 或递归映射键直接拒绝，避免 raw/hash 命名碰撞。
  这不是字段级 secret 保管库：未命中模式的 command/raw 字符串仍可能持久化。

## 发布门禁

首个产品版本至少满足：

- 无参数 TTY、显式 `tui`、pipe stdin、`--help`/`--version` 四类入口互不污染；
- fake agent 验证真实增量输出、稳定 session、失败恢复和取消；
- Textual 无头测试覆盖提交、busy queue/steer、防重入、slash 命令和窄终端；
- journal 回归覆盖 durable-before-visible、可信前缀恢复、歧义只读、身份边界、
  容量耗尽和 session switch 原子性；
- wheel 声明并安装 TUI 运行依赖，fresh install 后可从源码目录之外启动；
- GitHub Release 构建并冒烟测试 Linux/macOS/Windows 单文件二进制，provider CLI 保持外部依赖；
- `ops serve` 的手机会话列表、只读历史、token/TLS 边界和 `--control` 异步指令 API 有回归覆盖；
- 现有完整 `make ci` 保持通过。

Linux 二进制发布额外执行 `tools/tui-soak.py`：在同一个真实 PTY/TUI 进程中反复
运行任务、注入失败/超时/取消，并通过 `/new` 恢复。它核对 journal 与进程/资源证据，
支持 `--cycles 0 --duration N`；provider 为本地确定性模拟，实际运行时间写入报告。
该检查不替代真实 provider 或其他平台的长期验收，操作见 [发布说明](RELEASING.md)。

## 后续版本

- 自动启动恢复、跳过 replay 的 projection state snapshot 加速与 provider 持续订阅；当前
  `checkpoint.json` 已提供精确边界/digest 完整性见证，但不替代 replay。其他 runtime 的
  provider archive/unarchive 仍保持 unsupported。
- Native Pipeline 的跨 stage/task richer actor 控制；当前显式启动/只读 attach、
  run-scoped approval IPC、scoped active-task/cooperative cancel 已接入，仍不发送 provider stop；
- provider/meta Actor Tree 的权限继承与单 actor 控制；当前 trusted child lineage、
  Native meta-role、声明式 App Server provider child 及 retained history groups 已投影到
  `/agents`；`/actor-stop` 与 `/actor-steer` 已接入 exact typed identity dispatch，
  但无 actor-control capability 或 provider 方法时仍 fail closed；
- 统一执行前 plan authorization；当前已有 explicit preflight plan JSON 与 bounded
  `plan_id`/`plan_digest` evidence binding，但仍不授权 commands、validators、gates、budget
  或 remote side effects；
- pi-batch Native/Pipeline approval channel 与 cancellation 的 active-task/恢复边界；
  当前 exact-run channel、active-task kill 和 stage-boundary cooperative cancel 已接通，仍禁止用 stdin
  文本模拟其他域的确认；
- image/PDF/二进制 artifact 专用渲染、Campaign/Isolate 生命周期和设备 Fabric 视图；
- 机器可读 CommandSpec（参数、风险、前置条件、输出 schema），替代现有
  子命令手写表单。
