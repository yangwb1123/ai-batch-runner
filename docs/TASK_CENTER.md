# 全局任务中心：分批实现与使用

目标：一个入口观察所有项目的目标、进度、资源消耗、交付、修复和清场。
执行事实、AI 推测、目标验收和操作授权必须分离。运行数据不进入工具源码 Git。

## 第一批：本机只读事实入口

```bash
# 任意目录均可查询；默认发现 PBATCH_RUNTIME_HOME/projects 下的工作空间。
pi-batch ops tasks
pi-batch ops tasks --json
pi-batch ops tasks --follow --interval 5 --json   # JSONL 快照，Ctrl-C 退出
pi-batch ops tasks --id tc-...                  # 查询当前有界窗口内的任务
pi-batch ops tasks --tui                        # 独立只读终端任务看板

# 自定义 /tmp 等运行目录明确登记一次，不扫描整机，也不重启正在运行的任务。
pi-batch ops sources add my-project \
  --data-dir /path/to/runtime \
  --workspace /path/to/project \
  --campaign-state /path/to/output/state.jsonl \
  --events-file /path/to/events.jsonl
pi-batch ops sources list --json
pi-batch ops tasks --source my-project --json
pi-batch ops sources remove src-...             # 仅移除登记，不删除任务/目录

# 浏览器与手机使用同一 JSON 投影。复用已配置的私有 token 文件。
pi-batch ops serve --global --token-file ~/.config/pbatch/ops-token
# 打开 http://127.0.0.1:8731/?token=<token>；页面每 5 秒刷新数据，不调用模型。
```

`--workspace` 只声明相对产物的定位/读取边界，不授予执行或清理权限。
`--global` 必须带 token，不能与 `--control` 组合。非 loopback 监听仍要求 TLS。
已有 `ops serve` 的工作空间模式、历史会话和控制 API 保持原作用域；默认不会扩大成全局。
远端优先通过 SSH/VPN 使用，不直接暴露 Runner。当前不是多租户 Hub 的授权聚合入口。

### 已提供的事实

- Agent 任务按来源 + run/task 身份分组；新流水线再加入明确的父级 run，重复运行不按标题合并。
- Campaign 按来源 + campaign/module/direction 身份展示历史与最近状态。
- 执行、验证、重试分别可追溯；metering 与 agent ledger 同一尝试去重。
- LLM 配置、调用结束后的输入/输出/缓存 token 与费用，带完整/部分/不可用标记。
- 显式开始事件或报告耗时可定位观察窗口中的时长；排队时间、ETA 暂不猜测。
- 声明产物的存在性和字节数，只检查明确登记的 workspace/runtime 边界，不读取正文。
- 分支/commit/合并记录与目标验收分开；存在文件、执行成功不等于成果已验收。
- 独立显示本机已有进程注册表的 PID 存在性与心跳，不把活动当作有效进展。
- 损坏、超长、未写完或被截断的账本窗口可见；缺失数据不当作零费用。

### 尚未实现，不能误读

旧任务仍是原有账本的只读投影，不会补造历史事件。旧运行没有开始事件时，
只有调用结束后更新的账本事实和独立的进程活动视图；不声称逐 token 实时计费。
`usage.calls` 是有界窗口内终结的 execution 记录数，不是 provider 请求数；
一个 CLI 执行可能调用模型多次，`llm_requests` 在没有相关遥测时明确为未知。
总 token 只采用报告的总量；缓存计入输入还是独立计量依赖 provider，不补造总量。
费用的报告值与币种覆盖分开；币种未追踪时不能据此作跨 provider 的财务比较。
Campaign 子进程若没有可关联身份，不把整个工作空间 token 归给某一个方向。
只有短摘要时不声称分析了完整 prompt；不同项目/角色的文本不能自动成为执行授权。
历史验证未绑定当前文件完整摘要，产物标记为 `not_reverified`，不是实时 Verified。
没有明确契约/检查点的旧任务，目标验收与下一步仍为 `untracked`。实时 ETA、已验收产出成本仍未知。
第三批仅增加受限的历史 CLI 耗时先验，不把它称为真实目标完成时间。

API（只在 `--global` 下开放）：

```text
GET /api/tasks?limit=100&source=<已接入来源 ID 或名称>
GET /api/tasks/<task_id>?source=<已接入来源>
GET /api/goals?source=<已接入来源>
GET /api/goals/<goal_id>?source=<已接入来源>
GET /api/intents?source=<已接入来源>
GET /api/intents/<intent_id>?source=<已接入来源>
```

CLI/Web 共享 `schema_version: 1` 的投影。HTTP 查询不接受文件路径，不暴露原始会话正文。
接口无执行、删除、合并或上传动作；Web 用 textContent 渲染外部数据。

### 数据与读取预算

- source catalog：用户级 runtime 的 `ops/task-sources.json`，0600，锁内原子替换。
- 默认最多 64 个来源；每个 ledger 最多尾部 1 MiB / 512 条，单行最多 256 KiB。
- 单次最多返回 200 个任务、每任务 32 条观察引用、16 个产物候选。
- 进程注册表独立有界读取，不使用会创建探针或删除死记录的读取路径。
- 上述数值均是窗口观测，不是所有历史的全量统计。列表之外的任务不代表已删除。
- 读取不创建 runtime；损坏 catalog 不自动修复或覆盖；符号链接/FIFO 等不作为账本读入。

当前统一目录解析器的默认运行根为 `~/.abr`（目录变更由另一个工作批次实现）。
未显式配置运行根时，本入口还只读兼容已有 `~/.codex/pbatch` 的工作空间与进程登记。
如设置 `PBATCH_RUNTIME_HOME`、`CODEX_HOME` 或 `PBATCH_HOME`，则遵守该明确边界，
不额外读取旧默认位置；需要的旧运行目录通过 `ops sources add` 接入。
本入口不会搬迁旧数据或修改旧进程配置。config/cache/keys 的现有 XDG 路径不在本批迁移。

### 工作空间实例汇总的修复边界

`pi-batch ops instances [--json]` 是另一个**按工作空间**读取收据与 isolate 会话的
入口，不是逐进程任务中心、全机清单或卡死检测器。现在按追加顺序、完整
`session_id` 保留窗口内每个会话的最新状态：同一会话 `RUNNING → MERGED` 不再
误留旧状态，另一会话完成也不会遮蔽仍记录为未完成的会话。兼容的 `session_state`
仅表示最新追加状态；`sessions_observed`、`unfinished_sessions` 是当前窗口引用，
不证明当前进程活性或所有会话已完成。

- 每个账本最多读尾部 1 MiB + 1 个对齐字节；收据最多 500 行、会话最多 200 行，
  单行最多 256 KiB。只有换行完成的严格 UTF-8 JSON 对象可作为记录。
- 截断、未完成尾行、坏 JSON/重复键/非有限值、未知状态/身份和损坏字段保留覆盖
  标记；损坏行打断连续失败序列，不拼接两侧记录来制造“连续失败”。
- 拒绝符号链接、FIFO/特殊文件、硬链接别名、未知/外部 uid，以及读取期间检测到
  的文件变化。只读检查不会创建目录/锁、修复账本、调用 provider 或操作进程。
- 工作空间枚举最多 800 个目录项、展示最多 200 个工作空间；拒绝链接与控制字符
  名称，部分窗口明确可见。遗漏总数未知，`complete_inventory` 始终为 false。
- `receipt_window` / `session_window` 区分缺失、不可用、损坏、截断与已读数据；没有
  可追踪收据时计数为 null / “未追踪”，不是零通过率或安全证明。
- 显式 `--notify` 仍是独立的投递操作。损坏/截断/空/缺失窗口和未观察工作空间
  不会自动解除旧告警；只有两类账本均有完整的可判定记录、且条件消失的工作空间
  才可解除。投递失败不记为已通知。

这些是顺序 stat/字节/记录观察，不是原子跨文件快照、lease、恶意同 uid 隔离或
独立验收；旧无时区时间戳与静默提示也不证明卡死。进程—任务—目标的完整验证
映射、全量实时遥测、真实多设备/Flutter 验收与授权恢复执行仍未完成。

### 注册进程查询的只读修复（`ps`）

`pi-batch ps [--json]` 现在只选择已存在的登记目录，不调用写入端的 mkdir/写探针。
主目录缺失时可选择已知的已有 fallback；已经存在但不安全/不可读的所选目录不
触发扩大范围的 fallback。这个窗口不是全机清单，也不把 PID/心跳认作实际进展。

- 最多 256 个目录项、64 条注册对象，每个对象最多读取 16 KiB + 1 个预算判定字节；
  拒绝链接/特殊文件/硬链接别名、未知/外部 uid、非 UTF-8、重复键、非有限 JSON
  及检测到的文件/所选目录变化。遗漏/拒绝项在 stderr 诊断，JSON 保持列表兼容。
- CLI JSON 和人读表格只投影固定元数据，不再输出 argv、任意 extra/body 或内部
  `_file` 路径；控制字符/C1/bidi 按字面安全显示。未知开始/心跳不补成零秒。
- `registered_status` 是声明状态；`pid_present_or_unknown` 包括保守的权限/未知
  探测结果，不证明原进程身份、lease 或健康。进展未追踪、活性未验证、控制权 false。
- `task_center_activity` 共用完整的目录准入与有界扫描：校验未知/外部 uid、路径
  类型和所选目录前后身份，再投影 PID 元数据；目录消失/替换不发布之前读到的
  行。`coverage` 按已知根窗口给出 `missing/observed/unavailable/not_read`，预算未读
  不是零进程。该数组是本次窗口顺序，不是可用于连接任务/目标的身份映射。
- 主机匹配在显示处理前按原始值严格比较；不同注册路径不凭 PID/主机/开始时间
  合并。活动视图保留旧 `pid_present` 兼容字段，但 `presence_basis` 明确该布尔
  探测包含权限/未知结果，不是确定存在或原身份证明；`liveness` 始终未验证。
- JSON 源字节和解码后的所有键/嵌套值都须可严格 UTF-8 编码；孤立代理字符拒绝，
  合法转义代理对可读。活动标签仍先脱敏，再剔除 C1/bidi；错误不公开元数据正文。
- 未来开始/心跳不钳成 0 秒；`ps` 显示未追踪，活动 `heartbeat_age_seconds=null`，
  `heartbeat_clock=reported_future`。正常差值也只是读取端时钟计算，不是进展或
  同步时钟、健康/卡死证明。未测得心跳是 null，不补当前时间。
- 查询不清理登记，也不操作进程。既有显式 `ps --prune` 维护动作和登记/心跳写入
  路径没有被观察器调用；这不是新增的清场授权、执行映射或恶意同 uid 隔离保证。

### 本地 Web 刷新不保留旧事实

普通定时刷新和手动/强制刷新在发起新的共享 GET **之前**清除任务、目标、活动
及其它记录行、已选详情和旧时间/计数；等待阶段显示“等待新快照”，不把之前的
running/心跳/验收记录冒充本次事实。完整同范围的新响应才按保留的完整记录 ID
重新显示选择；错误、过期响应/错误或当前窗口缺记录不会恢复旧数据。来源选择
仍保留查询条件，不从标题或 PID 推断关联，也不补读详情/历史。

请求版本/来源 fence、AbortController 和 GET-only 行为保留；客户端 abort 不证明
服务端读取或任务已取消。这一修复是显示失效，不是单读器队列/硬实时保证、控制
或独立验收。Node VM 执行交付脚本验证等待、完成、失败、乱序及 literal 标签；
不是 Flutter、真实浏览器/设备、部署或可访问性验收。

## 第二批：目标记录、运行关联与声明下一步

新启动的流水线记录唯一 `pl-...` run ID、完整有界计划、阶段开始/结束与最终执行结果。
检查点保存于 `<data-dir>/tasks/runs/<run_id>.json`，阶段 JSONL 每条携带完整当前计划：
即使 tail 没有启动行，也不靠拼接文本猜剩余 DAG。实际顺序包含执行器的串行阶段边界
和 `from_outputs` 依赖；`planned_ready`/`planned_waiting` 只描述计划，不授权派发。
失败、取消或中断均阻断“声明下一步”。任务执行时长与进程存活、目标完成仍分离。

新 CLI 子任务在 spawn 成功后记录开始，在结束/验证后记录结果与父级来源/run。
尝试事件后台有界写入，不把磁盘等待塞进 Agent 硬超时窗口；关闭流水线和进程退出时
最多等待 0.5 秒排空，满队列/坏磁盘丢观测并告警，绝不改变任务结果。
`agent_ledger.enabled: false` 和 dry-run 不创建这些运行事实。
不同流水线即使共用进程 run ID、prompt 和 task ID，也不会合并或重复计费。
同标题、近似时间不能补造父子关联；未接入来源/窗口之外的父级显示不可解析。

### 显式目标契约与判定记录

```bash
# JSON 契约示例随源码放在 examples/task-goal-contract.json。
pi-batch ops goals define --source my-project --file examples/task-goal-contract.json \
  --confirm --by maintainer --reason "确认目标与验收标准"
# 返回 goal_id=g-... 与 version=1；用 ops tasks --json 找到实际 pl-... run。
pi-batch ops goals bind g-... --run-id pl-... --if-version 1 \
  --by maintainer --reason "这一轮继续同一目标"

# 仅在实际审查后记录；路径须位于已登记 workspace 或 runtime，且为非空普通文件。
pi-batch ops goals attest g-... --criterion regression --decision accepted \
  --evidence reports/regression.json --if-version 2 \
  --by reviewer --reason "已核对复现与门禁证据"
pi-batch ops goals list --json
pi-batch ops goals show g-...
# CLI 与 --global Web 的目标视图相同；HTTP 不开放 define/bind/attest 写入。
```

契约必须为纯 JSON，字段仅 `schema_version`、`goal_id`、`statement`、`criteria`。
目标/标准必须明确；未知字段、坏版本、重复标准、源歧义在写入前拒绝。
后续修订必须显式 `--if-version`；并发冲突/损坏状态不覆盖，不自动修复。
每次修订保留旧契约、run 绑定和判定历史，新契约使旧判定失效，不重置过去的运行引用。
已绑定检查点可直接有界读取，目标不会仅因运行落出最近 200 条任务窗口而消失。

**`operator_attested` 是操作人记录的判定 + 当前证据完整 SHA-256 匹配，不是独立 Verified。**
`--by` 是未认证的署名，不证明真人或其权限；内容含 PASS、文件存在、pipeline succeeded
都不能自动形成目标判定。证据正文不回传；字节改动、删除、路径越界、符号链接或读取
预算不足时，判定不显示为当前匹配。程序没有运行声明的测试，也没有进行语义核验。
因此这里仍不计算单位“真实已验收目标”的成本，不把记录变成发布批准或执行授权。

本批契约是观察与审查记录，**尚未注入执行 prompt 或成为执行器的强制策略**。
同一目标的下一轮需显式绑定实际 run；绑定不会续跑、改预算或重设原始截止时间。

### 第二批数据预算

- 目标记录：用户级 runtime `ops/goals/<goal_id>.json`，0600、锁内原子替换。
- 最多 128 个目标，每目标 32 个标准、64 个 run 绑定、128 次历史变更。
  每个 JSON 最多 128 KiB；满预算拒绝更新，绝不丢弃旧历史来腾空间。
- 每个来源最多 1024 个流水线检查点，计划最多 64 阶段；超出计划标记不完整，停止推导下一步。
- 新尝试/检查点账本最多 4 MiB，保留最近完整行；读取仍受上述 tail 预算约束。
- 判定写入的单个证据最多 16 MiB；查询共用 32 MiB 哈希读取预算，失败读取也计入预算。
  缺失/不可复核保持可见，不把它们计为通过。资源回收/迁移仍未开启。

## 第三批：授权历史线索与检验过的耗时先验（首个切片）

### 本机显式授权，不自动读取会话

```bash
# 把待授权的导出放在已登记的 workspace/runtime 内。
# JSON 形状见 examples/task-prompt-history.json；替换其中的 goal_id 为实际 g-...。
pi-batch ops intents import --source my-project --goal g-... \
  --file /registered/runtime/prompt-history.json --confirm \
  --by maintainer --reason "仅授权此目标、此角色通道的历史投影"
pi-batch ops intents list --source my-project --json
pi-batch ops intents show ih-...
# 后续增量导入必须带当前 --if-version；撤销不删除原始审计与恢复证据。
pi-batch ops intents revoke ih-... --if-version 1 \
  --by maintainer --reason "停止历史投影"
```

授权绑定 source + goal + channel + 当前契约摘要。契约改变即暂停推断，需要显式重新导入授权；
撤销后下一次 CLI/HTTP 查询即不返回线索。HTTP 无 import/revoke 接口，也不能选文件路径。
原始 export、Pi/Codex 会话目录没有被自动扫描或上传。默认支持规范化 JSON 导出；
第三个切片另支持显式选文件/分支的原生**用户文本投影**，不声称导入了完整 session。
被授权的消息私有复制保存在用户级 `ops/intents/ih-....json`，
0600、原子替换；正文不进入源码 Git，不通过 API 整段回传。

- 消息必须有稳定 id、时间、role、完整 text；只追加，不偷偷修订旧文本或重排时序。
- source/goal/channel 分开分析；assistant/system/tool 文本不能升级成用户目标或约束。
  role 与 `history_complete` 是导出方声明，不是独立身份认证或全历史覆盖证明。
- 确定性双语词法提取目标、约束、当前重点、下一步**候选线索**，保留消息完整摘要、
  片段位置/摘要和时间引用；显示片段脱敏并有界，不执行其中任何指令。
- 目标重述/转向提示保留前后引用，标为未确认；不把关键词变化裁定为真实语义漂移。
  下一步候选不覆盖执行器 DAG、StopPolicy、预算或人工确认的契约。
- 元数据最多 32 个历史、每个 128 条消息 / 64 KiB text / 128 KiB JSON；超限拒绝，
  不裁剪后假称读完。变更审计最多 32 次，预留撤销槽和字节空间，不删历史腾预算。
  导入会记录新消息计数；可重复提交有重叠的增量，不能把仅增量包宣称为完整保留历史。

导入耗时、保留正文字节数和每次投影的计算耗时可见。此算法没有模型调用，所以它自己的
`llm_requests=0 / total_tokens=0` 是已知事实，而不是原任务的零消耗。原 Agent 费用仍来自
原计量账本；本地 CPU 的财务价格未追踪，不伪称总成本为零。刷新投影不写新的计费账本。

### 历史耗时先验，不是实时 ETA

新 CLI 尝试附加完整声明 prompt/config 的不可逆 SHA-256 与实际 spawn 标记。
仅在同一来源、阶段和指令配置下选样本，不按任务标题混合、不补造旧数据画像。
至少 5 个训练样本与 3 个按时间后移的检验样本；区间用经验 10%/90% 分位数，滚动后验
覆盖不足 0.8、窗口不完整、存在失败/未计量终结样本时，都保持未知，避免成功样本偏差。
最多取最近 64 个成功样本并显示样本数、检验分母与省略数；已结束的调用不回填“预测”。

CLI/Web 显示 `forecast.duration_range_seconds` 的**历史先验**。仓库内容、共享 session 状态、
实际负载与语义难度没有受到控制；少量历史检验不是统计保证，因此 `remaining_seconds`
和 `eta` 仍为 None。不据此延长超时、重置截止、重试、排名或计算已验收成果性价比。

### 已验证执行映射与有界剩余区间

`ops tasks --json` 顶层 `execution_mapping` 只把已记录的 spawn/终态观测按精确
`run_id/task_id/attempt/phase` 与任务身份配对，并显式区分：

- `verified_spawn_and_outcome`：既有 spawn 证据又有终态；
- `outcome_without_spawn_observation`：有身份与终态但缺 spawn 证据 —— 只计数并保留
  未验证来源，既不当成已验证执行，也不当作 0；
- `spawn_only_active_observations`：有 spawn 无终态 —— 是观测，不是存活证明；
- `legacy_identity_observations`：缺少 run/task 身份的旧记录，单独计数。

窗口不完整（截断/损坏/读取失败）时 `coverage=partial_window` 且 `unknown_not_zero=true`，
计数只是下界。映射不做标题/PID/时间戳推断、不发现新来源、不探测进程，也不代表
所有权、验收或执行授权；refs 有界并显示省略数。

active 任务的 `forecast.prior_remaining_interval_seconds` 只在同时满足：同指纹回测先验
成立、`spawn_observed is True`、`started_at` 已记录且不晚于当前时间时给出，数值为
“历史区间 − 已观测时长”并截断到 0；`remaining_seconds` 与 `eta` 保持 None，
`not_a_live_eta=true`，理由是时钟、存活与未来负载未验证。缺 spawn、缺开始时间或
无回测先验时不给出区间，并写明 `prior_remaining_basis`。

### 第二个切片：目标关联的阶段消耗

`ops tasks`、`ops goals show/list --json` 与认证后的 HTTP/Web 共用 `consumption` 投影：

- 只汇总显式 run 绑定和有界账本内记录的父子关系；同标题、同进程、相近时间不产生归属。
  目标/流水线可包含嵌套流水线的子调用，但循环关系与超过 16 层的关系不用于分配费用。
  自定义子来源仍需登记；source 筛选不会偷偷读取筛选外的项目。
- 汇总先于返回任务数量限制，`--limit 1` 不会把已读取的其他调用消耗丢掉。
  仍然是窗口小计：`lifetime_complete=false`，账本缺失/轮转/损坏不冒充全生命周期计量。
  未关联调用计数、来源覆盖和关系诊断可见；没有子观测是未知消耗，不是免费工作。
- 失败调用、重试、实际声明的 stage/role、执行及验证结果分开显示；验证行不重复携带模型费用。
  Agent/metering/attempt 的同一次观测去重并补齐缺失字段；计量冲突标记诊断，冲突量保持未知，
  不能靠优先账本覆盖冲突获得看似完整的 Token 或费用。
- 保留最多 128 个 stage/role 桶、32 个子任务引用并报告省略数；总窗口小计仍包含省略桶。
  目标契约修订保留过去绑定的消耗。两个目标绑定同一调用时，各自小计为包含式视图，
  标记 `shared_execution_observations`；不可跨目标相加或虚构平均分配费用。
- `billing.currency_totals` 只按**显式报告的币种标签**分组，不默认 USD、不换汇、不跨币种相加。
  旧的无币种数额不变成金融总额；缺失币种/费用的观测数可见。标签与金额仍不是发票核销。
  `comparable_total` 只在本次观测覆盖完整且币种一致时有值，仍不代表全生命周期或已验收性价比。
- CLI 完成观测次数与 provider 请求次数不同。Pi 原始 session 的有 usage 的 assistant 消息记录
  可增量计数为 `assistant_usage_messages`，不声称它是网络请求（不含不可见重试/失败请求）。
  只有显式 `provider_requests_basis=provider_request_events` 的计量才进入报告的 provider 请求小计；
  当前 CLI adapter 未提供完整请求事件时保持未知，绝不把一条 assistant 消息当一次 provider 调用。
- 授权历史导入的已记录本地耗时单独列在 `consumption.monitoring`；确定性导入自身模型消耗已知为零。
  不把原任务消耗清零，不伪造所有轮询的累计 CPU/财务成本。撤销/契约改变不抹掉过去的导入成本。
- 执行成功、gate PASS、操作人判定和证据字节匹配仍不是独立质量验证；
  `independently_accepted_goals` 和已验收成果成本比保持未知，不用于排名或自动修复授权。

### 第三个切片：显式选择原生 Pi/Codex 用户历史

```bash
# 先自行选择并复制需要授权的 JSONL 到已登记 workspace/runtime；程序不扫描 ~/.pi 或 ~/.codex。
# 来源必须登记 workspace；文件头声明的 cwd 必须与该 workspace 完全匹配。
pi-batch ops intents import --source my-project --goal g-... \
  --format pi-session --channel pi_primary --leaf-id <entry-id> \
  --file /registered/runtime/selected-pi.jsonl --confirm \
  --by maintainer --reason "仅授权指定 Pi 分支的用户文本"
pi-batch ops intents import --source my-project --goal g-... \
  --format codex-session --channel codex_primary \
  --file /registered/runtime/selected-codex.jsonl --confirm \
  --by maintainer --reason "仅授权所选 Codex 文件的用户事件"
# 后续同通道导入需要 --if-version；Pi 的新 leaf 必须延续原来已授权的用户消息链。
```

- Pi 仅支持 v3 JSONL 树，必须显式指定终点 entry ID，沿父链选择用户消息。
  不默认选最后一行，不合并其他分支；缺父节点、重复 ID、循环或不支持的版本拒绝。
  分支缩短/切换须另建通道，不能悄悄删除原来确认的目标线索。
- Codex 仅采用 `event_msg/user_message` 用户事件；`response_item` 用户镜像不重复导入，
  assistant/tool/系统事件不变成用户意图。没有受支持用户事件的文件直接拒绝。
- 仅复制完整的受支持用户文本，不复制工具结果、assistant 正文、图片或原始 JSONL。
  用户多模态/附件/未知字段、未知文本块形状直接拒绝，不删掉关键内容后假称读完。
  角色和 cwd 仍是原生文件的声明，不是身份认证或项目归属证明。
- 每文件最多 **2 MiB / 2048 行**，每行 **128 KiB**；UTF-8、重复 JSON 字段、损坏行、
  未写完尾行、超限输入均拒绝，不裁剪、不修复 JSON、不抓取省略历史。
  转换后的消息仍遵守 128 条 / 64 KiB text / 128 KiB 私有状态预算，预留撤销空间。
- 审计保留完整原始字节的 SHA-256/大小、版本化 adapter 元数据、会话/分支摘要及选取/排除计数。
  同通道重导入必须保留**整个先前导入文件的字节前缀**，包括未复制的工具/assistant/元数据；
  重写、重排、轮转或换会话必须另建通道，不能只校验最后一条用户消息而掩盖改动。
- `history_complete=false` 固定表示“选定原生用户文本投影”，并不宣称全历史完整。
  CLI/HTTP 返回相同覆盖/审计与有界脱敏线索，不返回原始正文、路径或原生会话 ID。
  原生文件删除后保留已授权消息与历史审计；撤销立即停止线索投影，既不删除原始文件，
  也不删除私有恢复证据。契约修订仍需重新授权。

此适配仍不调用模型、不执行历史指令、不续跑、不上传、不扩大 HTTP 文件读取边界。

### 第四个切片：长会话的显式用户范围导出与重放

```bash
# 输入仍须自行选择并置于已登记根内。先查当前 goal version；不存在隐式扫描/导入。
pi-batch ops intents export --source my-project --goal g-... --goal-version 1 \
  --format pi-session --channel pi_window --leaf-id <entry-id> \
  --file /registered/runtime/selected-long-session.jsonl \
  --first-user 20 --last-user 40 --output pi-window-20-40.json --confirm \
  --by maintainer --reason "仅导出所选分支第 20 到 40 条完整用户文本"
# Codex 使用 --format codex-session，不接受 --leaf-id；序号针对规范 user_message 事件。
# 返回的 path 在 <data-dir>/exports/intents/ 下，0600；导出本身不产生历史授权。
pi-batch ops intents import --source my-project --goal g-... \
  --file /registered/runtime/exports/intents/pi-window-20-40.json \
  --origin-file /registered/runtime/selected-long-session.jsonl --confirm \
  --by maintainer --reason "另行授权此用户范围及原生文件重放"
```

- 新导出仅显式读取一个原生文件，最多 **32 MiB / 65536 行**，单行 **1 MiB**。
  原有直接原生 import 的 **2 MiB / 2048 行 / 128 KiB 单行**门槛不变。
  两次有界流式读取保留普通文件身份、完整快照 SHA-256 和字节数；Pi 只索引有界的
  ID/父链/角色/序号，不把工具与 assistant 的大正文装成整份历史列表。
  两次读取之间变化、坏 JSON/重复字段/未写完尾行、超限仍拒绝，不采用尾部截断。
- `first-user`/`last-user` 必须显式给出，1 起算、两端包含，针对所选 Pi 分支或 Codex
  规范用户事件。最多选 128 条，保留每条完整文本，仍受 64 KiB text/128 KiB JSON 预算。
  范围外的用户消息明确计为未选取；图片/未知形状只要位于选定范围内就拒绝，不删附件后导入。
- 输出参数只能是简单 `.json` 文件名，不是任意路径；发布到登记的 runtime 私有子目录。
  独占原子发布不覆盖已存在文件，最多 64 个导出；容量满拒绝，不删除旧成果腾位置。
  缺 `--confirm`、坏选择或陈旧目标版本不会读原生文件；导出不创建 intent 或执行授权。
- 导出为严格的 **schema 2**，带范围/源/当前契约摘要、版本化 producer、原生快照证据、
  消息摘要和操作人审计；`history_complete=false` 固定，不把范围包冒充完整 session。
  原 schema 1 导出仍保持原形状，未知字段/坏版本仍拒绝。
- 导入 schema 2 **必须另行指定 `--origin-file`**。程序不根据导出 JSON 内的路径自行打开文件，
  而是从此次明确授权的原生路径重放所选范围，逐字核对消息、身份/范围元数据及快照摘要。
  允许明确指定字节一致的复制件；原文件追加后只复核导出绑定的原始字节前缀。
  文件不稳定、变更、缺失或不能复现时保持拒绝，不靠导出方自称“Verified”通过。
- 同一范围通道只接受同会话/工作空间及**同一个 Pi 终点**的连续或重叠范围，缺口/缩短需另建通道。
  新导入同时复核过去全部原生快照的字节前缀，包括未选中的工具/assistant/元数据。
  不能切换到 schema 1/直接原生模式以绕过范围或前缀约束。消息容量和撤销预留不扩大。
- CLI/HTTP 共用范围覆盖与“仅导入时已重放”的回执，不回传原始路径、终点 ID 或正文。
  后续查询不重新打开原生文件；删除原文件不丢已授权副本，撤销/契约修订仍立即停止线索。
  重放只证明本次复制与字节一致，不认证用户/工作空间标签，不核验语义，不批准执行/发布。

导出命令返回该次本地计算耗时和已知为零的模型调用；导入计时包含重放，仍与原 Agent
消耗分开。导出回执不是所有导出/轮询的完整累计财务账本，未记录的活动或费用保持未知。
### 第五个切片：Web 线索、引用与授权覆盖

认证后的全局 Web 页面现在把同一份 `intents` 投影结构化显示，不新增读取/控制 API：

- 目标/约束/当前重点/下一步按四类展示**词法候选**，附低置信度、状态和脱敏/有界标记。
  可展开消息/片段 SHA-256、字符位置、时间和历史/消息 ID；展开引用只显示已有 JSON，
  不打开原始文件，不触发模型或 provider 请求。
- 用明确绑定的 source/goal 身份显示项目和确认目标，不按任务标题猜归属。
  规范化导出的完整性声明、原生分支投影、用户范围/文件快照覆盖分开标注。
  “导入时重放”与“当前原始文件重新核验”严格区分；重叠回执不求和冒充全历史。
- 可展开导出/导入/撤销审计、未认证署名、重述/转向的前后引用；词法变化仍不裁定语义漂移。
  已记录本地导入耗时/零模型调用和未知费用与原 Agent 消耗分开，不当作全程监测账单。
- 撤销、契约失效或目标不可用时不展示候选线索，历史审计和已记录消耗仍可读。
  开始下一次刷新即清除旧线索，失败或记录落出窗口不保留过去“已授权”的正文。
  这是每 5 秒轮询视图，不是撤销即时推送；服务端撤销在下一次查询生效。
- 切换来源立即清除旧项目视图并取消旧请求。过期响应/错误不能回填到新来源下；
  不再因慢请求结束较晚而显示错项目的目标/历史。所有外部字段以 `textContent` 渲染，
  未认证署名、prompt 里的 HTML 与引用均不是可执行 DOM。

这一切仍只是本机只读 Web 视图；没有 import/export/revoke 按钮、自动执行或任意文件路径访问。
Node VM + 最小 DOM 回归覆盖脚本执行、竞态、过期缓存与文本安全；这不等于真实浏览器
视觉/无障碍/移动端验收，也不代表 Flutter/App 或多租户 Hub 已接入。

第三批仍待：更多原生形状/更大历史的授权适配、语义意图/漂移检验、全生命周期 phase/provider 请求与币种覆盖、
独立验收成果效率、跨任务难度校准与真实剩余时间预测。以上不得由线索或窗口消耗代替。

## 第四批的第一个切片：只读失败检查与恢复预览

`ops tasks`、认证后的 `/api/tasks` 列表/详情和 Web 的“失败 / 恢复预览（只读）”
共用任务上的 `repair_preview`。这是确定性的**检查提案**，不是恢复授权或可执行计划：

- 仅使用本次已准入的任务、生命周期检查点、目标投影及窗口消耗；不新增文件读取，
  不调用模型/provider、Git、进程探测、预算账本或执行器，也不写提案状态。
  诊断文本是已记录、不可信的引用，不按其中的指令操作，也不据此猜根因。
- 完整且无窗口缺口的顺序计划，只有明确记录失败的原阶段才能作为检查候选。
  失败前必须全部记录成功，失败后必须全部待执行；身份不匹配、计划不完整、阶段含糊、
  损坏/截断/未完成尾行或已读取的目标检查点头部与 feed 冲突时，不补造候选。
  取消和中断不自动转成重启候选；执行/验证/Campaign 记录没有阶段计划时只提供检查提示。
- 候选保留原阶段 gate、已记录前置状态及后续声明步骤；旧成功不批准复用，旧 PASS
  不证明当前仓库可接受。每类前置、目标和引用最多显示 8 条、后续步骤最多 8 条，
  附省略计数；后续 gate 总数包含被省略的步骤，不能借显示预算跳过门禁。
  阶段 key 由原 source/run/stage 身份派生，不用可能脱敏重合的显示名称路由。
- 附来源/run、检查点序号/时间和规范化字段摘要，明确这不是原文件字节摘要或新鲜复验。
  目标投影里的已读取检查点只做头部比较，不认证文件内容、存活或 lease。
  返回条数变少不丢原窗口里的活跃子执行告警；来源过滤不跨到其他来源找“同名任务”。
- 目标绑定跨修订保留，用于记忆和消耗归属，**不证明 run 按当前版本契约启动**。
  多个目标不静默选一个；无绑定保持未追踪。目标修订或 `operator_attested` 也不授予恢复权。
  每个失败提案仍需核对：认证执行授权、原目标/执行绑定、持久原截止时间与剩余预算、
  当前进程/lease、工作树归属/Git 基线/脏状态、新鲜检查点/产物证据，以及当前工程、
  合并和独立验收门禁。它们全部标为未检查，不拿窗口消耗推算剩余额度或重置预算。
- budget/provider pause/recovery/merge block 只提示检查已记录阻断，不清除 breaker，
  不自动等待后重试，不合并、不清场、不恢复文件或热改 supervisor。
  所有 `authority` 位固定为 false；HTTP 仍只有原只读接口，没有 repair/resume/retry 控制入口。

回归覆盖取消/中断、门禁/预算阻断、目标修订/歧义、陈旧/损坏/不完整记录、跨来源同名
与阶段脱敏碰撞、显示省略、CLI/HTTP 一致性、文本 Web 详情，以及查询零写入/零子进程。
Node VM 检查不是实际浏览器验收。此切片不等于有预算的真实修复完成；后续执行仍须单独授权，
并保留原任务目标、截止时间、预算和独立门禁。完整 CI、原任务验收和打包发布仍应分别验证。

### 第二个切片：显式、有界的本地恢复请求登记

`ops repairs` 现在可以把检查意向和**声明限制**保存为私有记录，仍没有执行器。
它不是认证授权、原预算证据、可执行计划或恢复/合并回执。

```bash
# 先从只读详情取得原 source/task/run、候选 stage_key、checkpoint 序号/摘要，
# 从目标详情取得 goal ID、当前 version 和 contract_digest。
pi-batch ops tasks --source my-project --id tc-...
pi-batch ops goals show g-...
# 自行填写 examples/task-repair-request.json 的结构，并明确选择一个目标。
# 示例全部 ID/摘要和限制是合成数据，不是可直接登记的真实恢复参数。
# 填好的 JSON 必须位于此 source 已登记的 workspace 或 runtime 根内。
pi-batch ops repairs register --source my-project --file /registered/runtime/request.json \
  --confirm --by maintainer --reason "只登记对此原失败阶段的检查意向"
pi-batch ops repairs list --source my-project --json
pi-batch ops repairs show rq-...
pi-batch ops repairs withdraw rq-... --if-version 1 --by maintainer --reason "撤回登记意向"
```

- manifest 为闭合 schema 1，输入/状态最多 **16 KiB**。重复字段、坏 UTF-8/JSON、
  非有限数、坏身份/版本、未知字段、symlink/FIFO/不稳定文件拒绝；不存在 command、
  自动续跑、放宽门禁、reset 或 force 字段。缺 `--confirm` 先拒绝，不打开 manifest。
- 登记要求已有目标/run 绑定，并精确固定当前目标 version/契约摘要、原 task/run/source、
  checkpoint 序号/规范化字段摘要和原失败阶段 key。登记前及合作式非阻塞锁内再次核对
  当前目标、实际持久检查点和同一只读候选；陈旧/不完整/歧义记录不被登记为新失败。
  这只核对元数据，不证明目标在原执行时已落实，也不验证工作树、进程/lease 或产物语义。
- 必须显式声明带时区的 `requested_deadline_at`、正尝试/耗时/Token/费用上限及三字母币种。
  尝试最多 3、耗时最多 86400 秒、Token 最多 10⁹、费用数值最多 10⁶；过期声明拒绝登记。
  币种是操作者的声明，不做换算。这些**不是原截止时间/剩余预算，也未参与限额执行**，
  `original_limits_verified=false`、`execution_authority=not_granted` 固定。
  真正执行仍需单独核对原有持久限额，取不超过剩余额度的范围，不能用新声明重置原预算。
- 私有 `runtime-home/ops/repair-requests/rq-....json` 为 0600，最多 64 个请求；容量满拒绝，
  不删历史腾位置。相同 source/run/goal/契约/checkpoint/stage 是同一请求 anchor；完全相同
  的重登是 no-op，不刷新时间。不能换署名、重写/提高限制或借重登擦掉审计。
  状态只允许登记 v1 → 撤回 v2；撤回永久保留审计，不能重新激活。不要求旧来源、目标或
  checkpoint 仍可用才能撤回；过期/陈旧请求仍可撤回。坏存储只报错，不自修复。
- `snapshot.repairs`、任务/目标的 `repair_links`、CLI 和认证后的 `/api/repairs[/rq-...]`
  共用投影。query 只读有界请求和已准入事实；不另开原 checkpoint 文件、不跟随 manifest
  路径、不重新登记。失效目标、窗口缺口、检查点变化/头部冲突、过期和撤回分开标记；
  列表截成 1 个任务也不把原已准入的目标失败当作不存在。
- Web 仅显示声明、状态和文本详情，不提供登记/撤回/执行按钮。刷新先清旧请求详情，
  失败/缺失/切换来源不保留旧状态；外部字段只用 `textContent`。全局 HTTP 仍只读且需
  原 token 认证，默认 workspace server 不新增访问；所有变更 POST 仍拒绝。

这不是第四批真实修复完成：没有发起 Agent、修改源码/工作树、复位 breaker、续跑、合并、
清场、上传或改变正在运行的 supervisor。审计 label 未认证，登记收据仅说明登记时核对过
这些字节字段；后续“匹配”只是当前有界观察，不是新鲜文件复验、授权或可执行状态。

### 第三个切片：登记请求的只读就绪检查（不是实时预检）

```bash
pi-batch ops repairs check rq-... --source my-project --json
# 存在但被阻断/已撤回/声明过期：exit 3；不在已接入有界视图：exit 1。
# exit 3 不是启动失败或预算重置指令；此命令不提供允许执行的 exit 0。
```

`repairs[].assessment` 是 `repair-request-inspection-v1` 确定性报告，CLI `check`、
认证后的请求详情 API、任务快照和 Web 的“只读检查 / 阻塞”显示同一份投影。
没有新的 HTTP 检查/执行 action，POST 仍拒绝。报告只使用原本已准入的请求、
目标投影、生命周期窗口和同一个消费索引；不再次读 feed/检查点/产物、不探测 Git、
PID/lease 或预算账本，不写请求、不调用模型/provider/执行器。

- 元数据逐项标为 `observed_match`、`observed_conflict` 或 `unavailable`，不用 PASS 表达。
  分别比较请求登记/声明截止、目标当前版本与保留的 run 绑定、原 source/run/task 身份、
  检查点序号/规范化字段摘要、窗口完整性、唯一原失败阶段、已读取的目标检查点头部，
  以及登记时的计划摘要/gate 字段。显示名称不参与路由；头部匹配仍不证明内容或存活。
  声明截止匹配不证明原截止时间或剩余预算，也不是限额执行。
- 子执行沿已经准入的显式跨来源父链展开，不按同名 task/run/标题匹配。
  所有已选执行记录里的 running/queued/retrying 行都作为**活动观测**显示，旧行也可能已过时；
  非零值阻断候选，零值不证明空闲、进程已退出或 lease 已释放。源过滤不去读被过滤来源。
  来源内无法归属/循环/深度受限/缺祖先的记录单独计数并保守阻断，但不分配给此请求。
  引用最多 8 条，附省略数，计数不受返回任务条数或引用显示上限影响。
- 撤回/声明过期为 `inactive`，其余为 `blocked`。陈旧、缺失、歧义、窗口不完整、已知头部
  冲突、登记收据冲突或子执行/谱系风险都不给阶段候选；仍保留各项冲突及缺证原因。
  只有全部元数据项匹配时才显示**检查用**原失败阶段，旧成功不获准复用，剩余 gate 总数
  包括展示上限之外的步骤。
- 即使元数据全部匹配，`ready_to_execute`、`grants_execution`、`live_preflight_performed`
  仍固定 false。七项认证授权/原契约启动绑定/持久原限额/进程与 lease/工作树归属与 Git/
  新鲜产物/当前工程合并独立验收，全部为 `required_not_evaluated`；原截止与剩余预算为
  `null`。目标修订、操作人 attestation 或旧 PASS 不清除这些条件。

Web 只显示阻塞与文本详情，下一次刷新先清旧报告，失联/缺记录不能保留旧结果。
Node VM 回归不是实际浏览器验收。真正执行仍未启用；此报告不能被作为恢复授权、
自动重试/清场/上传/合并或预算复位的凭证。

### 第四个切片：显式一次性的工作区 Git 诊断

```bash
# 期望 SHA 必须由操作者明确提供完整的小写 40/64 位 OID；它不是原运行基线的证明。
pi-batch ops repairs inspect-workspace rq-... --confirm --expected-head FULL_OID --json
# linked worktree 还必须明确允许读取对应的绝对 Git common-dir：
pi-batch ops repairs inspect-workspace rq-... --confirm --expected-head FULL_OID \
  --common-dir /explicit/repository/.git --source my-project --json
```

这是 `repair-workspace-inspection-v1` **一次性、本地诊断**，不是完整实时恢复预检。
必须先有未撤回/未过期、元数据匹配且无已知子执行/谱系缺口的登记请求；缺 `--confirm`、
坏 OID 或非绝对 common-dir 在读取请求前拒绝。只检查来源登记的 workspace，不接收任意
workspace 路径，也不猜 isolated Campaign 真正用了哪个工作树。不存在保存回执、授权、
执行、retry、force、merge、清场或 reset 选项；有报告仍返回 **exit 3**，输入错误 exit 2。

- 普通 `tasks`、`repairs list/show/check`、HTTP/Web 的查询/轮询**不运行 Git**，不自动变成
  工作区探测。HTTP 没有此 action；此命令的 JSON 不自动落账本或回流 Web，也不是导入授权。
- symlink 路径、非普通元数据、错误 linked-worktree common-dir/backlink、非当前有效用户的
  OS uid、不可读子树、锁/操作 marker、外部对象 stores、危险/不支持的 Git config 均拒绝。
  uid 匹配只证明目录/元数据的 OS 归属，**不认证操作者或确认独占执行权**。元数据最多
  2048 个枚举项、config 每文件 64 KiB、HEAD/pointer 4 KiB；超限不是空/干净目录。
- 只运行固定的 Git argv（无 shell），用剥离继承环境的配置、关闭可选锁/懒 fetch、外部
  fsmonitor、hooks、attributes/excludes 文件及网络协议。为避免仓库 config 在诊断中变更
  后触发 executable filter，Git 使用临时私有的最小控制目录：config/HEAD/index 为有界
  副本（index 最多 1 MiB），只引用已明确准入的 objects/refs。临时目录 0700、文件 0600，
  位于被检查 workspace/Git scope 之外，结束只清除此命令自己创建的副本。它**不是 OS
  sandbox 或原仓库配置语义的完整复制**；filter、info-exclude 等未继承，观察结果不能
  当作原配置下的内容验收。
- 固定 **15 秒共享的 Git 诊断截止**，各命令 stdout/stderr 各最多 64 KiB；硬超时/进程组
  回收沿用公共执行器。Git/解析失败、截断、非 UTF-8 输出、目录/元数据/请求/来源/目标
  改变时不给有效观察。原代码、原 index、原 config 与失败改动不被修复、提交或恢复。
- 报告两次采样的 HEAD/branch 与状态、tracked/untracked/ignored/conflict 计数、gitlink
  数、最多 8 条脱敏显示路径及省略数。路径文本不用作路由/文件证据；ignored matching
  entry 不代表递归文件数量。改名含第二个 NUL path，不能误当另一条状态记录。submodule
  不递归检查，存在 gitlink 单独阻断；ignored 证据也不获准删除。
- HEAD 与期望不一致、detached、Git 变更/未跟踪/ignored 数据和操作 marker 明确阻断并
  保留现场。两次样本相同与 metadata stat 摘要相同，仍**不是原子时间点、全文件字节、
  活进程/lease、原 run-workspace 绑定或恢复授权**。

即使两次样本无报告变更，`original_git_baseline=null`、`ready_to_execute=false`、
`grants_execution=false`、`live_preflight_performed=false`，七项真正执行前提仍未满足。
工作区、原请求和原预算不写入；没有重启 supervisor、模型/provider、上传、合并或清场。
回归只对临时自有仓库/worktree 执行，不探测受保护的实际 campaign 工作区。

## 第五批的第一个切片：显式资源登记与保全计划（没有删除器）

```bash
# 只接受已接入 source 的 runtime 内、已存在的绝对后代路径，不登记源码 workspace 或整棵 runtime。
pi-batch ops retention register --source my-project --path /registered/runtime/rejected/report.md \
  --kind file --confirm --by maintainer --reason "保留失败证据"
# kind 可选 file / directory / worktree；后两项只声明目录性质，不自动跑 Git 或扫描目录内容。
# 可加 --run-id pl-...，但必须有该来源的实际匹配 checkpoint，不能按名字制造关联。
pi-batch ops retention list --source my-project --json
pi-batch ops retention show rr-...
pi-batch ops retention extend rr-... --if-version 1 --until 2030-01-01T00:00:00Z \
  --by maintainer --reason "延长人工复核窗口"
pi-batch ops retention pin rr-... --if-version 2 --by maintainer --reason "永久保护，暂不处置"
```

`registered-resource-preservation-v1` 是**显式登记资源的保全投影**，不是磁盘全量盘点或
垃圾判定。没有隐式导入 `worktrees/`、目录爬取、迁移、归档、`unpin`、删除、force 或
72 小时自动清场，也不复现 `docs/RUNTIME_RETENTION.md` 所记的历史人工处置。

- 登记前需要 `--confirm`、明确 kind、署名与原因；没有确认时不读取来源或资源。
  只允许已登记 runtime 的严格后代，拒绝根目录、registered workspace 重叠、自己的
  retention 元数据、符号链接/FIFO/非普通对象、uid 冲突。目录 uid 只是一项 OS stat
  字段，不认证操作者或独占归属。登记前及合作式非阻塞目录锁内复核来源与目标 stat；
  optional run 只绑定实际 checkpoint 元数据，不授执行或清场权。
- 首次登记把**复核日期**固定为登记时间 +72 小时，不拿旧文件 mtime/体积当安全删除
  判据。相同登记是 no-op，不刷新时间；性质/run/stat/来源/最初署名原因不能重写。
  资源换 inode、同尺寸改写或来源被换定义时，不把新对象收养为旧对象。
- `extend` 必须带准确 version，日期带时区且严格晚于当前时间及原复核日期，只能延长。
  它不修改 execution deadline、预算、breaker、checkpoint 或 lease。`pin` 是永久保护，
  不提供撤除入口；资源/旧来源已丢失仍可保护私有登记。重复 pin 不增加审计或刷新日期。
- `runtime-home/ops/retention/rr-....json` 为 0600，闭合 schema 1，每条最多 32 KiB、
  最多 64 资源/16 条 audit。为最终 pin 预留最后一个 audit 槽和 4096 字节，不删历史
  腾空间。重复字段、未知 delete/command/force 字段、非有限数、损坏/不稳定/超限状态
  fail closed，不自修复。登记路径包含关系或硬链接/相同已记载物理身份拒绝扩大范围。
- `snapshot.resources`、pipeline/goal 的 `resource_links`、CLI 与认证后的
  `/api/resources[/rr-...]` 使用同一投影。只有选中的已接入来源才 stat 目标，不爬被
  过滤来源。queries 不开资源正文、不枚举目录内容、不创建锁/元数据、不执行 Git、
  模型/provider、归档或子进程；署名/路径/诊断按文本脱敏显示，路径显示不能作路由 key。
- availability 明确为 **stat-only**；相同 stat 不证明内容字节、恢复包或独占归属。
  普通文件的当前 stat 字节数可以显示，目录总量未知，不求和伪装可回收容量。
  `content_sha256=null`、`archive_verified=false`、`estimated_reclaimable_bytes=null` 固定。
- explicit run 的引用只来自已准入 pipeline/子执行、跨修订保留的 goal binding 与登记
  repair 请求。子执行与引用最多显示 8 条、保留完整计数/省略量，不受返回任务条数
  影响；partial、缺 run、无法归属/循环谱系、活动观测均保守标记。零活动行不是 idle
  或 lease 已释放；其他未知引用仍为 unknown，不按时间/标题归属，也不去扫描外部来源。
- 无论未到期、复核到期、永久 pin、资源变更/丢失或已记载旧 PASS，plan 固定
  `preserve`。到期只表示需要复核，不意味着应删除；工作树/目录内容与 Git 状态未核验
  时单独阻断。认证处置授权、独占归属/活 lease、完整引用/可续跑性、未保存成果与
  Git 集成、独立验证的可恢复归档、原子新鲜复核及正式处置事务，七项均未求证。
- Web 只有“保留计划（只读）”详情，不提供保护变更、归档、删除按钮。刷新先清旧资源
  表/已选详情，失联、缺记录、切换来源或旧请求晚到不会复用旧计划。HTTP 的默认
  workspace 边界、token 与只读 POST 拒绝不变，query 参数也不能登记任意路径。

这些是**保全元数据与复核计划**，不是安全清场完成。测试只使用临时自有资源，未登记、
探测或清理实际 campaign/worktree；Node VM 测试不是实际浏览器/移动端视觉验收。
只有以后独立完成归属、引用与保全验证并获得处置授权，才可能增加资源回收事务。

### 第五批第二个切片：单文件私有保全与显式字节复核（不是完整归档）

```bash
pi-batch ops retention preserve rr-... --if-version 1 --confirm \
  --by maintainer --reason "保全这个普通文件的当前失败证据"
pi-batch ops retention verify-copy pp-... --confirm
```

`preserve` 显式读取一个已登记普通文件的**当前**完整字节，登记的原 inode/device/uid/mode、
runtime 根身份及来源定义必须仍一致；版本必须准确。只允许 file，不遍历 directory/worktree，
不访问 Git、provider 或命令。缺确认即不读资源；不把换 inode 的新对象收养为旧对象。
同一物理文件后续内容改变时可以再次明确保存当前快照，但不会重写原登记、复核日期、
永久保护、原 deadline/budget、checkpoint、lease 或以前的副本。

- 单文件最多 16 MiB，流式读取、前后 fd/path stat 复核，空文件/二进制可保全，不解码
  或执行正文。超限/增长/竞态拒绝，绝不截断保存成“完整证据”。这些 stat 窗口不是 OS
  原子快照或恶意同 uid 写入者的隔离；普通文件系统读取也不冒称严格硬实时 I/O。
- 副本位于 `runtime-home/preserved/resources/pp-....blob`，收据 `pp-....json`；存储必须
  在该 registered runtime 和 workspace **之外**，自己的保全目录也不能作为输入。
  根目录 0700、文件 0600。合作式非阻塞 catalog/publication 锁只保护本次 metadata
  发布，不认证操作者、证明独占资源或释放 lease。
- 保存前后再核对资源 version/来源/当前源 stat；payload fsync 后通过**独占 hardlink**
  发布，receipt 最后 fsync/独占发布，再完整读回 payload 并比较字节数与 SHA-256。
  不使用 rename 覆盖已有证据。收据固定 source/resource/version、当前 stat、完整摘要
  与 unauthenticated actor/reason；闭合 schema 1，最多 8 KiB/64 收据。不删除旧证据腾空间。
- 相同 snapshot 是无时间续期的 no-op，署名原因不可重写；若旧副本损坏、不可读或缺失，
  重复保存也不会修复/覆盖它。receipt 发布或读回失败会保留已发布 blob/receipt 供诊断；
  未完成 blob 也不按 orphan 自动回收。临时对象只在仍匹配本命令创建的 inode 时清理，
  同名已存在对象和其他进程替换的对象保留。
- `verify-copy --confirm` 只显式读取按 typed `pp-...` ID 定位的私有 receipt/payload，
  全量摘要匹配返回 `recorded_copy_bytes_matching`。即使原资源或 source 已丢失，也可复核
  保存的字节；它不重开原资源，不替代原 workspace 的完整可恢复性验证。
  `current_source_checked=false`、`recoverable_archive_verified=false`、execution/cleanup
  权限仍为 false/not_granted。返回 0 只代表复制/字节复核成功，不是处置准入。
- CLI/HTTP/Web resource 详情共享**仅收据元数据**的投影，最多显示 8 条并保留全部
  observed/omitted 计数，不在 list/show/刷新中重读 source/payload 或隐式验证。
  `current_payload_reverified=false`；baseline `archive_verified=false`、plan `preserve`、
  所有七项处置检查仍未求证。HTTP 不提供 preserve/verify-copy/正文下载入口，query
  参数不能触发这些动作；Web 只有文本详情，不增加按钮。

字节保全并不包含目录树、Git refs/index/未合并提交、lease、执行上下文、认证归属或
恢复演练，因此不是工作树完整归档，更不是删除许可。复制/复核不调用模型，但存储、
I/O、CPU 的价格及 lifetime 成本仍未知；不把副本收据计作独立验收或交付效率。

### 第五批第三个切片：明确选择的有界目录字节副本（不是 worktree/数据库恢复）

```bash
# 仅接受已登记为 directory 的 runtime 后代；原有 preserve 仍只接受 file。
pi-batch ops retention preserve-directory rr-... --if-version 1 --confirm \
  --by maintainer --reason "保存这个明确选定目录的当前字节样本"
pi-batch ops retention verify-copy pp-... --confirm
```

`bounded-directory-bytes-v1` 在确认、准确资源版本、原目录 inode/device/uid/mode、来源定义
和根身份复核后，保存一个完整的**有界采样窗口**；超限不取子集、不省略或截断成员。
它不自动发现目录，不接受 workspace/runtime 根、kind worktree 或含 `.git` 的目录；
不读取外部 Git common-dir、refs、lease 或执行历史，也不启动 Git、provider/子进程。

- 最多 **256 项（含选中根和每个空目录）**、16 层、相对名严格 UTF-8 最多 99 字节；
  拒绝绝对/不规范/traversal/backslash/控制及 bidi 字符、`.git`、链接/FIFO/特殊类型、
  hard-link alias、重复物理身份、不同 device、uid 不匹配和 setuid/setgid/sticky 模式。
  字节数据和可执行位保留，不把文本中的命令/URL当指令。uid/device 样本不是独占归属、
  mount/lease 或恶意同 uid 写入者隔离的证明。
- 完整源文件字节及整个无压缩 **USTAR 容器（含 headers、padding）**各受 16 MiB 预算。
  逐目录 fd/path stat 与全部名字清单有界采样；每个文件完整读前后复核，结束重新枚举
  比较全部名字/stat，再复核资源/来源。可见变化、读失败、超限、锁冲突均不给收据。
  这些是顺序的稳定性样本，**不是原子跨文件或应用/数据库一致性快照、硬实时 I/O**。
- 使用已有独立私有保全目录和不覆盖的 payload-first/receipt-last 发布；fsync 后完整
  读回容器。新**私有 receipt schema 2**增加 `directory_copy`：format、完整项/文件/
  目录数、内容字节和 canonical member SHA-256；身份还绑定完整容器摘要及根 stat。
  root-relative names/正文只在 0600 `.blob` 内，public 只显示计数与摘要，不暴露成员名。
  USTAR 为可解析的字节容器，不含链接、扩展、脚本或压缩；uid/gid/mtime 归零，不冒充
  原 owner、时间线或可恢复执行上下文。旧 schema 1 收据不迁移、不重写；两种共享原
  64 收据/8 KiB metadata 预算，损坏旧证据和孤立 payload 保留，不删来腾空间。
- `verify-copy --confirm` 对目录副本还严格检查每个 USTAR header、相对名/parent/order、
  类型/模式、完整 body、零 padding/结束块、全成员摘要与全部计数；global hash 匹配
  不足以容忍未知/损坏容器。只读保存的 payload，不重开原目录，**不 extract/还原**。
  缺失、篡改或 verification 竞态拒绝，不修复旧副本。原 source 已丢失仍可显式复核。
- 普通 CLI/TUI/认证 HTTP/Web 继续只读收据 metadata；不枚举目录、不重验容器、不提供
  HTTP preserve-directory/restore/download。`current_payload_reverified=false`、
  `archive_verified=false`、plan `preserve` 和七项未求证处置检查不变。签名原因仍为
  unauthenticated label；相同字节副本 no-op，不重置日期、预算、deadline 或保护。

成功只表示保存/核对了这些被明确选中字节的完整有界样本，不是独立验收或完整恢复演练。
Git 外部对象、原权限/owner 语义、活动 lease、跨文件一致性、未保存成果的集成与正式
处置授权仍须分别求证；没有还原、删除、worktree 清场、上传或自动升级权。

## 第六批第一个切片：本地最小合成反馈草案（不执行、不上传）

```bash
# 文件必须显式选择，且处于通过 ops sources add 登记的 workspace/runtime 内。
# examples/task-feedback-synthetic-case.json 只是格式示例，不是真实缺陷或复现结果。
pi-batch ops feedback draft --source my-project --file cases/synthetic-case.json \
  --confirm --by maintainer --reason "私下记录这个合成边界，等待独立复现"
pi-batch ops feedback list --source my-project --json
pi-batch ops feedback show fd-...
pi-batch ops feedback withdraw fd-... --if-version 1 \
  --by maintainer --reason "永久撤回这个本地草案，不删除证据"
```

`local-synthetic-feedback-v1` 只登记一个**操作人声明为 synthetic 的纯 JSON 案例**。
没有自动发现/复制 Pi、Codex、session、native history 或 runtime 目录；默认发现的 source
也不足以授予案例读取权限，须先明确登记。`--confirm` 缺失时不读取来源或文件。
synthetic 与署名都是未认证声明，不是独立证明、无隐私保证，更不是允许外发。

- 输入闭合 schema 1：`case_id`、固定 `case_type: operator_declared_synthetic`、`summary`、
  1–8 个 `inputs: [{name, content}]`、`expected`、`observed`、`hypothesis`。名字是有界
  简单标识符，不是文件路径；文本均为**不可信引用**，即使写有命令/URL/用户角色也不执行、
  请求网络或获得指令权。未知 command/upload/force 等字段、重复 JSON 字段、NaN/Inf、
  非 UTF-8、坏 JSON/多条 JSONL 和 unsupported shape 一律拒绝，不进行 JSON salvage。
- 整个原输入最多 64 KiB，合计文本最多 16 KiB，每个文本/片段最多 4096 字符；不截断或
  丢弃片段来使案例通过。只允许登记根内普通文件，拒绝符号链接/FIFO/目录、非当前有效
  用户 uid 和 hard-link alias。uid 匹配不认证操作者或建立独占归属。
- 私有目录 `runtime-home/ops/feedback/` 为 0700；完整原 JSON 字节位于 `fd-....case`，
  仅摘要、来源/内容摘要、原输入 proof 与审计位于 `fd-....json`（均 0600）。存储不能与
  任意明确登记的 runtime/workspace 重叠，也不能导入自己的元数据/副本。32 个草案、
  metadata 每条最多 16 KiB，预留 4096 字节用于 withdrawal；目录枚举独立有界。
- ID 按完整 `source_id + case_id` 键控，不按标题或脱敏摘要归并。来源定义、原输入路径与
  字节/hash、case 摘要、初始署名/原因不可重写；相同登记 no-op，不刷新审计/时间。同一
  case ID 的不同内容或原文件不会被收养为旧草案，要明确选用新的 ID。
- 在合作式**非阻塞** source-catalog/publication 锁内重新检查来源与输入 stat；完整副本
  fsync/独占 hardlink 发布后读回验证，metadata 最后独占发布，不覆盖历史。原输入变动、
  损坏旧副本、容量/枚举预算耗尽都拒绝；已经发布但没有 metadata 的 orphan 仍保留，
  不自动删来腾空间。只清理仍匹配本命令创建 inode 的临时文件。这不是 OS sandbox、
  恶意同 uid 写入者隔离或原子文件系统快照，也不宣称硬实时 I/O。
- `withdraw --if-version` 只把 v1 draft 变为永久 withdrawn v2，保留完整副本与审计，
  不提供 reactivation。原 source/输入/copy 已丢失或容量已满仍可撤回有效 metadata；
  腐坏 metadata 则拒绝，不自修复。重复撤回是 no-op，不新增 audit 或更新时间。
- `snapshot.feedback`、CLI 和认证后的只读 `/api/feedback[/fd-...]` 共享同一元数据投影。
  查询不重开原输入或 `.case` 正文，不隐式重新验证复制内容，不创建目录/锁/草案，不运行
  命令、Git、provider 或网络上报。原路径、输入、期望/观察正文、根因假设正文不回传。
  只显示脱敏有界摘要；withdrawn 或当前 source 定义不匹配时摘要为 null，审计仍保留。
  复制 hash/时间记录只证明登记时的字节绑定，不证明当前副本、根因或独立复现。
- 独立复现、根因核验、修复、回归、经审查发布与原任务验证**逐项分开**，本切片分别标为
  `not_performed` / `unverified_hypothesis`。登记成功不代表案例已复现、任务通过、工具
  已修好或交付效率提升；不把私有原 runtime、任意历史或案例正文直接提升为正式测试。
- Web 只有草案元数据详情，没有复制正文、执行、撤回、上传、rule promotion、release、
  upgrade 或清理按钮。刷新先移除旧草案及已选详情；失联/缺记录/切换来源/旧响应或错误
  晚到不能恢复旧摘要。HTTP 的 token、workspace 默认边界和 POST 拒绝保持不变，query
  参数不授予导入、复核、上传或正文下载能力。

后续须另外明确授权最小化案例的独立复现、隔离修复/回归、审查/发布/可回滚升级及原任务
复验；本切片没有自动 learn promotion、二进制更新或 live repair。测试只用自有合成文件；
Node VM 结果不是实际浏览器、移动端、跨租户授权或真实 provider 的验收。

### 第六批第二个切片：反馈副本显式字节复核与内容元数据匹配

```bash
pi-batch ops feedback verify-copy fd-... --confirm
pi-batch ops feedback list --source my-project --json
```

`verify-copy` 是一次性的**已保存副本字节检查**，不是案例复现。无确认时不读 metadata
或副本；只接受 typed `fd-...`，不接受路径/URL/原文件参数。读取私有 metadata 与一个
`.case` 的完整字节（仍最多 64 KiB），验证严格 JSON、输入字节数/SHA-256、闭合案例
摘要及新 schema 的内容身份，随后重新读取 metadata；若 withdrawal/记录变更则不返回
匹配结果。文件不稳定、损坏、缺失、非私有、symlink/hard-link/FIFO/uid 冲突都拒绝，
不截断、不修复旧证据，不创建锁/收据/目录，也不执行命令、模型或网络访问。

- 可在原输入/source 丢失或永久撤回后明确检查自己的私有副本，不重开原路径或爬取
  已删来源。返回 `recorded_copy_bytes_matching`/exit 0 **只说明完整副本与已记载字节
  和案例摘要匹配**，不是 metadata 被认证、原对象仍存在、可恢复归档或复现成功。
  `current_source_checked=false`、`stores_receipt=false`、独立复现 `not_performed`，
  根因 `unverified_hypothesis`，执行/清场/上传/发布权限仍不授予。结果仅此刻的有界
  stat/metadata 样本，不是文件系统原子快照或恶意同 uid 写入者隔离。
- 新登记的**私有 metadata schema 2** 增加 `case_content_sha256`；输入案例 schema
  仍是 1，public 投影也仍是 1。内容身份保留完整 summary、inputs 名字/文本/**顺序**、
  expected/observed/hypothesis 与所有其它闭合案例字段，**只排除 case_id 标签**；使用
  canonical JSON（对象 key 排序），不改空白、大小写、Unicode 或 input 顺序。
  不按脱敏标题/署名/时间接近度/词义猜测“同一问题”。
- schema 1 的旧 metadata 继续验证、查询、幂等登记与永久 withdrawal；不会为了增加
  content hash 迁移、补写或重新盖章。显式字节检查可计算一个仅本次返回的内容摘要，
  **也不回写旧 metadata**；普通查询仍给出 legacy content identity 未追踪，不能把
  同标题或 case 全摘要当作相同完整内容。
- `duplicate_inspection` 是纯 metadata 投影：仅将**同完整 source_id + 当前相同
  source definition**、未撤回且内容身份已记录的草案作匹配候选。不同 case_id 仍是
  两条独立不可重写草案；不合并、覆盖、撤回、重命名、复用原成功或提升为已复现事故。
  stale/withdrawn 不加入候选，legacy 不猜 hash。零候选只是“窗口内没有观察到其它
  匹配 metadata”，不是唯一性、全部历史覆盖或无重复问题的证明。
- 保留窗口中全部候选计数，最多显示 8 个按完整 typed ID 排序的 peer references，
  另记遗漏数量。坏记录、枚举预算或记录数限制显式标为 incomplete；未追踪内容的
  记录数也保留。`current_copies_reverified=false`、语义比较 `not_performed`、根因
  等价 `not_verified`、merge/reuse 权限 `not_granted`。即使副本已损坏或丢失，查询
  也只显示“记载的元数据候选”，不会偷偷读取 `.case` 来认证它或重建旧记录。
- CLI/HTTP/tasks/Web 显示同一候选投影，不受任务显示 limit 影响，不跨过滤 source
  暴露匹配关系。Web 把候选数与“非同根因、非完整去重；副本未复核”一起显示；stale、
  withdrawn 或 client 状态不一致时删除旧摘要/peer 关系。刷新、切换来源、迟到请求
  的既有失效规则保持不变。
- 没有 HTTP/Web 字节检查或正文下载能力；GET query 的 `verify-copy/confirm` 不授
  能力，POST 仍拒绝。普通 list/show/refresh 仍不读原文件、副本正文，不创建状态，
  不跑 Git/provider/命令。显式检查不形成长期“已验证” badge，不计作独立验收或
  根因去重成果；I/O、CPU 价格与 lifetime 消耗未追踪，不能把这些费用记成零。

## 第七批第一个切片：独立只读终端任务看板

```bash
pi-batch ops tasks --tui
pi-batch ops tasks --tui --source my-project --limit 100 --interval 5
pi-batch ops tasks --tui --source src-... --id tc-...
```

这是新的本地观察入口，**不是** Agent 交互 TUI、Hub、多设备调度器或 Native Pipeline
控制台。现有那些控制器与会话不变；看板不导入它们或调用其恢复/执行/取消动作。

- 与 CLI/Web 使用同一 `task_center.snapshot(limit, source)`，展示任务、目标、已授权
  意图线索、恢复请求、保留资源与反馈草案六类 public 记录；详情不再次查询原文件或
  私有 payload。既有快照的准入、窗口、归属、计费、证据与保全限制原样保留。
- 上方来源输入框按 Enter 后才应用：仅是原 CLI 的登记 ID/名称过滤，不是文件路径、
  URL、命令、tenant/instance grant。未知名称/路径式文本不能变成任意目录读取能力。
  分类下拉框只是本次快照的显示过滤，不新增读取。表格 Enter/点击选择详情，按
  `r`/`Ctrl-R` 刷新，`q`/`Ctrl-Q`/`Ctrl-C` 退出看板；输入框中的字母不触发快捷动作。
- 用完整 typed ID 及记录类别路由，不能用标题、脱敏显示、PID/时间邻近度合并记录。
  不接收歧义/无来源/重复身份或超预算 public 列表；筛选来源与快照冲突则清除事实。
  `--id` 必须是完整任务 ID；不在窗口只是不可见，不代表完成、删除或仍有授权。
- 任务详情先显示上一/当前观测阶段、声明下一步、墙钟耗时、报告 token/明确币种费用、
  产物存在/声明数，再显示同一 JSON 事实；没有实时 ETA、独立验收或 lifetime 总账
  时仍为未追踪。关联消耗含共享、包含式范围，不能把重叠数相加或做效率排名。
  恢复检查仍不可执行；资源仍保留；反馈候选仍非同根因、非副本实时认证。
- 每次刷新先清空旧表格、摘要、诊断及详情，仅保留**完整选择 ID**用于本次数据中的
  重新定位。来源切换还清空选择；失败/缺记录/withdrawal/revoke/来源失效不恢复旧事实。
  请求 generation 阻止迟到数据或错误覆盖新范围；退出使结果失效，不取消真实 Agent。
- 每个看板至多一个文件快照 reader，加一个“最新范围待刷新”标记。计时器在忙时
  不排队；手工刷新/范围切换可使旧结果失效，但不取消重启底层 reader 或堆积线程。
  默认 5 秒、最短 1 秒；这是轮询间隔，不是硬实时读取截止或进度保证。已开始的
  文件 I/O 不能据此声称被取消、成为原子快照或隔离恶意同 uid 写入者。
- 文本用 plain Rich Text/无 markup Static 展示；命令、URL、角色与诊断仅是引用。
  C0/ANSI、C1 和方向控制不能成为终端动作或身份；JSON 中必要时转义，保留字节字段
  原义。详情最多显示 128 KiB UTF-8 前缀，显式标明不是完整 JSON；不静默 salvage、
  不追加读取隐藏正文。withdrawn/stale/inconsistent 反馈隐藏旧摘要与 peer 关系；
  意图授权失效时隐藏分析。未显示/未读取的数据不是不存在。
- 不创建看板状态、catalog、锁、收据、导出或日志；没有登记/修改/withdraw/verify-copy/
  Git inspect/执行/上传/升级/清场按钮，没有 command palette、网络请求或 provider
  调用。`--tui` 不与 `--json`/`--follow` 混用；已有 CLI 输出默认行为不改变。

测试用自有合成记录、headless Textual 与私有目录；这些结果不是真实 provider、实际
终端可用性/无障碍、移动端或跨租户部署验收。此本地首切片未接入 Hub；认证原生
元数据入口见下面第七批第三个切片。本地看板仍不能绕过或代替远端授权边界。

## 第七批第二个切片：共享的需关注事实（不是干预授权）

```bash
pi-batch ops tasks --attention
pi-batch ops tasks --attention --source my-project --json
pi-batch ops tasks --attention --json --follow --interval 5
# 独立终端看板中选择「关注事实」分类；Web 顶部展示同一观察清单。
```

- `task_attention.attention_view()` 是纯确定性投影：只使用本次已准入的候选、public
  metadata 与来源覆盖，不补读 checkpoint、产物、原历史、反馈/保全 payload，不调用
  Git/provider/网络/进程探测，不读新时钟，不创建告警、ack、锁、收据或通知投递。
  HTTP 仍仅在已认证的 global `/api/tasks` GET 返回同一 `attention`；query flags
  不授予检查/修复能力，不增加路径读取或 mutation endpoint。
- 支持的提示是**精确记录状态**，不对诊断 prose/标题做关键词根因分类：最新失败、
  拒绝、流水线/gate/validation/合并/预算/provider/recovery 阻断；已准入来源覆盖
  的读错/损坏/截断/缺口；本地目标拒绝/证据不再匹配，历史契约失效/目标不可用，
  恢复请求声明到期，未永久保护的资源复核到期，以及草案来源定义变化。
  各提示附固定 reason code、完整 source + typed target identity 和原记录状态引用。
  旧失败之后最新记录成功则不再提示那个失败，但**不删除旧失败记录**。
- 运行/静默、取消/中断、待验收、未知金额/ETA、legacy 未记录内容 hash、内容匹配
  候选或正常 withdrawal/revoke **不因此变成故障或必做操作**。缺少可准入事实
  不编造引用；catalog 诊断仍计入覆盖，零条关注项不证明安全、完整、没有遗漏或
  不需人工判断。提示不等于实时仍被阻断、已知根因或独立验收失败。
- 任务关注计数使用全部已准入 candidates，**先于任务显示 limit**；遗漏任务的
  提示也可以显示，但不能因此读取它的产物或扩大其它 metadata 窗口。保留最多
  128 个关注引用，完整计数/按 reason 与 kind 分组/遗漏数另列。按完整 attention
  ID 稳定排序，不是严重性/近期/效率排名；计数不是不同故障、同根因事故或唯一
  待处理工作数，父子记录可描述同一次失败，不能把它们算成独立缺陷。
- 不回传旧历史/草案摘要、正文、原始私有路径或未显示的证据内容；只有固定说明与
  已公开元数据引用。at-ID 由完整 source、kind、target ID 与固定 reason 决定，
  不以标题、脱敏显示、PID、时间或用户署名猜身份。目标可能不在本次任务表窗口，
  不代表已解决/删除；未增加隐藏展开、自动重试、复用成功或跨来源查询。
- CLI `--attention` 与 `--id`/`--tui` 明确互斥，JSON follow 仍为 JSONL，普通
  `ops tasks` 保留原任务数据并追加观察计数。终端新增只读分类，旧 schema1
  快照缺少 attention 时不伪造零；未知 attention schema/身份/超预算列表拒绝。
  Web 用 textContent/JSON 文本，选中事实不发送额外请求；刷新立即清除旧清单和
  选中引用，切换来源、失败、缺失、迟到响应/错误不恢复旧事实。
- 本地 Web 在任何记录/来源选项写入 DOM 前准入关注投影：只有**缺少字段**才是
  legacy 未追踪，显式 null/false/零或未知 schema 不能伪装成零项/旧版数据。关闭的
  字段、最多 128 个引用、整数计数/遗漏与 reason/kind 汇总、来源/候选/诊断覆盖、
  完整 typed ID/来源过滤/稳定顺序、原记录状态及固定 false 权限均须一致。相同
  attention ID 或完整引用 tuple 的歧义重复拒绝；不同目标或 reason 的引用不会
  被猜测合并。不要求目标在任务显示窗口内，也不补读未显示目标、原始正文或产物。
  多余私有字段、未知 shape、孤立 Unicode 代理字符或计数/权限冲突使本轮不可用，
  不显示部分新事实或复活旧数据；合法 Unicode 对和 literal 标签仍可显示。
  此准入只核对已供给的有界元数据 shape/关系，不重算 at-ID digest、不核验原进程
  身份/活性，不证明语义等价、根因、完整覆盖或执行权，也不是 HTTP 字节预算/原始
  JSON 重复键校验、单读器或实际浏览器/移动端验收。
- 每项 `operator_action=not_selected_or_authorized`、`root_cause=not_verified`；
  仍无执行/恢复/合并/清场/上传/预算期限复位权。资源到期仍保留，声明请求过期
  不等于原 deadline/预算验证；本地操作人拒绝不冒充独立评审。此清单没有通知
  投递/ack、调度、Hub tenant grant 或 Console 远端入口。

测试以自有合成状态与实际共享快照覆盖纯度、完整身份、显示遗漏、不变的权限、
CLI/认证 HTTP/终端一致、Web 失效与 literal 内容；Node VM/headless 不是实际浏览器、
广泛终端可用性/无障碍/移动端/多设备验收，实际执行/复现/发布仍未启用。

## 第七批第三个切片：认证 Hub 原生任务事实与远端共享入口

```bash
# 使用既有的用户登录配置或私有 token 文件；不把凭据放 URL/命令参数。
pi-batch fleet tasks --hub https://hub.example --json
pi-batch fleet tasks --attention --json
pi-batch fleet tasks --source src-<完整24位hex> --tui
pi-batch fleet tasks --json --follow --interval 5
# 同源 Web：/api/v1/agent/task-center/view（无数据的公共 shell）
# 数据 GET：/api/v1/agent/task-center?limit=100&source=src-<完整24位hex>
```

- **接入范围**是已经明确发布到 Hub 的 instance/session/turn 元数据，不是远端整机、
  私有 runtime、全部 Campaign、计算任务或已认证多设备完整清单。Gateway 默认、
  本地 `ops tasks`、原生 Agent/Hub 控制 TUI 与受保护 Campaign 均不改变。这里不
  自动发现/上传本地目标、历史、恢复、资源或反馈，五组明确为 `untracked_not_imported`；
  空数组是共享显示容器，不是五类数据已不存在或已经完整读取。
- 数据入口只接受 user 身份，仍先验证签名/tenant/client grant 和可选 membership
  门禁。**同时要求** `agent.instances:read` 与 `agent.sessions:read`，不借 session
  scope 暴露另外受保护的实例心跳/代际。tenant 与 instance allowlist 在 SQL 的 LIMIT
  **之前**生效；Gateway、别的 tenant、单独 compute scope 和未知/路径 query 不获额外权。
  非 loopback 仍要求 TLS，开发静态凭据仍只能 loopback；没有新增/拓宽 grant。
- 在既有 Hub 数据库一个 `query_only` 读取事务中最多选 64 个授权实例、512 个 session
  和 512 个 turn，额外一行只判断窗口缺口；SQLite VM 工作另有限额。源过滤只认当前
  授权 64-source 窗口中的完整 source ID，不循环翻页/反查整库、不猜实例名或路径。
  窗口外/无权限/缺失来源统一不提供记录；不据此推断删除。发生裁剪时库内遗漏总量未知，
  显示遗漏与库窗口遗漏分别表述；SQLite 读事务不是设备/文件系统/进程原子快照或 lease。
- 不选择/解析/回传 prompt、消息/event payload、commands、actor、幂等键、原生历史或
  本机目录正文；不调用新时钟、provider、进程/设备探测、Git 或修复/归档验证。这里没有
  业务写入/调度/收据/通知；不将 SQLite 自身的读取协调声明成无锁文件系统隔离。
- source/task/attention 身份采用独立 Hub namespace + **完整 tenant/instance/session/turn**，
  不按标题、local ID 或时间合并。session → turn 父子关系显式保留；父记录可能在显示
  窗口之外，此时不是已完成或消失。Hub 与本地记录**不会自动去重或相加**，两者尚无
  经过验证的映射。状态只是已记录状态；标签不是原用户目标，generation/冻结不是 liveness。
  没有读取执行开始/消耗/产物证据，因此耗时、token、币种、provider 请求、验收与 ETA
  保持未知，不将 queued→terminal 的登记间隔冒充执行耗时或计入效率。
- `attention` 直接复用纯共享算法，先于任务显示 limit，最多 128 个引用、完整有界
  计数/遗漏；这里的来源缺口只是元数据读取窗口。提示仍不是根因、不同事故数、必做
  操作或恢复批准，不复位原期限/预算。取消/中断/lost 本身不被改称失败根因。
- Python 客户端至多一次元数据 GET、2 MiB 响应；重建/核对封闭的公开任务投影，拒绝
  多余私有字段、身份/计数/权限冲突、未知 shape 与不一致的已显示关注引用。请求前后
  credential context 改变则拒绝旧结果；无本地账本、events/body、其它端点或旧缓存兜底。
  `fleet tasks --tui` 复用独立观察器的单 reader/最新待刷新标记、清空先于刷新与 generation
  fencing，不接入现有 Hub 的 send/cancel/compute widgets。退出只表示退出观察器。
- Web 是 **Console 可消费的只读 Web 入口，不是 Flutter Console 已集成/发布**。公共
  shell 没有任何租户数据；它不接受 URL/query 凭据。临时 bearer 只留页内存，输入即
  清空旧事实，字段随后清空；不写 local/sessionStorage，清除本页凭据不冒充服务端撤销。
  所有数据 GET 重新鉴权，401/403 停止自动读取；同源连接、无重定向、no-store、no-referrer、
  hash CSP。最多一个请求和一个最新待刷新标记，忙时 timer 不堆积；源/凭据切换、失败、
  格式冲突或迟到结果不恢复旧事实。文字只用 textContent，命令/HTML 是引用；详情是
  限缩的公开字段显示，不是完整 JSON/正文，不额外 fetch。没有操作、外发或升级控件。

新增回归以自有 HTTP/SQLite、真实本地签名 JWT、合成元数据与 Node VM/headless TUI
检查权限交集、授权先于裁剪、无正文选择、跨端身份/关注一致、缺口/损坏、凭据 fence、
literal 内容和失效规则；不声称生产 issuer、实际远端设备、Flutter、无障碍或移动端已验收。

**仍未完成全部规划**：受控修复执行/独立复现/发布升级、完整可恢复归档与授权清场、
provider/全生命周期计量、语义/效率/ETA，以及 Hub 计算/本地账本的显式授权桥接、Flutter
与真实跨设备/平台验收仍有缺口。实现这些机制不授权在受保护任务上实际执行或删除；
已知真实 Codex 访问约束也不能通过改账户、绕过区域限制或排除原测试换取验收。

## 第七批第四个切片：显式计算任务元数据观察（不调度、不合并）

```bash
pi-batch fleet tasks --compute --json
pi-batch fleet tasks --compute --attention --json
pi-batch fleet tasks --compute --source src-<完整24位hex> --tui
# 独立 Web shell：/api/v1/agent/compute-task-center/view
# 数据 GET：/api/v1/agent/compute-task-center?limit=100&source=src-<完整24位hex>
```

- 默认仍是原生 session/turn 视图。`--compute` **选择独立的计算元数据窗口**，不是把
  两类记录汇总/去重/相加；两者 source/task 的 namespace 不同，原生 source ID 不能
  用来扩大计算读取范围。没有本地账本上传、设备清单/放置探测或 Flutter 控件集成。
- 继续要求 user、当前 tenant/client grant 和可选 membership，**同时要求**既有
  `agent.instances:read` 与 `agent.tasks:read`。只读任务所记载的 **source instance**，
  不把 destination 的 `compute_instances`/设备权限反向当作读取其它来源的授权。
  `agent.sessions:read` 不能替代计算权限，计算读取权限也不能打开原生会话投影。
- 授权 SQL 先于 LIMIT：一个 query-only 事务最多 64 个授权来源和 512 个任务，再多
  一行只检查窗口缺口；VM 工作有界。完整来源 ID 只在已准入的 64-source 窗口内解析。
  不循环翻页或猜任务/来源；无权/窗口外不返回记录，不推断删除，裁剪后库总遗漏未知。
- 只选择 task/source/session ID、已记录 generation/state 与登记/更新时间；**不选择、
  解析或回传** `data_json`、`request_json`、argv/workdir、result、artifact/device/
  workspace、actor 或幂等键，不读取 session 标签、event body、私有历史或新时钟。
  读取不初始化 schema、不启动 broker/tick、Fabric/provider/网络探测或执行命令。
  未配置 compute 返回不可用，而不是在 GET 中自动安装/启动；普通 SQLite 读取协调
  不等于设备快照、独占锁/lease、硬实时 I/O 或隔离恶意同 uid 写入者。
- 完整 tenant/instance/session/compute-task 身份进入独立 ID。session ID 是已记录的
  引用，**不是已解析的原生父任务或目标绑定**；destination/attempt/turn 未读，保持
  未追踪。任务 ID 不是用户目标；queued→terminal 时间差不作为执行时长；generation/
  heartbeat/state 不证明活性。token/费用/provider 请求/独立验收/真实 ETA 仍为未知。
  `lost`、取消、排队和成功不因此成为已知故障/已验收；精确 `failed` 只产生共享关注事实。
- 关注计数先于任务显示 limit，128 个有界引用/完整已准入计数/遗漏；非事故去重、优先级
  或修复授权。客户端只读取一次 2 MiB 元数据响应，重建闭合计算投影；私有字段、跨 profile
  身份/phase、伪造数量/费用/权限或 credential context 变化都拒绝，无 native/local/
  task-body/旧缓存兜底。相同独立终端观察器和 Web 使用已有的失效/单 reader/文本规则。
- 新 Web shell 无租户数据或凭据；事实 GET 重新认证，bearer 仅页内存、无 URL/storage，
  hash CSP 随该固定计算脚本生成。没有取消/重试/reschedule/删除/上传或预算/期限复位
  动作；HTTP mutation/body/未知或重复 query/重复 Content-Length 仍拒绝。退出只是退出
  看板，不取消任务或释放 lease。现有计算控制 API 与 Agent/Hub TUI 均未改变。

合成 HTTP/SQLite、SQL column authorizer、签名 JWT、CLI/headless TUI 和实际交付的
Web JS 回归只验证此读取机制。元数据中的坏私有 JSON 可以保持不可读而不被解释为命令；
这不证明对应计算任务本身可用、真实设备/生产 issuer/Flutter/跨平台或独立成果已验收。
本切片未实现统一跨来源合并、真实执行遥测、完整可恢复归档或 live repair/清场。

## 第七批第五个切片：显式计算源会话身份引用（不是执行/本地映射）

```bash
pi-batch fleet tasks --compute --source-sessions --json
pi-batch fleet tasks --compute --source-sessions --tui
# 数据：GET /api/v1/agent/compute-task-center/source-sessions?limit=100&source=src-<完整24位hex>
# 无数据 shell：/api/v1/agent/compute-task-center/source-sessions/view
```

默认原生/计算视图及其权限均不变；必须显式选择此第三个 profile，**同时**具备
`agent.instances:read`、`agent.tasks:read`、`agent.sessions:read`。既有 user 身份、
JWT/development grant、tenant/source-instance allowlist、membership 仍先于 SQL LIMIT
生效；destination compute 权限不替代源读取权。没有自动授予权限或本地账本发现/上传。

一个 query-only 事务在 64 个来源窗口内各读取最多 512 条计算记录/源 session 元数据，
分别多取一行检测裁剪，并共享有界 SQLite VM 工作预算。源 session **仅**选择完整
instance/session 身份、generation 和登记更新时间；不选择标签/frozen/project、正文、
原生 turn/event、命令/结果 JSON、设备或 actor。来源仍接受计算 namespace 的完整
source ID；原生 source ID/名称/路径不会扩大窗口。没有单条 session 兜底查找。

`hub-compute-source-session-references-v1` 保留计算任务的完整身份，为每个候选记录
附加稳定 `hr-...` 引用。仅在该窗口内 instance/session/**generation 均完全一致**时
给出 `observed_recorded_reference` 及独立原生 namespace 的 source/task 指针；其它情况
均为 `unresolved_in_authorized_window`，不区分缺失/外租户/窗口外/代际不一致，不猜
旧代际/同名/相近时间的目标。指针不证明目标会显示在另一次原生 GET 中。

引用计数先于任务显示裁剪；多个计算任务指向同一 session 仍是多个引用，不去重成
事故或产出。session 窗口裁剪明确披露，遗漏总量未知；未解析引用不是故障告警。
`parent_execution_verified`、`goal_binding_verified`、`deduplication_authority`、
`billing_addition_authority`、`grants_execution` 永远为 false；费用、执行耗时、活性、
独立验收和 ETA 仍未追踪。元数据 identity 匹配不是因果关系、执行父子关系、目标绑定、
账单相加或 local/native/compute 等价证明。此处**没有完成跨本地账本的验证映射**。

CLI/Python/TUI/Web 都仅执行一次有界元数据 GET，无原生/计算默认端点、本地历史或
旧缓存回退。凭据/来源变化先清空事实，单 reader/generation fence 拒绝迟到响应；
Web 的公开 shell 不授予读取权，bearer 仍只在当前页内存中、无 URL/storage。
仅 literal 引用显示，不自动打开目标、下载正文或增加控制；mutation/query/body 限制
继续生效。没有 provider 调用、schema/controller 初始化、执行/取消/修复/上传/清场。
这是 Console 可消费入口，不是 Flutter、真实设备、平台、无障碍或部署验收。

## 后续批次与验收条件

| 批次 | 内容 | 完成判定 |
|---|---|---|
| 2（本批） | 显式目标契约/证据判定、持久检查点、父子 run 与声明下一步 | 新旧事件分开；历史/完整字节可复核；Agent 自述不判完成；不替代执行与独立验收 |
| 3（已实现五个切片） | 授权导出/原生范围重放、角色线索与 Web 引用、历史耗时检验、显式关联的阶段消耗 | 保留来源/覆盖；调用去重、失败与共享消耗可见；全量请求计量、语义预测与独立验收效率仍待实现 |
| 4（已实现预览/请求登记/只读检查/显式 Git 诊断） | 失败检查提案、声明限制、永久撤回、缺证阻塞与一次性工作区诊断；后续有预算的项目修复、工具改进任务与独立验证 | 预览/登记不授执行权；真实修复需在预授权范围内产生代码/测试；正常门禁；不无限重试或自我授权 |
| 5（已实现显式保全登记/72h 复核/永久保护/只读计划） | 后续归档验证与安全清场 | 只回收登记资源；无活跃引用；未保存成果不删；历史/恢复信息仍可查询 |
| 6（已实现本地合成草案/不可重写审计/永久撤回/显式副本字节检查/内容元数据匹配） | 后续独立复现任务与审查发布闭环 | 默认不外发；原始 runtime 不进 Git；只晋升可复现的回归测试/修复 |
| 7（已实现本地只读 TUI/关注事实及独立的认证 Hub 原生/计算元数据 CLI/TUI/Web 入口） | 后续经验证的跨来源关联/本地账本授权桥接、Flutter 与真实多设备验收 | 不绕过 tenant/instance 授权；共享完整身份与事实；只读入口不伪造操作回执或部署完成 |

每批先提供测试保护，再增加自动化。任务绑定工具版本，更新从新任务或明确检查点启用。
清场、外发和自动修复没有因这个只读入口而获得授权；硬门禁和合并权限不改变。
