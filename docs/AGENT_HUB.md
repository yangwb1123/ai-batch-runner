# Agent Hub：跨端、跨实例会话控制

当前已实现 P0/P1 与 P2 计算主路径：统一实例和会话目录、精确 prompt 路由、
幂等 turn、持久事件游标、取消请求，以及 CLI/TUI/Flutter Console 客户端。
P2 增加设备资源目录、授权选机、计算任务、Agent 调度工具与 Vault 证据归档；
配置和使用见 [COMPUTE.md](agent-hub/COMPUTE.md)。
GPU 增量支持 NVIDIA 物理卡发现、逐卡显存选机、持久分配和启动前复核，
CLI/TUI/Console 接入说明见 [GPU.md](agent-hub/GPU.md)。
Vault 工作区增量支持固定版本输入、Runner 独立目录恢复和指定输出回传，
使用方式和容量边界见 [WORKSPACE.md](agent-hub/WORKSPACE.md)。
P3 增加 Aero ID 成员资格门禁、Aero IM 状态通知、Audit 账本回执与持久投递队列，
配置、恢复边界和运维命令见 [ECOSYSTEM.md](agent-hub/ECOSYSTEM.md)。
实现采用一个 Hub 服务、SQLite WAL 和主动出站的实例 Gateway。
会话运行时继续属于 Gateway 所在设备，数据库默认位于用户级 runtime。
Hub/Gateway 当前使用 POSIX 文件锁，本轮运行验证在 Linux 完成。
Flutter 客户端共享 Web、桌面和移动端代码；本轮完成 Web 构建，
尚未验证 Android/iOS/桌面安装包，也未提供 Windows 原生 Gateway 文件锁适配。

## 数据所有权与部署边界

| 系统 | 本次职责 |
|---|---|
| Snaplink | 签发用户/机器 access token；Hub 验签并检查 issuer、audience、有效期、tenant、subject、client_id、scope |
| Agent Hub | 保存实例登记、会话索引、命令、事件和游标；按主体的实例授权过滤全部入口 |
| 实例 Gateway | 在操作员固定的 cwd 中拥有 AgentSession，执行指定会话的 prompt，报告受限事件 |
| snaplink-console | Flutter Web/桌面/移动端的 Agent 入口；使用独立 Hub resource audience |
| Aero ID | 按显式账户映射和配置的新鲜度窗口检查 active 成员投影；不保存运行时会话 |
| Aero IM | 可选的机器集成状态卡片接收端；普通聊天消息不直接成为执行命令 |
| Aero Vault | 固定版本执行证据归档与有界工作区输入/输出存储 |
| Audit Governance | 消费脱敏持久状态事实并返回经过核验的入账回执；不复制对话正文 |
| Device Fabric | Hub 授权选机、资源准入、幂等提交、运行心跳、取消和结果回传 |

Snaplink 的登录 session、登录 device 与 Agent session、计算 Runner 分属不同域。
Hub 没有用户密码库，不签发人类用户令牌，不读取生态服务数据库。
初版授权由操作员配置明确的 tenant/subject/client_id → instance allowlist；
同时要求 access token 的 scope。尚未实现动态项目成员同步或 Snaplink
在线权限撤销推送，权限变更通过短期令牌到期和 Hub 配置重启生效。

## 持久化设计

业务模型为 Instance、AgentSession、Turn、SessionEvent；传输 DTO 不包含
数据库连接、Agent 对象、执行器路径或用户凭证。数据库表按 tenant 分区查询。

| 数据 | 身份与不变量 |
|---|---|
| Instance | `(tenant, instance_id)` 唯一；boot_id 标识进程；generation 用于拒绝旧 Gateway |
| AgentSession | 全局 ID 由 `(tenant, instance_id, local_session_id)` 派生；三元组另有唯一约束 |
| Turn | prompt 原文进入私有命令记录；turn 查询不返回 prompt，授权事件流含脱敏后的用户输入；每会话至多一个非终态 turn |
| SessionEvent | 稳定 event_id + 持久递增 cursor；同 ID 同内容复用，同 ID 异内容冲突 |
| Gateway receipt/outbox | 在执行前写入本地收据；事件先持久化后发送，网络重试保留 ID 和内容 |

Hub 在事务内创建命令、检查幂等键、推进状态并记录事件；SQLite 使用 WAL、
FULL synchronous 和私有文件权限。文件锁限制一个 Hub 进程拥有该库，
因此这不是多副本或跨地域数据库方案。列表使用有界分页，prompt 和事件大小
受限；事件历史在本机磁盘保存，目前没有自动归档/清理策略，运维需监控容量。

Gateway 心跳过期后，UI 显示实例离线，会话不再可控。新的进程只能在旧实例
离线后接管；generation 递增，旧执行变为 lost、旧会话变为只读。
断线和进程崩溃不自动重放可能产生副作用的 prompt。当前没有运行中 Agent
会话迁移、任意本地 TUI 进程接管或历史 provider 会话的隐式恢复。

取消排队 turn 会直接变成 cancelled；运行中的 turn 先变成 cancel_requested，
实际停止由 Gateway 的终态事件确认。取消与自然完成可以竞争，终态不会倒退。

## 本地试用

安装开发依赖（含可选 Hub JWT 验证器）：

```bash
pip install -e '.[dev,hub]'
pi-batch fleet init --directory ~/.config/pbatch/hub-development --instances alpha beta
pi-batch fleet serve --config ~/.config/pbatch/hub-development/hub.json
```

`init` 仅创建新目录，目录权限 0700，随机令牌文件 0600，输出只有路径。
development 认证仅允许 Hub 绑定字面量 loopback 地址；不能把这份配置用于远端监听。

在两个终端中启动不同 Gateway；把 cwd 和 agent 换成确实存在且已授权的项目与 CLI：

```bash
PBATCH_HUB_TOKEN_FILE=~/.config/pbatch/hub-development/gateway-alpha.token \
pi-batch fleet agent --hub http://127.0.0.1:8788 --instance-id alpha \
  --cwd /path/to/project-a --agent pi --sessions 1

PBATCH_HUB_TOKEN_FILE=~/.config/pbatch/hub-development/gateway-beta.token \
pi-batch fleet agent --hub http://127.0.0.1:8788 --instance-id beta \
  --cwd /path/to/project-b --agent pi --sessions 1
```

客户端设置单个 Hub 地址；Gateway 地址由 Hub 管理：

```bash
export PBATCH_HUB_URL=http://127.0.0.1:8788
export PBATCH_HUB_TOKEN_FILE=~/.config/pbatch/hub-development/user.token
pi-batch fleet instances --json
pi-batch fleet sessions --json
pi-batch fleet send SESSION_ID '分析这个项目的入口' --idempotency-key request-001
pi-batch fleet events SESSION_ID --follow
pi-batch fleet cancel TURN_ID
pi-batch fleet tui
```

重试同一个 prompt 时沿用 idempotency key；新 prompt 使用新 key。
CLI 发送前会把请求 key 输出到 stderr，确认丢失后可用该 key 重试；
远程 TUI 按会话保留相同提交的 key，最多保存 128 个待确认请求；达到上限时
拒绝新提交，先重试已有请求以释放位置。TUI 显示最近 60 条事件。完整历史可通过
`fleet events` 的游标逐页读取。
令牌仅接受环境变量或私有文件，不能放在 URL。网页/原生 Console 使用 Snaplink
登录获得 Hub audience 的 access token；开发令牌初始化不改变 Console 登录配置。

## Snaplink 与远端部署

生产配置样例：`examples/agent-hub.yaml`。先在 Snaplink 配置独立的 Hub resource、
客户端 allowed_scopes/allowed_resources、tenant 绑定及相应回调地址。
Hub 的 `grants` 必须匹配令牌中的真实 subject/client_id；示例占位值不会授予访问。
Console 请求 Hub resource 的 token，不能将给 IM 或 SSO admin 的 token 混用。

部署时建议选择固定 HTTPS resource URI，并让 Console 的
`SNAPLINK_AGENT_HUB_RESOURCE`、Snaplink 的 `allowed_resources` 和 Hub 的
`auth.audience` 完全一致。默认 `agent-hub` 与当前 Snaplink 实现兼容；
[RFC 8707](https://www.rfc-editor.org/rfc/rfc8707.html#section-2) 的 resource 参数要求绝对 URI。
Console 与 Gateway 客户端都必须配置非空 tenant，且共享 Hub 信任的 issuer/JWKS。
Console 用户的 `sub` 是 pairwise subject，不能直接用账户原始 ID 替代；
初版需为每个用户配置 grant，不会自动继承 tenant 成员权限。

用户 scopes：`agent.instances:read`、`agent.sessions:read`、
`agent.sessions:write`、`agent.turns:cancel`。
Gateway scope：`agent.gateway:connect`，一个机器主体只能绑定一个实例。
Gateway 不透传用户 token 给 AgentSession；provider 子进程的启动环境剥离
`PBATCH_HUB_*` 控制面变量，保留 provider 自身的模型服务凭据。

每个 Gateway 使用独立 confidential OAuth client，通过 `/token` 的
`client_credentials` grant 取得令牌，申请 `agent.gateway:connect` 与同一 resource。
该客户端必须允许此 grant/scope/resource；Hub gateway grant 的 `subject`、
`client_id` 均填机器客户端 ID，并绑定单个 instance。CLI/Gateway 从私有令牌文件
读取 access token，每次请求重新校验并读取该私有文件，支持部署侧原子替换令牌
而不重启会话。当前没有自动换取或刷新机器令牌的功能，需由部署侧维护。
若 Snaplink 开启全局 scope registry，还需注册上述五个 Agent scopes；
仅设置客户端 `allowed_scopes` 不够。启用计算时还需注册四个计算 scopes，
并更新用户与 Gateway 的令牌和 grant，详见 [计算配置](agent-hub/COMPUTE.md)。

### Aero IM 状态通知

`pbatch.hub_messaging.MessagingSink` 实现只出站的机器状态卡片适配器。它对
Hub 的持久 lifecycle fact 调用 `prepare(fact)`，仅接受终态计算任务和 turn
事件，并把固定接收 subject 与最小纯文本 Block 冻结在 Hub notification
outbox 中；`deliver(payload, delivery_id)` 只把 `target` 和 `blocks` 发给
IM，不发送 prompt、命令参数、Agent 输出或失败正文。事件字段只用于列出
状态、task/session/turn/instance/event 标识。IM 创建的是专用 bot 的普通状态
消息，继续使用消息 outbox 和跨设备实时扇出；通知消息本身不会被 Hub 当作
命令读取或执行。

Hub 在顶层 `integrations` 配置中按 tenant 配置独立 IM endpoint、integration
installation、0600 access token 文件及 `actor_subject -> IM OIDC subject` 显式映射。
例如：

```yaml
integrations:
  - kind: im
    name: tasks
    tenant_id: team-acme
    base_url: https://im.example.test
    installation_id: 00000000-0000-4000-8000-000000000001
    token_file: /etc/pbatch/aero-im.token
    recipient_subject_map:
      hub-pairwise-subject: im-pairwise-subject
    # Optional on first registration only. Omit to start at the current tail;
    # set to 0 to replay the durable event log from its beginning.
    start_cursor: 0
```

`start_cursor` 只在首次注册该 sink 时生效；默认从当前日志尾部开始，不补发历史。
两个 IdP 的 `sub`
可能是不同的 pairwise subject，不能直接复制或用 participant UUID 猜测。没有
映射的 actor 会得到永久 `recipient_unmapped` 错误；映射在 outbox 准备后发生
改变时，旧投递以 `recipient_binding_changed` 失败，不会悄悄改发给新用户。
Hub 自己不自动换取/刷新 IM token，部署端负责通过原子替换 token 文件轮换。

IM workspace 管理员须预先注册 Integration Installation：选定专用 bot、配置
Snaplink machine issuer/client_id 和 trusted human `user_identity_issuer`，并
启用 `allow_user_dm`。Hub 请求
`POST /api/integrations/v1/installations/{installation_id}/notifications`，使用
scope `aero.notify.publish` 的 machine access token；IM 服务端会用 installation
绑定的 issuer + human subject 映射 recipient，并按 installation workspace 检查
有效成员、阻断和信息屏障策略。Hub 不能在请求中自报 workspace 或 participant。

每条冻结通知使用 outbox 的稳定 UUID 作为 `Idempotency-Key`。IM 对相同目标和
Block 内容重放时返回原消息；同一 key 携带不同内容/目标会冲突。IM 幂等 receipt
保留 7 天，因此 Hub 只允许 first attempt 后 6 天内自动或手动重发；pending 409
须沿用同一 delivery ID 重试，其它授权/幂等冲突应按永久失败处理。

```bash
pi-batch fleet serve --config /etc/pbatch/agent-hub.yaml \
  --host 0.0.0.0 --port 8788 --cert-file /etc/pbatch/tls/server.crt \
  --key-file /etc/pbatch/tls/server.key
```

支持 EdDSA、RS256、ES256，JWKS 来源由操作员固定为 HTTPS，拒绝重定向，
有响应大小、key 数量、缓存寿命和请求超时限制。当前没有 DPoP、mTLS-bound
OAuth token 或委托 act-chain 的证明验证，携带这些约束的 token 会明确拒绝。
Hub 可置于可信同源反向代理后，通过 `/api/v1/agent/` 路由；
必须保留 Authorization 和 Idempotency-Key，并在 Hub 中配置精确 allowed_origins。

## API 契约与验收

详见 `docs/agent-hub/openapi.yaml`。所有业务响应为 `{data: ...}`，错误为
`{error: {code, message}}`，禁缓存。用户和 Gateway 使用不同路径及身份类型。
JSON polling 与有限 SSE replay page 使用相同持久 cursor；SSE 客户端通过
Last-Event-ID 重连。SSE 不是无限占用线程的长连接，客户端应按 retry 间隔重连。
事件页服务端上限 200，内置 Python 客户端和 CLI 上限 100。
Gateway 的消息正文每块最多 4 KiB UTF-8；每个运行时事件最多投影 256 KiB，
超出后设置 `truncated`。客户端按 cursor 排序、event_id 去重，按
`chunk_index/chunk_count` 拼接；`message.completed` 的第 0 块替换该消息的
delta 文本，后续块继续追加，并显式显示截断标记。单事件的序列化内容上限
32 KiB，内置 Gateway 客户端对完整事件请求体执行同一上限。
Gateway 事件回执还带当前 `turn_state`；重放同一 event_id 时该状态仍反映
当前 turn，可用于确认开始许可是否已被取消。

验收覆盖：两个实例拥有相同 local_session_id 仍不串会话；授权列表过滤；
读/写/取消分别授权；重复 prompt 幂等；错误 tenant/instance 被拒绝；
Gateway 换代 fencing；队列取消与运行中取消；消息游标重放；私有开发配置；
真实 JWT 验签、错误 audience/issuer/过期和 JWKS 轮换。
`tests/test_hub_integration.py` 通过真实 HTTP、SQLite、Gateway 与 AgentSession
验证两个实例路由和执行一次、运行中取消、开始确认丢失后的取消，以及取命令后
立即取消不阻塞下一个 turn；仅 provider 边界使用确定性替身，不调用付费模型。

工程门禁：`make ci`、`python quality.py --strict .`；
Console 运行其 Flutter 分析、测试与工程门禁。跨仓运行状态与实现证据应区别于
部署验收：未配置真实 Snaplink/远端主机前，不宣称已在用户设备上完成生态部署。
具体通过项、全仓既有样式问题和未执行项见
[`VERIFICATION.md`](agent-hub/VERIFICATION.md)。

## 后续里程碑

1. GPU 探测/调度已实现；真实 CUDA 工作负载和跨设备部署仍需验收。
2. P2 工作区增量已实现：Vault 固定版本快照、任务绑定输入与输出摘要校验，以及原生客户端文件选择/保存通道；大文件传输和自动源目录合并待实现。平台验证范围见 Console 的原生工作区说明。
3. P3 已实现：Aero ID 成员投影门禁、Aero IM 通知 outbox、Audit Governance 脱敏事件和 ledger 回执；生产部署另行验收。
4. P4：跨租户资源池策略、HA、区域路由和多控制面联邦。
