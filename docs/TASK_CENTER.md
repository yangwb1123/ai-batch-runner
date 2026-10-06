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
第三批仍待：更多原生形状/长会话的授权导出、语义意图/漂移检验、全生命周期 phase/provider 请求与币种覆盖、
独立验收成果效率、跨任务难度校准与真实剩余时间预测。以上不得由线索或窗口消耗代替。

## 后续批次与验收条件

| 批次 | 内容 | 完成判定 |
|---|---|---|
| 2（本批） | 显式目标契约/证据判定、持久检查点、父子 run 与声明下一步 | 新旧事件分开；历史/完整字节可复核；Agent 自述不判完成；不替代执行与独立验收 |
| 3（已实现三个切片） | 授权有界导出/原生用户投影、角色线索、历史耗时检验、显式关联的阶段消耗 | 保留来源/覆盖；调用去重、失败与共享消耗可见；全量请求计量、语义预测与独立验收效率仍待实现 |
| 4 | 有预算的项目修复、工具改进任务与独立验证 | 在预授权范围内产生真实代码/测试；正常门禁；不无限重试或自我授权 |
| 5 | 72 小时保留窗口、归档验证与安全清场 | 只回收登记资源；无活跃引用；未保存成果不删；历史/恢复信息仍可查询 |
| 6 | 本地最小反馈案例、去重、复现任务与发布闭环 | 默认不外发；原始 runtime 不进 Git；只晋升可复现的回归测试/修复 |
| 7 | Hub 多设备授权与 TUI/Console 共享入口 | 不绕过 tenant/instance 授权；所有端看到相同身份、事实和操作回执 |

每批先提供测试保护，再增加自动化。任务绑定工具版本，更新从新任务或明确检查点启用。
清场、外发和自动修复没有因这个只读入口而获得授权；硬门禁和合并权限不改变。
