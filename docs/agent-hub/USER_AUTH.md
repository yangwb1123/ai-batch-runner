# CLI/TUI 用户设备登录

`fleet auth` 通过 Snaplink 设备授权取得短期用户 access token。终端显示确认地址
和用户验证码，用户在浏览器中完成批准；CLI 不接收密码、不监听回调端口。
登录后，会话、计算、工作区和集成查询命令共用同一份私有登录状态。
Gateway 使用独立的[机器认证](MACHINE_AUTH.md)。

## 配置与使用

在 Snaplink 登记专用 public OAuth client，认证方法为 `none`，明确允许
`urn:ietf:params:oauth:grant-type:device_code` grant、目标 resource、用户 scopes
和 tenant。应签发 Hub 支持的 JWT；Hub 的 issuer、audience、tenant、client ID
和用户 subject grant 仍需逐项匹配。开启全局 scope registry 时，也须登记 scopes。
此配置不会自动建立 Hub 权限、设备权限或项目成员资格。

将 [配置示例](../../examples/agent-hub-user-auth.json) 复制到私有配置目录，修改
服务地址及已登记的 client/resource：

```sh
mkdir -p ~/.config/pbatch
cp examples/agent-hub-user-auth.json ~/.config/pbatch/user-auth.json
chmod 600 ~/.config/pbatch/user-auth.json
unset PBATCH_HUB_TOKEN PBATCH_HUB_TOKEN_FILE PBATCH_HUB_OAUTH_CONFIG
export PBATCH_HUB_USER_CONFIG=~/.config/pbatch/user-auth.json
pi-batch fleet auth login
pi-batch fleet auth status
pi-batch fleet instances --json
pi-batch fleet sessions --json
pi-batch fleet tui
pi-batch fleet auth logout
```

`login/status/logout` 均接受 `--config /private/user-auth.json`；显式参数优先于
环境变量。供后续业务命令使用时，设置 `PBATCH_HUB_USER_CONFIG`。Hub 地址默认
来自配置；显式 `--hub` 或 `PBATCH_HUB_URL` 必须指向同一规范化 origin。
同时设置静态 token、token 文件或机器 OAuth 配置会被拒绝。Gateway 不接受
用户登录配置，请在其进程环境中使用独立机器凭据。

配置文件必须为当前用户拥有的普通文件，权限 `0400` 或 `0600`，最多 32 KiB。
`credential_directory` 相对配置文件目录解析，由登录命令创建或使用；每份配置
使用独立目录。目录为当前用户所有、权限 `0700`，路径不能经过符号链接。
目录内的 lock/state 文件为 `0600`，拒绝符号链接、硬链接和特殊文件。

配置只接受已定义字段。设备授权和 token endpoint 必须使用同一 origin，并配置
完整路径；远端使用 HTTPS，仅字面量 loopback IP 允许 HTTP。不进行发现、跟随
重定向或使用环境 HTTP 代理。浏览器确认 URL 默认必须属于这个 OAuth origin；
若部署独立确认页面，可显式设置 `verification_origin` 为其 origin。

示例包含四项会话 scopes。需要计算能力时追加 `agent.devices:read`、
`agent.tasks:read`、`agent.tasks:write`、`agent.tasks:cancel`；需要生态投递管理时
按职责追加 `agent.integrations:read`、`agent.integrations:retry`。实际名称以
Hub 的用户 scope 注册表为准。可另加 `openid`，但客户端不把 ID token 当作
身份或权限证明；至少需要一项 Agent scope。Gateway scope 和 `offline_access`
不属于用户配置的允许集合。

## 登录与恢复语义

- 终端只显示确认 URL 和用户验证码。`device_code`、access token、可选 refresh
  token、ID token 及原始 OAuth 错误正文不进入终端或运行日志。含 device code
  的显示字段会被拒绝。浏览器应显示本次请求的 client/resource/scopes 供用户核对。
- 设备授权最长等待 30 分钟，以原请求开始时的单调时钟截止；遵守服务端轮询
  间隔，`slow_down` 后累计增加 5 秒，网络故障降低请求频率。拒绝、失效、已消费
  grant 或无法解析的响应结束登录，不重新创建 device code。
- `Ctrl-C` 取消本地等待，保留此前登录。取消不会远程撤销设备授权；本进程
  不再轮询或保存它，授权按服务端期限失效。
- token 仅保存于私有状态文件，寿命最多 24 小时。每次 Hub 请求重新读取状态，
  因而另一个终端的登录、退出或失效会在下一次请求时生效。过期时需要重新登录，
  本轮不申请、保存或使用 refresh token，不提供无人值守的用户续签。
- 状态绑定 Hub、OAuth endpoints、确认 origin、client、resource 和 scope 集合；
  修改绑定后不能复用旧状态。请为新绑定使用另一个凭据目录，原配置用于清理
  原登录。`status` 只报告本地缓存是否有效，不请求 Hub，也不证明当前服务器授权。
- 登录以短事务登记一个待完成标识，网络等待期间不占用文件锁。后开始的登录
  或本地退出会使旧登录完成结果失效；退出后，迟到的授权结果不能复活会话。
  保存返回 I/O 错误时，可用 `status` 核对状态；原子替换后若目录同步失败，
  新完整状态可能已经落盘，不会通过回滚恢复旧 token。
- `logout` 清理本工具管理的 token 和待完成登录，保留用于防止旧写入的状态。
  它不撤销 Snaplink token、不退出浏览器账户，也不删除外部手动 token 文件。
  已经发出的请求仍由 Hub 正常处理；退出不回滚已接受的任务。
- Hub 401 仅清除该请求实际使用的 token，迟到的旧 401 不清除新的登录。
  业务请求不会自动重放；403 保留登录。TUI 保留 prompt 草稿和未确认请求的
  幂等键，提示登录或检查权限，重新登录后可刷新并沿原 key 重试。

## Snaplink 配套与边界

设备授权请求使用现有 `POST /device/code`，表单为 `client_id/resource/scope`；
轮询使用 `POST /token`，表单为设备 grant、`client_id/device_code`。resource
在设备授权时绑定，token 轮询不重新自报。Snaplink 将已消费或过期授权返回为
`invalid_grant`，客户端同时识别标准 `expired_token`。轮询策略依据
[RFC 8628](https://www.rfc-editor.org/rfc/rfc8628.html#section-3.5)及实际服务端契约。

Snaplink 配套修复将设备审批者的 pairwise subject 先解析为本地主体，再按目标
client 映射一次，避免重复映射导致用户身份变化或 UserInfo 查询失败。不同
sector/client 的最终 subject 仍可能不同，Hub grant 必须匹配目标 CLI 的真实
签发主体；不能直接复制另一个 client 的 pairwise subject。

当前内置设备确认页面只完成 password 登录直接返回 access token 的分支，尚未
覆盖所有 MFA 或授权码登录策略。使用这些策略时须接入已支持相应用户认证的
外部设备确认页面，并显式配置 `verification_origin`；本轮没有实现通用 SSO
批准页面，也没有修改 Console 的浏览器或原生登录流程。

私有状态依赖 POSIX 文件锁和 `O_NOFOLLOW`，不包含 Windows CLI/Gateway 适配。
HTTP 正文有大小限制和读取截止；DNS、连接、响应头仍沿用 socket 超时，不能
视为完整请求的硬截止。配置和凭据受文件权限保护，不隔离同一系统账户内的
其他进程。测试、配套 Snaplink 门禁和部署范围见 [验证记录](USER_AUTH_VERIFICATION.md)。
