# 只读 Web 接入：离线预检与观察型 Gateway

这是只读MVP的**接入工具**，不是本人授权、真实SSO或部署验收的替代品。
已有HTTP/customDomains路由无需为这两个入口重新配置。

## 1. 不启动服务的配置预检

```sh
abr fleet serve --config /private/agent-hub-web.yaml \
  --host 127.0.0.1 --port 8788 --check-config --json
```

预检与正常启动共用验证器：
- 有界、严格UTF-8 YAML（文件最多256KiB），拒绝重复键、merge键、循环/超预算别名、
  未知顶层/auth/grant字段、符号链接与特殊文件。其余文档加载器默认的合法merge行为不变。
- 验证认证声明、监听/TLS策略、Web配置和完整编译资源，及已配置Identity/Compute/Integration
  适配器的本地配置；外部投递sink的私有凭据缺失也拒绝。
- 返回退出码0/2及有界摘要，不打印subject、客户端ID、凭据、URL、配置正文或本地路径。
- **不监听、不创建/打开SQLite、不启动后台worker、不读取JWKS、不调用登录/token端点，
  也不注册实例、写grant或投递审计。** `--database`路径在预检中不被创建/打开。

`result: passed`仅指本地配置有效。报告的`external_acceptance.result`固定为`not_executed`：
未验证账号归属、已批准client、实际grant、边缘TLS/日志、端口是否空闲、数据库恢复或Gateway。
语法正确的示例值不是可用身份；`declared_tenant_read_policies`也不是允许租户全员访问的批准。

正常启动同样先完成上述检查，再创建数据库和监听。兼容性变化：Hub YAML不再容忍重复/merge
键或拼错的auth/grant字段；原来会在后台才发现的sink凭据问题提前阻断。请修正配置，不放宽检查。
`--json`只用于`--check-config`，不将运行日志变成成功JSON。
正常启动还会执行独立的[存储准入](LIFECYCLE.md)：先获取安全写锁，再只读核对数据库版本，
最后事务化初始化核心表。路径/所有权或版本被拒绝时不强破锁、删除WAL或改小版本号；
离线配置预检不访问这些文件，也不能替代该项准入。

## 2. 只登记实例和发送心跳

原来的`--sessions 0`仅表示没有初始会话，**仍允许轮询远程创建会话、关闭和执行命令**。
首次只读接入应显式选择观察模式：

```sh
abr fleet agent --observe-only \
  --hub https://abr.ywbsd.site \
  --oauth-config /private/abr-gateway-oauth.json \
  --cwd /home/u1/ai-batch-runner \
  --state-dir /private/abr-observer-state \
  --instance-id abr-local --project-id abr
```

机器客户端仍须独立登记、绑定正确tenant/resource和`agent.gateway:connect`；不是Web public
client，也不能用用户Cookie、示例secret或首次访客身份替代。凭据文件契约见[MACHINE_AUTH.md](MACHINE_AUTH.md)。

观察模式的实际边界：
- 必须显式指定Hub、已存在的固定cwd、项目/实例ID和项目之外的独立私有state目录。
- CLI省略会话参数时使用`--sessions 0 --max-sessions 0`；显式非零值或compute tools拒绝，
  不静默忽略。API调用`HubGatewayConfig`也须显式填零。
- 只POST登记和心跳；登记能力为0会话容量、不支持会话关闭。它不是零网络写入模式。
- 不创建AgentSession/调用provider、不轮询prompt/创建/关闭请求、不重放执行outbox，
  不扫描项目、读取文件正文或自动导入已有CLI/TUI会话。**空会话列表是正常事实，不伪造会话。**
- 目录中的持久绑定固定Hub、cwd、项目、实例和观察模式。不能用旧执行态目录开启观察，
  也不能将观察目录改成执行模式；升级权限需独立state和明确批准，不能删除收据来绕过。
- 沿用机器凭据续签、Hub在线所有权、generation fencing与本地单写者锁。只读不等于允许
  抢占其它Gateway；实例在线冲突仍拒绝，不能以`--observe-only`强制接管。

Gateway启动先获取目录锁，再打开SQLite；关闭数据库后才释放锁。私有目录/guard要求当前
用户拥有，拒绝公开权限、链接/硬链接锁和不安全数据库文件，不自动chmod接管未知锁。
这些是POSIX协作边界，不证明敌对同uid写入者隔离、Windows支持或长时间故障恢复。
失败保留state和可能的登记结果，不盲目重放未知注册，不将“已请求退出”写成已停机。

## 3. 实际上线仍需独立批准与验收

Hub CLI的SIGTERM/SIGINT退出、HTTP有界收尾与SQLite单写者保留规则见
[LIFECYCLE.md](LIFECYCLE.md)。它只管理自身Hub进程，不停止现场Gateway或替代存储恢复验收。

可先用预检发现本地配置问题，再在已授权现场启动观察型Gateway、登录Web核对真实实例。
生产配置和机器凭据不能放入Git；不要直接运行文档中的`/private/`占位路径。
本轮只实现并用隔离fixture验证入口，没有生成真实grant、注册Snaplink client、启动持续现场
Gateway或替换公网服务。最终准入仍按[WEB_RELEASE.md](WEB_RELEASE.md)逐项记录。
