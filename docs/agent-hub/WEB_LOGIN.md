# ABR Web：Snaplink SDK 登录（首个只读切片）

## 范围与验收边界

面向需要浏览器查看授权实例和会话的操作员。可选配置 `web` 后，现有
`abr fleet serve` 同时提供首页、登录回调和只读 facade；不另建执行或权限内核。
本切片没有网页创建会话、发送 prompt、取消、文件读取或运维修复按钮。
`ops serve` 的静态 token 模式保持不变，不把它直接穿透为多用户 SSO。

使用官方 **`snaplink-sso==0.3.0`** Python SDK 的 `Snaplink.login()`：托管登录页、
state、S256 PKCE、issuer 回调检查及授权码兑换均由 SDK 实现。没有复制 SDK、
自写密码登录或把账号密码交给 ABR。SDK 的 ID token 不作为 Hub 授权依据；
access token 仍由既有 Hub JWT/JWKS 验证器验证，再检查 tenant、subject、client_id、
scope、instance grant 和可选 membership gate。

回归使用真实 SDK、真实签名 JWT、本地 HTTP/SQLite，**issuer 是测试替身**。
这不代表 `abr.ywbsd.site` 的真实浏览器登录、FRP/TLS、审计入账或生产验收。

显式浏览器烟测：在隔离开发环境安装 `playwright` 并提供本机 Chrome/Chromium 后运行
`python -m pytest tests/browser_hub_web.py`（不在默认测试收集中，不自动安装浏览器）。
它验证真实浏览器的表单 Origin、CSP 跨 issuer 跳转、Cookie 和本地退出；身份服务仍为
模拟。仅此测试上下文忽略自签名本地证书，生产服务和 SDK transport 不关闭 TLS 验证。

## 安装与配置

使用独立 Python 环境，从已核对的源码安装（不依赖 ABR 已获得 PyPI 名称）：

```sh
python -m venv /path/to/abr-venv
/path/to/abr-venv/bin/python -m pip install '/path/to/ai-batch-runner[web]'
/path/to/abr-venv/bin/abr fleet serve --config /private/path/agent-hub-web.yaml
```

模板：[`examples/agent-hub-web.yaml`](../../examples/agent-hub-web.yaml)。
没有 `web` 时行为不变，不要求安装 Snaplink SDK；配置了 `web` 但缺 SDK、字段错误、
使用 development token、非 HTTPS public origin 或缺精确 allowed origin 都启动失败。
无需添加 Web client secret。依赖是可选 `web` extra；`dev` 安装真实 SDK以运行契约测试。

在 Snaplink 经授权注册专用客户端，不读取或直接修改它的数据库：

| 项 | 值/要求 |
|---|---|
| Web client | `abr-web`，public，绑定明确 tenant |
| Grant | `authorization_code`，强制 PKCE S256 |
| Callback | `https://abr.ywbsd.site/auth/callback`，精确匹配 |
| Scopes | `openid agent.instances:read agent.sessions:read` |
| Resource | `https://abr.ywbsd.site/api/v1/agent`，与 Hub `auth.audience` 一致 |
| Hub grant | 同 tenant、真实 Web pairwise subject、`client_id=abr-web`、明确实例列表 |

开启全局 scope registry 的 Snaplink 还须注册 scope；仅写客户端 allowlist 不够。
SDK 使用 issuer 下的现有 Console `/login/`，登录 UI 应与部署版本匹配。
模板占位 subject 不会自动匹配当前管理员或所有租户成员；不得以 `*` 猜测授权。
Gateway 使用独立 confidential client，仅绑定一个固定工作区和实例，配置见
[MACHINE_AUTH.md](MACHINE_AUTH.md)。没有在线 Gateway/登记会话时，授权窗口可能为空，
不能把这个空窗口当作目标项目已经接入。

## 路由与浏览器安全边界

| 路由 | 行为 |
|---|---|
| `GET /` | 无凭据/租户数据的静态首页，登录和本地退出表单 |
| `POST /auth/login` | 必须同源；建立独立 SDK 事务，303 跳转 Snaplink |
| `GET /auth/callback` | 一次性消费浏览器事务，SDK 校验并兑换；Hub 验签和授权后轮换 Cookie |
| `POST /auth/logout` | 必须同源；仅清除本地 ABR SDK 会话，不调用全局 SSO logout、不取消任务 |
| `GET /web/api/instances` | 每请求重新鉴权，复用 HubService 实例过滤 |
| `GET /web/api/sessions` | 每请求重新鉴权，复用 HubService 会话过滤 |

facade 只允许上述读取；原 `/api/v1/agent/*` **仍只接受 Bearer**，Cookie 不成为
原有写 API 的凭据。Web 不接受混合 Authorization/Cookie 身份，不接受任意代理 URL。
Host 必须与固定 public origin 一致，不信任用户提交的 `Forwarded`/`X-Forwarded-*`。
POST 缺失 Origin、Origin 为 null 或跨源均拒绝。授权码只在回调 query 中短暂出现；
303 返回固定 `/`，无开放重定向；回调 query 重复/未知字段、URL 超限均拒绝。

Cookie 为 `__Host-abr_session`，`Secure; HttpOnly; SameSite=Lax; Path=/`，无 Domain。
浏览器不接收 access/refresh token，不用 localStorage/sessionStorage 储存凭证。
响应 no-store；API、回调和重定向使用 no-referrer。仅无凭据、无 query 的静态首页
使用 same-origin Referrer-Policy，让原生表单发送真实 Origin（Chrome 在 no-referrer 下
发送 null）；仍不向外域发送 Referer。hash CSP 的 form-action 只允许自身和固定 issuer
origin，以允许托管登录的 303 跳转；禁止嵌入及 HTML 字符串注入。
公网反代也必须**禁记回调 query、Cookie 和 Authorization**，不能只依赖 Hub 不记访问日志。

每浏览器独立 SDK 实例和 state store；单 Hub 进程内最多 256 个事务/会话。
登录事务 10 分钟，登录后绝对会话上限 30 分钟，同时受 JWT 到期约束；读取不续期。
满额拒绝，不驱逐其他用户的活跃会话。服务重启清除会话，需要重新登录。
不做静默 refresh，不重放兑换结果未知的授权码；网络故障需重新开始登录。
SDK transport 复用 Hub 的 32 KiB JSON 响应/5 秒读取期限限制，禁重定向和环境代理，
拒绝重复 JSON 字段；只允许固定 issuer 的 `/token`。DNS/系统调度不是硬实时保证。
该实现不是跨进程/多副本会话存储，也不提供即时全局 SSO 撤销订阅。

## `abr.ywbsd.site` 分阶段部署

1. 注册 Web/Gateway client、核对真实 pairwise subject、明确允许用户和工作区；保管机器
   secret 的私有文件不入 Git/argv/日志。不得为了取得 subject 给 Web 发放 admin scope。
2. 独立 Hub 数据库与运行目录；先启动 loopback listener，反代保留正确 Host。
   HTTPS 在可信边缘终止，FRP 链路须核验认证、加密和服务器身份；不得暴露可绕过边缘的端口。
3. 配置域名证书及专属 FRP 路由。现场 `/opt/frp/frpc.toml` 为共享服务，修改前保存私有备份、
   校验并安排窗口；当前无 reload 服务，重启会影响其它代理。不要覆盖家目录另一份配置。
4. 真实浏览器验收：登录/拒绝、跨浏览器 state、回调重放、越权实例、JWT 到期、本地退出、
   旧 Bearer API 不接受 Cookie、代理日志脱敏及入口绕过防护。
5. 审计另按 [AUDIT.md](AUDIT.md) 配置 source/schema/专用服务身份及 outbox。
   现场审计栈不可用时不能标记已接入；入账须核对真实 ledger receipt，202 不是入账。

既有 Hub 审计只覆盖其已支持的持久生命周期事实，本切片**未新增登录失败、授权拒绝和
本地退出审计事件**；审计也不是执行前同步门禁。启用审计、事件覆盖或要求故障阻断执行
需要独立确认。后续网页控制仍须显式授权、幂等、副作用确认和现有 Hub 门禁，不由登录隐式开启。
