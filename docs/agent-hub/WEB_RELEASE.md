# ABR Web：发布准入（只读与任务控制分别验收）

用户是浏览器中的项目操作者。默认只读配置保持兼容；显式控制配置、本人精确授权及
执行型Gateway就绪后，支持创建会话、发送指令、查看事件及取消/关闭，见 [WEB_TASKS.md](WEB_TASKS.md)。
不承诺任意文件浏览或自动接管已有项目进程。登录成功、页面200和空窗口不构成验收。
控制增量需要新的完整候选；此前只读镜像、扫描和测试结论不覆盖新代码。

## 1. 候选制品门禁（开发侧可执行，不发布）

使用隔离Python开发环境、`web/package.json`固定的Bun，以及`iris-source.json`固定的干净Iris源码：

```sh
cd web
bun install --frozen-lockfile --ignore-scripts
bun run build
cd ..
make web-ci
python quality.py --strict .
python -m pytest checks/
git diff --check
```

- `make ci` 含标准库源码漂移检查；`make web-check`额外执行类型、Bun测试及完整字节重建比较。
  检查模式不回写资产，不以重新生成清单掩盖已检查的旧包。
- `make web-browser`需要真实Chrome/Chromium和Playwright；缺工具失败，不静默skip。
  本地自签名测试TLS、合成issuer/响应仅验证代码路径，不能替代下面的实网检查。
- `.github/actions/verify-web`被PR CI及两条发行路径强制调用。Actions只读checkout Iris、使用
  冻结锁文件和固定工具，不需要Web secret、用户token或注册管理凭据；工作流是否真正通过以
  该次运行记录为准，不用本地测试代称远端成功。
- 运行时资源清单v2为内部编译资产格式，不改变HTTP只读API、Cookie或授权配置；新loader与
  新清单须随同一候选整体更新，不混装。Python运行端不需要Bun/Iris checkout。
- 发布候选必须绑定确切源码与资产摘要、运行依赖/镜像及扫描时间；本机hash不是独立签名，
  更不能将旧镜像、旧漏洞库或另一个Python环境的扫描结果归给新候选。

## 2. 真实只读接入验收（现场证据目前仍待补）

现有接入入口：`fleet serve --check-config --json`可先做无网络/数据库的配置检查；
`fleet agent --observe-only`可在授权后仅登记固定项目实例和心跳，不启动Agent或轮询命令。
操作与限制见[READ_ONLY_ONRAMP.md](READ_ONLY_ONRAMP.md)。两者都不自动完成下面的实网验收。

| 验收项 | 通过条件 |
|---|---|
| 本人身份 | 真实Snaplink登录；验签、issuer/audience/tenant/client/subject与本人明确确认匹配 |
| 授权范围 | 明确单账号、单实例/项目读取；不能拿样例、第一位访客或租户全员策略替代批准 |
| 数据闭环 | 独立Gateway身份、固定工作区；本人能看登记实例/会话，不读任意文件或正文 |
| 拒绝边界 | 其他账号、错误tenant/client/实例被拒；Cookie不能调用Bearer或写入接口 |
| 会话边界 | state/回放、过期、退出、刷新后失权和旧数据清除，浏览器无token持久化 |
| 入口与日志 | 外部HTTPS、同源Cookie/CSP真实生效；query/code、Cookie、Authorization不进入边缘/应用日志 |
| 生命周期 | 持久单写者服务；重启需重新登录；备份/恢复、升级失败和回退按真实存储验证 |

沿用已确认的 **HTTP + customDomains → loopback 127.0.0.1:8788**，不以此清单要求额外CA、
SSH、K8s或再次修改FRPC。传输身份的未决证据与路由分开记录，不用公网200追认其通过。

Hub的信号处理、请求收尾与事务锁保留已提供本地实现及隔离回归，见
[LIFECYCLE.md](LIFECYCLE.md)；它不是已安装持久服务、完整停止期限或现场恢复通过的证明。

切换服务前先核对现有8788拥有者、临时控制器及其自动恢复行为，不能盲用历史PID、重复兑换
OAuth code或重复执行旧操作脚本。单独审阅持久配置，原子切换整套候选；不修改在线安装目录
来运行`bun run build`，不拷贝活动SQLite/WAL冒充一致性备份，也不通过删数据库回滚。

本轮没有获得新的本人身份确认、许可结论或发布授权；这些是**待确认的外部决策/验收**，
不能由代码、合成fixture、构建清单或本文件自动代填。

## 3. 任务控制附加验收

只读验收不能替代执行批准。须另核对实际Web client允许的写/取消scope、本人精确grant、
独立机器身份、固定工作区、目标项目AGENTS/预算及执行型Gateway；不得把观察模式原地升级。
真实项目需验证创建→指令→事件→取消/关闭，响应丢失不重复执行、另账号/实例拒绝、
账号切换不重放、Gateway换代与容量释放。Cookie仍不能调用原Bearer API。
`make web-browser`增加真实Chrome/Hub/Gateway/AgentSession链路，但其issuer/provider仍为合成替身，
不代表已批准真实模型调用、仓库写入或线上服务切换。

## 4. 发布裁决

- **本地演示候选**：可以用明确标注的合成数据验证体验，不对外宣称真实项目接入。
- **受限只读试用**：候选门禁完整通过，且真实身份、权限、安全和持久服务检查全部有证据后，
  由维护者明确批准目标账号/项目和部署操作。审计未接入必须显示未追踪，不能声称已入账。
- **公开分发**：还须满足[发行契约](../RELEASING.md)，包括上游许可、相应渠道名称所有权、
  干净可追溯源码和受保护的发布流程；PyPI名问题只约束该分发渠道，不是HTTP路由前提。

记录每项实际结果 `passed` / `failed` / `not_executed`，未执行写明原因；完整CI超时或任何必需
项缺证据均不批准发布，不将多个局部复跑拼成全量PASS。执行成功不代表生产授权。
