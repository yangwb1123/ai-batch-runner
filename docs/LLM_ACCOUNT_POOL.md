# LLM 账号池与 Go 请求网关

`pi-batch pool` 管理用户级 OpenCode Go 账号池。运行中的 Agent 把模型请求发给
本地网关，网关选择账号并转发；收到明确的额度拒绝后，在尚未向客户端输出响应时
尝试下一个独立配额组。进程内不修改全局环境，也不覆盖 OpenCode 的 `auth.json`。
启用额度预检后，网关先查询 Go usage 接口，直接跳过已满的订阅窗口。

本功能显式启用，不改变现有 batch、TUI、Fleet 或模型路由的默认行为。
当前私有凭据文件要求 POSIX 文件权限和 no-follow 支持；验证目标为 Linux。

## 安装与配置

使用现有源码安装，无新增 Python 依赖。配置模板随 wheel/sdist 分发：
[`examples/llm-pool.yaml`](../examples/llm-pool.yaml)。

可复制模板到 `~/.config/pbatch/llm-pool.yaml`，按实际账号修改 `accounts` 和
`quota_group`，在 `models` 中显式列出需要使用的模型及其协议。例子中的模型仅为
配置演示；可用性与协议以 [Go 官方端点列表](https://opencode.ai/docs/go/#endpoints) 为准。

一个 `quota_group` 表示一份共享订阅/配额。同一份额度下的不同 API Key 必须使用
同一组名；它们共享冷却和并发限制。不同文件中的相同 Key 不能分配到不同组。
服务端账户归属无法仅凭 Key 字符串推断，运营者负责正确声明配额组。

也可以使用初始化命令，不再需要手工复制 YAML：

```bash
pi-batch pool init --account go-a --quota-group subscription-a \
  --model glm-5.1=chat/completions --dry-run
pi-batch pool init --account go-a --quota-group subscription-a \
  --model glm-5.1=chat/completions
pi-batch pool account add go-b --quota-group subscription-b --priority 20
```

`--model MODEL=PROTOCOL` 可重复传入，必须选择实际可用的模型与协议。
init 默认启用额度预检，只创建配置，不读取或生成 Key、不创建数据库、不启动网关，
已有文件一律拒绝覆盖。默认新凭据引用位于配置目录下的
`llm-pool-keys/<pool_id>/accounts/<account>.key`，网关令牌同级独立存储；
Key 和令牌仍通过下面的 `key-set` / `token-init` 显式创建。

```bash
# 可先在 checkout 用模板验证，无网络请求、无运行态写入
python pi-batch.py pool --config examples/llm-pool.yaml check --json

# 以下使用已复制到用户目录的配置
pi-batch pool token-init
pi-batch pool key-set go-a
pi-batch pool key-set go-b
pi-batch pool check --credentials --json
pi-batch pool serve
```

`key-set` 隐藏交互输入，不接受 argv 明文 Key；非交互秘密输入可显式使用 `--stdin`。
`token-init` 生成独立的本地网关令牌，只输出路径。已存在的凭据默认不覆盖，轮换时
必须显式提供 `--replace`。凭据目录要求 `0700`，文件要求 `0600`；符号链接、硬链接、
公开可读文件均拒绝。网关启动时加载凭据，轮换后重启网关；认证故障还需显式
`reset-auth ACCOUNT`。恢复账号不会删除配额冷却记录。

可在项目 `pi-batch.yaml` 或用户 `settings.yaml` 中指定共享配置位置：

```yaml
llm_pool:
  config_file: /absolute/path/to/llm-pool.yaml
```

优先级为 `pool --config FILE` > 当前解析配置的 `llm_pool.config_file` >
用户级 `llm-pool.yaml`。配置中的相对凭据路径相对于该账号池文件。
`pool_id` 确定共享运行状态：
`~/.abr/llm-pools/<pool_id>/state.sqlite3`，受 `PBATCH_RUNTIME_HOME` 等
现有用户目录选项控制，独立于项目 worktree 和项目数据目录。

### 本地接入诊断

```bash
pi-batch pool doctor                 # 离线逐项检查配置与启用账号的私有凭据
pi-batch pool doctor --json          # 脱敏结构化报告，适合脚本
pi-batch pool doctor --gateway       # 显式核验本机网关令牌、模型及实际加载的配置/凭据
```

与 `check` 的整体通过/失败不同，`doctor` 汇总各账号的检查结果和固定修复提示，
包括缺失/不安全的凭据、所有账号被禁用、网关令牌复用供应商 Key，以及同一 Key 被
错误分配到不同配额组。禁用账号的 Key 不读取、不作为缺失凭据报错。
不显示 Key、Key 摘要、凭据路径、原始异常或服务端响应正文。

默认完全离线；`--gateway` 只使用本地令牌向配置端口的
`http://127.0.0.1:<port>/v1/models` 发起一次 GET，不发送供应商 Key、不调用模型或
usage 接口、不采用环境代理、不跟随重定向。探测共用 3 秒绝对截止时间，响应上限
64 KiB；超时、认证拒绝、模型 ID 不一致或畸形响应均明确失败。令牌缺失/非法或
已识别为启用账号的供应商 Key 时跳过探测，不尝试其他凭据。

网关在启动时冻结账号/模型配置和启用凭据，并在鉴权成功的模型清单响应中附带
`X-Pbatch-Pool-Snapshot: v1:<opaque>`。这是以本地网关令牌为密钥的 HMAC-SHA256
校验值，覆盖完整解析配置（含账号顺序、配额组、协议、凭据路径、运行状态文件位置、
监听端口、并发/超时/额度预检参数）及实际加载的启用账号 Key。网关只在启动时
计算，不在探测时重新读盘或访问数据库；不返回原始配置、Key 或裸 Key 哈希。
响应禁止缓存，校验信息不附加到模型请求或供应商响应。

doctor 用本次本地读取计算预期值，新增 `gateway_snapshot` 检查：一致为
`gateway_snapshot_match`；账号增删/排序、协议变更、同路径密钥轮换或运行目录不同
等情况返回 `gateway_snapshot_mismatch`，提示核对配置并重启。YAML 注释、字段顺序、
等价默认值不造成误报。禁用账号的密钥内容不参与校验，也不读取；启用凭据读取不全
则跳过快照比较，保留本地失败与未检查标记，不能用部分凭据宣称加载状态一致。
报告不显示校验值、密钥摘要或差异内容。

旧网关没有校验信息时返回 `gateway_snapshot_unavailable`；未知版本、畸形或重复
校验头返回 `gateway_snapshot_invalid`，均为失败，不降级到“模型清单一样就通过”。
升级后请重启网关。模型列表的格式和匹配仍独立校验，不被快照检查替代。

本命令不启动网关、不创建/迁移/打开 SQLite，不自动修复权限、写配置或恢复账号。
配置读取沿用账号管理的安全约束：所有路径组件非符号链接，文件为当前用户所有的
普通单链接文件，配置及直接父目录不可被组/其他用户写入；因此放在组可写 checkout
里的模板可能通过 `check` 的 schema 检查，但不能通过 `doctor` 的安全检查。
请使用用户私有配置目录，不要为通过检查自动修改共享目录权限。

退出码：0 表示本次选中的本地检查通过；1 表示报告有失败项；非法 CLI 参数为 2。
JSON 包含 `schema_version: 1`、`scope: local_setup`、`ok`、`read_only`、
`checks`（pass/fail/skip、稳定 code 和 hint）及 `not_checked`。
**通过不代表供应商认证有效或额度可用**：不检查运行态 pause/auth/冷却，也不证明
OpenCode 客户端已经接入。只有显式 `--gateway` 才比较加载状态；默认离线检查仍将
这部分列为 `not_checked`。这是本次读取与网关启动快照的对照，不是对磁盘的原子快照
或远程可信证明：检查期间/之后的并发编辑仍可能改变文件，持有本地令牌的进程也能
伪造比较结果。管理配置和轮换凭据时应串行操作，变更后重启并重新检查。
额度观察继续使用 `pool usage --refresh`，实际可调度状态查看 `pool status`。
诊断仅作为 CLI 操作提供，不扩展只读 MCP 的凭据访问范围。

## 连接 OpenCode 和现有 Runner

先单独启动 `pi-batch pool serve`，再在另一个终端使用一次性启动入口：

```bash
pi-batch pool run --model glm-5.1 --dry-run -- opencode
pi-batch pool run --model glm-5.1 -- opencode

# 可显式选择另一款已配置的轻量任务模型；默认与主模型相同
pi-batch pool run --model glm-5.1 --small-model minimax-m2.7 -- opencode
```

`pool run` 只为本次命令合并 `OPENCODE_CONFIG_CONTENT`，保留其他 provider、MCP、
权限和其余配置；更新默认 `model`、`small_model` 及 `opencode-go` 的本地 `baseURL`
和 `{file:...}` 网关令牌引用。不改当前 shell、全局配置或 `auth.json`，不读取 Key
或令牌，也不打开 SQLite。两个模型都必须在账号池配置中声明。

已有内联配置必须为纯 JSON 对象，输入及合并结果各不超过 64 KiB，嵌套深度不超过
32 层；拒绝 JSONC、重复字段、非有限数和无效 UTF-8，不静默丢弃无法解析的配置。
若可见配置显式禁用该 provider/模型，或包含冲突的认证头、自定义 SDK/传输、所选
模型的 provider/ID 重定向，启动器会报错，请先审查配置。它不替用户绕过已有过滤策略。

`--dry-run` 始终输出 JSON 脱敏计划，不查找或启动命令、不访问网络、不创建文件；
不显示命令参数、继承的配置/环境、凭据路径或密钥。报告明确列出未检查的可执行文件、
网关、供应商认证、额度和客户端最终配置。预览通过不代表接入可用，可另行运行
`pool doctor --gateway` 检查本机网关。

必须使用 `-- COMMAND [ARGS...]` 分隔启动器和目标命令；分隔符后的参数原样交给
目标命令，不做 shell 展开。正常运行要求 POSIX，通过 exec 替换当前进程，保留工作
目录、终端/标准输入输出、退出码及信号行为；启动器不额外打印配置，也不脱敏目标
命令自己输出的内容。输入非法退出 2，无法执行退出 126，找不到程序或解释器退出
127；启动成功后退出码由目标程序决定。网关不会自动启动或停止。

需要保留现有 Runner 工作流时，也可将其作为目标命令，环境会由后续子进程继承：

```bash
pi-batch pool run --model glm-5.1 -- pi-batch --agent-bin opencode \
  --model opencode-go/glm-5.1 -p "分析当前项目"
```

这仅注入默认模型配置，**不是网络隔离或全量代理保证**。客户端命令行、agent 的模型
选择、其他 provider 或管理员配置仍可能改变实际路径；目标程序也可以忽略此环境。
OpenCode 的管理员配置优先级高于内联配置，见
[官方配置合并顺序](https://opencode.ai/docs/config/#precedence-order)。启动器只校验
已有内联配置，不读取其他层配置或替客户端展开变量；现有 Runner 的 agent 参数及
OpenCode 版本兼容性仍须在目标环境验收。

手工集成仍可使用 `pi-batch pool client-config --model glm-5.1` 导出最小配置。
该命令不合并现有环境，直接 `export OPENCODE_CONFIG_CONTENT=...` 会覆盖原内联配置；
请自行合并或使用 `pool run`。两者仅引用绝对令牌路径，不输出 Key；为避免文件引用
展开歧义，路径拒绝花括号、双引号、反斜线和控制字符，支持空格及合法 Unicode。
客户端须支持 provider `options.baseURL`、`options.apiKey` 和文件引用；见
[Provider](https://opencode.ai/docs/providers/) 与 [配置变量](https://opencode.ai/docs/config/#variables)。

其他 Agent 可以使用相同本地端点和网关令牌，但需要在各自配置中显式接入。
网关要求每个对话携带稳定的 `x-opencode-session` 请求头，并保持上游要求的客户端
身份。不要给每次模型请求生成不同的会话 ID。Pi RPC 等长期存活的子进程在改变
客户端配置后需要重启；本功能没有注入运行中子进程的隐藏凭据切换。

## HTTP 契约

监听固定为 `127.0.0.1`，默认端口 `8741`，每个请求必须携带本地网关令牌：
`Authorization: Bearer ...` 或 Messages 客户端的 `x-api-key`。
不支持匿名访问、浏览器 Origin、远程监听、任意上游地址或重定向跟随。

| 路径 | 用途 |
|---|---|
| `GET /v1/models` | 返回模型清单和已加载快照校验头，不调用供应商或读取运行态 |
| `POST /v1/chat/completions` | OpenAI-compatible Chat Completions 请求 |
| `POST /v1/messages` | Anthropic-compatible Messages 请求 |
| `POST /v1/responses` | 携带完整历史的前台 Responses 请求 |

模型必须与配置声明的协议匹配。HTTP 请求不超过 2 MiB，需要唯一的
`Content-Length` 与 JSON 内容类型，不支持 chunked 请求体；流式响应按小块转发，
最大 64 MiB，整个请求含所有账号尝试共用一个绝对截止时间。网关线程数量有界。
供应商认证头由网关替换，本地令牌、Cookie 等不会发送给供应商。
最终响应的 `X-Pbatch-Account` 仅包含选中账号的配置 ID。

`previous_response_id`、`conversation`、`item_reference` 与后台 Responses 请求
不允许跨账号转发；客户端必须提供完整历史。上游响应已开始交付、流内错误、
连接中断或结果不确定时不会切换账号，也不会重放已执行的工具或整个开发任务。

## 选择、冷却与恢复

数字越小的 `priority` 越优先，同优先级按配置顺序选取。当前账号可用时持续使用；
它不可用或并发槽已满时选下一组，优先账号恢复后可以重新被选择。
每组并发限制取组内配置的最小 `max_concurrent`，默认 1。

SQLite 事务保证跨 worker/跨网关实例共享准入。租约覆盖请求的绝对截止时间及
安全余量，正常结束时释放；进程意外退出后等待租约过期回收。

| 上游结果 | 行为 |
|---|---|
| 识别出的 Go 配额错误 | 记录相应窗口/范围的冷却，在本次请求尝试下一个组 |
| 普通 `429` | 按服务端提示或探测间隔冷却，返回限流错误；不伪装成订阅耗尽 |
| 认证/订阅资格 `401` / `403` | 标记该凭据认证失败，返回错误；更换凭据后显式清除故障 |
| 结构化 `CreditsError` / `MonthlyLimitError` / `UserLimitError` | 虽然 Go 返回 `401`，仍按明确计费耗尽切换账号 |
| 结构化 `ModelError` / `RegionError` / `DataPolicyError` | 请求或模型策略错误，不把凭据标记为失效 |
| `5xx`、未知拒绝、连接失败 | 返回脱敏错误，不跨账号重放不确定请求 |
| 全部不可用 | 返回 `429 pool_unavailable`，有已知下次检查时间时附 `Retry-After` |

5 小时、周、月窗口分别记录，同时满足才允许继续使用。调度器支持模型级范围；
本轮 Go 适配按已核验源码的订阅级错误保守阻止该配额组。`Retry-After` 作为下次检查依据，
不会硬编码“错误发生后再等五小时”。未知重置时间按 `probe_after` 有限探测；
状态中 `reset_known: false`、`probe_due` 表示待探测，不等于确认额度已经恢复。
成功探测只清除它实际观察到的旧故障，不覆盖并发请求产生的新故障。

### 实时额度查询与预检

```yaml
usage:
  enabled: true
  max_age: 60  # 额度缓存最多有效 60 秒，也不能越过任何窗口的重置时间
  timeout: 5   # 单次查询超时；同时受模型请求的总截止时间约束
```

模板已启用；旧配置不写 `usage` 时保持错误触发模式。网关按优先级懒查询候选组，
无需每次轮询所有账号。查询固定为 `GET https://opencode.ai/zen/go/v1/usage`，
只发送对应供应商 Bearer Key，不发送本地令牌、对话正文或 Cookie。
官方契约见 [usage.ts](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/go/v1/usage.ts)。

必须同时获得 rolling（展示为 `5h`）、weekly、monthly 三个窗口的合法结果；
`percent` 是**已使用百分比**，不是剩余额度。任一窗口明确耗尽，跳过该配额组；
已知耗尽在重置时间前持续生效，即使缓存已过期。重置后重新查询，而不凭本地时钟
假定已恢复。缺失、过期或查询失败的状态不能放行启用了预检的模型请求：尝试其他
可用组，全部不确定时返回 `503 pool_usage_unavailable`；明确全满仍返回 `429`。
预检成功也不会清除模型请求产生的冷却或认证故障，原有错误切换继续兜底。

SQLite 额度缓存和排他刷新票据跨进程共享；有效缓存复用，刷新失败短暂退避，
不跟随重定向，不无限重试。过期票据的响应不会覆盖较新的观察。查询接口认证失败
只记录额度查询错误，不擅自清除或设置模型认证状态。

```bash
pi-batch pool usage --json                    # 只读缓存，不读取 Key、不访问网络
pi-batch pool usage --refresh --json          # 显式查询，未过期缓存仍复用
pi-batch pool usage --account go-a --refresh  # 指定账号；同组共享结果
```

报告包含 `usage.state`（unknown/fresh/stale/error/refreshing）、`observed_at`（最后成功
采样时间）、`checked_at`（最后查询时间，可能失败）、
`fresh_until`、`next_refresh_at` 和三窗口 `percent/resets_at`；时间为 Unix 秒。
`--refresh` 尊重暂停/禁用与认证故障，不强制恢复账号；所选账号未全部获得 fresh
结果时退出码为 1，并保留脱敏报告。纯查看退出码为 0 不代表额度可用。

这是有时效的观察，不是供应商侧额度预留：缓存间隔内、其他客户端或正在运行的
请求仍可能消耗余额。Go 控制台若启用 Use balance，供应商可能继续扣余额而不报错，
预检不能保证零额外费用；使用者应按自己的扣费策略配置控制台。本工具不修改余额
选项，也不自动购买或充值。

已有 v1 账号池 SQLite 状态会在首次打开时事务升级到 v2，保留原账号控制、租约、
冷却和事件；不兼容或损坏的数据库拒绝升级。升级前应停旧网关；旧二进制无法读取
v2，回退需恢复自己保留的升级前状态备份，不能直接降级启动旧进程。

错误类型与顶层 `metadata.limitName` 来自官方
[error.ts](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/util/error.ts)
和 [handler.ts](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/util/handler.ts)。
仅这些明确类型允许切换；普通 `429` 或正文出现 quota 字样不构成订阅耗尽证据。

现有 `budget-max`/日预算继续限制 Runner 调用；账号池并不把它转换为供应商
token/金额上限。配额与价格说明见 [Go 官方文档](https://opencode.ai/docs/go/)。

## 管理与 MCP

### 账号配置生命周期

```bash
pi-batch pool account list --json
pi-batch pool account add go-c --quota-group subscription-c --priority 30 --dry-run
pi-batch pool account add go-c --quota-group subscription-c --priority 30
pi-batch pool key-set go-c
pi-batch pool account update go-c --priority 5 --max-concurrent 2 --dry-run
pi-batch pool account update go-c --priority 5 --max-concurrent 2
pi-batch pool account update go-c --disable

# 如果要立即停止新准入，先操作运行态，再移除配置，最后重启所有相关网关
pi-batch pool pause go-c
pi-batch pool account remove go-c --dry-run
pi-batch pool account remove go-c --confirm go-c
```

`account list` 只读取配置：`enabled` 是配置启用标志，不代表运行态已 resume 或额度
可用。它不读取凭据，也不打开、创建或升级 SQLite；实际调度状态用 `pool status`。
add 拒绝重复 ID、冲突路径和超过 64 个账号；update 只允许 priority、max_concurrent、
enable/disable，不改 ID、配额组或凭据引用。remove 必须重复输入相同账号 ID 确认，
也不能删除最后一个账号（可以 disable）；不删除 Key、运行历史或订阅本身。

init/add/update/remove 都支持 `--dry-run`，输出脱敏的 `changes.before/after`、原配置
`revision` 与结果 `result_revision`（SHA-256），但不创建目录、锁、临时文件或备份。
`account` 的写命令可带 `--if-revision <预览返回的 revision>`，配置已有变化时拒绝
应用，要求重新预览。无变化的 update 保留原文件字节和注释，不创建备份。

实际修改在独立文件锁内执行读取、校验、写入和原子替换，原 YAML 字节先保存为
同目录唯一的 `.<配置文件名>.backup-<随机值>`，报告中返回 `backup_file`。
新配置会重新序列化，注释/原格式保留在备份中，不保证留在新文件；备份不会自动清理。
管理文件要求 POSIX、当前用户所有、普通单链接文件，拒绝任何路径组件中的符号链接、
组/其他用户可写的配置和直接父目录；现有 0644 配置、0755 父目录可用，新文件为
0600、新目录为 0700，不自动 chmod 既有目录。凭据仍遵循更严格的 0700/0600 要求。
凭据引用不可覆盖配置、SQLite/sidecar 或配置管理锁/备份命名空间，也不能互为文件父子路径。

`.<配置文件名>.lock` 是保留的稳定锁文件，存在不代表繁忙，不应手工删除。合作写入者
由锁串行化，检测到文件已被外部修改时拒绝覆盖；不能消除同一用户的非合作进程在最后
检查后再次写入的所有竞争，因此使用命令管理时不要同时手工编辑同一文件。
配置繁忙退出 5，配置冲突/非法输入退出 2；若发布已完成而持久化确认失败，退出 1，
并明确返回 `committed: true, durability_confirmed: false`，应检查文件和备份后再决定
下一步，不能把它当作“没有保存”直接重试。

这些命令**只修改磁盘配置，不热更新网关**。报告中的 `restart_required` 指示已应用
变化需要重启；dry-run 中对应为 `would_require_restart`。在途请求不会被配置删除
终止；先 pause 可阻止旧网关的新准入，随后应重启使用该配置的所有网关。
修改 `enabled=true` 不会清除持久化 pause/auth/配额状态，仍需按意图显式 resume 或
reset-auth。不要把已有账号 ID、配额组或 pool_id 重新指派给不同身份；同 pool_id 的
不同配置文件也共享状态。本轮没有历史身份绑定注册表，remove→add 不是安全的身份重命名操作。

### 运行态与 MCP

```bash
pi-batch pool status --model glm-5.1 --json
pi-batch pool events --limit 20 --json
pi-batch pool pause go-a
pi-batch pool resume go-a
pi-batch pool reset-auth go-a
pi-batch pool mcp
```

`pause` 只影响新的准入，不终止正在执行的请求；`resume` 不绕过认证故障和配额
冷却。事件最多保留 256 条，只包含账号、组、模型及状态元数据，不保存对话、Key
或供应商原始错误正文。

`pool mcp` 是只读 stdio 服务，提供 `pool_status`、`pool_usage` 与 `pool_events`。宿主应将
`pi-batch` 配为 executable，参数设为 `pool --config /absolute/pool.yaml mcp`。
它不会返回密钥，也不提供远程改账号、恢复凭据或任意命令执行工具。
模型请求仍经过网关；MCP 不负责切换宿主模型的认证。
`pool_usage` 可按账号过滤，只展示缓存及其时效标记，不读取密钥、不触发供应商查询；
需要刷新时由网关预检或操作者显式执行 CLI `usage --refresh`。

实现已分离配置/私有凭据、Go 错误适配、共享调度、HTTP 传输、CLI 与 MCP，便于
以后作为独立包或 provider 插件复用。本轮没有引入通用插件加载器或迁移现有
核心门禁；未来新增供应商需要提供自身经过验证的错误和配额契约。

## 验证范围

`tests/test_llm_pool_*.py` 使用本地假供应商验证配置拒绝、密钥隔离、配额切换、
三窗口查询/过期/查询失败、共享刷新、v1 状态升级、并发租约、流式转发、截止时间和
MCP 协议，以及配置生命周期、无写入预览、备份、并发修改和路径冲突。
本地接入诊断另覆盖逐账号脱敏错误、零状态写入、网关令牌/模型探测、重定向拒绝、
畸形/超长响应和绝对截止时间；真实网关的诊断测试不会发起供应商调用。
加载状态校验覆盖同模型 ID 下的账号/协议/参数变更、同路径密钥轮换、重启后恢复匹配、
缺失/畸形/重复校验头拒绝、启动快照不可变及脱敏；探测不会重新读取网关凭据文件。
启动器覆盖内联配置有界合并、冲突拒绝、脱敏预览及无秘密读取；用本地 Python/假
可执行文件验证真实 exec 的参数、PATH、工作目录、标准输入输出、退出码和信号，
不启动真实 OpenCode 或模型请求。
不需要真实 Key，不消耗订阅。
真实 Go 账号的额度触发、OpenCode 客户端版本兼容性和生产部署仍需在目标环境
用授权账号验收；本地测试通过不代表这些外部验收已经完成。
