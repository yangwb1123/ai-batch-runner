# Gateway 机器令牌自动续签

Gateway 可以使用 Snaplink 的 `client_credentials` 获取短期机器 access token，
并在后续 Hub 请求前自动续签。心跳、命令轮询和活跃 turn 的计算代理共用同一个
进程内凭据缓存；续签不创建新的 Gateway generation 或 AgentSession。

## 配置与启动

在 Snaplink 预先登记专用 confidential client：

- 认证方法为 `client_secret_basic`，允许 `client_credentials`。
- client 绑定明确 tenant，签发可被 Hub 验证的 JWT；允许目标 Hub resource 和
  `agent.gateway:connect` scope。若开启全局 scope registry，也须登记该 scope。
- Hub 的 gateway grant 使用此 client ID 作为 `subject` 和 `client_id`，绑定
  唯一实例。签发者、audience、tenant 和实例授权仍由 Hub 验证。

把 [配置示例](../../examples/agent-hub-gateway-oauth.json) 复制到私有配置目录，
将占位值改成已登记的客户端与服务地址。例如 `/etc/pbatch/gateway-alpha.json`：

```json
{
  "hub_url": "https://hub.example.test",
  "token_endpoint": "https://sso.example.test/token",
  "client_id": "gateway-alpha",
  "client_secret_file": "gateway-alpha.secret",
  "resource": "agent-hub",
  "scopes": ["agent.gateway:connect"]
}
```

配置和 secret 文件必须是当前用户拥有的普通文件，权限为 `0400` 或 `0600`，
不能是符号链接或 FIFO。配置最多 32 KiB；secret 最多 16 KiB，必须是非空 UTF-8
单行，**不带末尾换行**。secret 字节不会被 trim，空白之外的有效 secret 内容会
原样用于认证。相对 secret 路径相对于配置文件所在目录解析；不要把真实 secret
写入版本管理、命令参数或环境变量。

```sh
python pi-batch.py fleet agent --hub https://hub.example.test \
  --oauth-config /etc/pbatch/gateway-alpha.json \
  --instance-id alpha --cwd /srv/project-one --project-id project-one
```

也可以使用 `PBATCH_HUB_OAUTH_CONFIG=/etc/pbatch/gateway-alpha.json`；显式
`--oauth-config` 优先。使用自动机器认证时必须移除 `PBATCH_HUB_TOKEN` 和
`PBATCH_HUB_TOKEN_FILE`，否则在连接前拒绝，不按优先级猜测身份。没有 OAuth
配置时，原来的环境 token 和每次请求重读私有 token 文件方式保持兼容。

`hub_url` 必须与客户端访问的 Hub origin 相同，默认端口和主机大小写按规范化
origin 比较。`resource` 是 Snaplink 登记的 audience 标识，当前可使用 `agent-hub`，
不会从 Hub 地址自动推导。`token_endpoint` 是显式配置的完整 URL，可包含路径前缀；
不进行发现、不跟随重定向、不使用环境 HTTP 代理。远端使用 HTTPS；只有字面量
loopback IP 可以使用 HTTP。本地测试不等同于生产身份系统部署。

若开启 `--compute-tools`，在上述配置的 `scopes`、Snaplink client/resource 和
Hub gateway grant 中同时追加已授权的计算 scopes：`agent.devices:read`、
`agent.tasks:read`、`agent.tasks:write`、`agent.tasks:cancel`。按
[计算配置](COMPUTE.md) 设置 `compute_instances` 与项目设备映射。续签不会扩大
发起用户与 Gateway 的权限交集，也不会让普通 IM 消息变为执行命令。

## 续签、轮换与失败恢复

- token 仅存在进程内存，不创建 token 缓存文件。多线程同时需要续签时，只有
  一个请求访问 token endpoint，其他请求复用结果。有效期使用单调时钟，从获取
  请求开始计算，避免把网络耗时额外算成 token 寿命。
- 接受 `Bearer` 与 1–86400 秒的整数 `expires_in`；在寿命的最后 10% 内续签，
  提前量最多 60 秒。请求若几乎耗尽有效期，会限制缓存窗口，避免立即重复获取。
  成功响应若包含 `scope`，必须与请求集合一致；未知扩展字段不保存、不使用。
- 网络错误、429、5xx 采用 1、2、4、8、16、30 秒上限退避。只有旧 token 尚未
  到期时才允许在这类临时故障中继续使用。过期 token、无效 secret、认证拒绝或
  无效 token 响应不会被当作可用凭据。响应体有 32 KiB 上限及读取截止时间。
- Hub 返回 401 时，仅使该请求使用的 token 缓存失效，并向原调用者返回错误；
  **不立即重放业务请求**。该 token 被拒后冷却 5 秒，期间不重新签发；之后的调用
  才重新获取凭据。迟到的旧 token 401 不会删除已续签的新 token 或延长冷却。
  403 不触发重新授权。Gateway 原有事件 outbox、开始许可和
  幂等收据负责恢复，避免重复执行 prompt。
- 每次重新获取 token 时重读 secret 文件，支持部署工具原子替换文件。client ID、
  endpoint、resource、scopes 和 Hub 绑定在进程启动时固定；更换这些配置需重启。
  secret 轮换后，已有有效 token 可以继续使用至续签点；这不是立即撤销机制。
- 临时认证服务故障超过 token 有效期时，Gateway 可能显示离线；凭据恢复后由
  现有循环继续通信。若实例已被其他进程接管，仍按 generation fencing 停止，
  不绕过接管规则。首次启动无法取得凭据时返回失败，由部署侧决定进程重启策略。

机器配置使用 `PBATCH_HUB_*` 命名空间，现有 Provider 环境过滤会移除这些值。
token、client secret、原始 OAuth 错误响应不会进入用户提示、Hub 事件或运行日志。
这不隔离同一操作系统账户可读取的文件；Agent 仍必须运行在受信任的设备身份下。

当前私密文件读取依赖 POSIX `O_NOFOLLOW`，缺少该支持的平台会拒绝加载；这没有
补齐 Windows Gateway。HTTP 连接和响应头沿用 socket 超时，响应正文另有单调时钟
读取截止检查；不能将其描述为覆盖 DNS、连接和首部阶段的完整请求硬截止。

## 协议依据与范围

实现按 Snaplink 实际 `POST /token` 契约发送 Basic 认证和表单
`grant_type=client_credentials`、`resource`、`scope`。tenant 来源于注册 client，
不会增加 `X-Tenant` 头或自报 tenant 字段。对应源码是 Snaplink 的
`protocols/oauth/oauthwire/client_creds.go`、`interfaces/sso/server_token_clientauth.go`
和 `internal/handler/tokengrant/token_client_credentials.go`。

当前 Snaplink 的 Basic 认证直接解码 UTF-8 `client_id:secret`，不做额外 form-percent
解码，本客户端与该行为保持一致，并拒绝带冒号的 client ID。它不是可配置任意认证
方法的通用 OIDC 客户端。成功响应中的 `token_strategy` 等扩展按
[OAuth 2.0 成功响应规则](https://www.rfc-editor.org/rfc/rfc6749.html#section-5.1)
忽略；续签通过重新执行机器 grant 完成，不保存或使用 refresh token。

本轮仅给 Gateway/HubClient 增加机器凭据来源。人类用户登录、Aero ID/IM/Vault/Audit
集成的独立凭据、Fabric HMAC 身份、DPoP/mTLS-bound token 和 Windows Gateway
文件锁适配均不在此增量中。后续验收结果见 [验证记录](MACHINE_AUTH_VERIFICATION.md)。
后续 CLI/TUI 人类用户设备登录增量见 [USER_AUTH.md](USER_AUTH.md)。
