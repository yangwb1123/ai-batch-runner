# 用户设备登录验证记录

日期：2026-09-14。范围为 Runner CLI/TUI 的设备登录、私有短期用户状态、
登录/退出并发语义，以及 Snaplink 设备审批的 pairwise 主体修复。
使用方式与部署限制见 [USER_AUTH.md](USER_AUTH.md)。

## 实现与测试环境

Runner 从上一轮已验证的 `ac1a61307d4271bb426a209070145d2c9a715047` 创建独立
worktree；主工作区中原有的机器认证暂存改动完整保留。完整 CI 对应
`80cc4b596a3d96bfe02251d24df38aa27fd13042`。Snaplink 使用另一个独立 worktree，起点为
`bfe4e32e8f75903f1043b0700e4ba13ffca63e5f`，身份修复对应 `ea6af9033a7eee19f8eae1de08e43177e5e2f734`；主仓原有的
客户端配置测试和部署配置改动保留。

Python 虚拟环境使用 3.12.9、pytest 9.1.1、PyJWT 2.14.0、cryptography 50.0.1，
以 editable 安装本次源码，使外部 worktree 启动的命令也使用同一入口。

## Runner 验收

| 检查 | 结果 |
|---|---|
| 设备 OAuth 协议 | 121 项通过，包含本地 HTTP 交换、轮询退避、截止、配置与响应边界 |
| 私有存储与 Provider | 65 项通过，包含真实多进程登录/退出竞争、原子写失败、文件替换及过期清理 |
| 认证组合回归 | 245 项通过，覆盖协议、存储、CLI、客户端和会话/计算 TUI |
| 共享用户客户端 | 6 项通过，覆盖登录、退出、重新登录、origin 绑定、401/403 及凭据清理失败时关闭响应 |
| 登录整链路 | 6 项通过，覆盖实签 JWT、HTTP、SQLite、Gateway 与 AgentSession |
| 打包/环境过滤/CLI/共享客户端组合 | 37 项通过，OAuth 用户示例随 wheel 分发 |
| 响应关闭修复组合 | 36 项通过，包含共享用户客户端、原有 HubClient 与真实登录整链路 |
| 最终 `make ci` | 通过，exit 0；3471 项测试、53 项规则 eval 及其余工程门禁全部通过 |
| 最终 `python quality.py --strict .` | 通过，exit 0；quality: OK |

专项组合存在重叠，并已包含在全量 CI 中，不能相加作为不同测试总数。

首次完整运行在审查发现响应关闭遗漏后主动中断，未算作通过。新回归先复现
401 后私有状态写入失败导致响应未关闭，再将凭据失效操作移入响应上下文；
固定持久化错误继续返回，不重放业务请求。修复组合通过后重新执行完整 CI。

整链路中，CLI 显示确认地址和用户验证码，授权完成后查询两个实例会话，重复
提交同一 prompt 只执行一次；本地退出使已存在的 HubClient 下一请求失效，
重新登录后同一个客户端恢复。真实 JWT 密钥轮换与受控 JWKS 读取延迟用于生成
迟到的 401，确认旧拒绝不会清除新的登录；403 保留凭据。拒绝新登录保留旧会话，
Ctrl-C 清理自己的待完成登录，含 device code 的显示字段不进入终端。

新增认证测试中的 OAuth issuer、JWKS 获取、模型执行及等待时钟使用可控替身；
HTTP 请求、JWT 签名与 claims 校验、Hub 授权、SQLite、Gateway、AgentSession、
CLI 和私有文件均使用产品实现。这些测试不等于实际 Snaplink 浏览器批准部署验收。
全仓 CI 包含 `tests/test_tui_integration_codex.py` 的已有真实 Codex CLI 测试；
本机安装了 Codex，该路径会使用已配置账户的模型设置。不能把完整 CI 描述为
完全没有模型服务调用；本轮未核验模型计费。

## Snaplink 验收

生产修改仅在设备批准时调用已有 `resolveLocalSubject`，将得到的本地主体保存
到 device grant，再由目标 client 正常映射。后端解析错误或空结果返回
`invalid_token`，设备授权仍待批准。共享 resolver 对 `ErrPairwiseUnknown`
继续沿用原行为，本轮没有改变未知映射策略。

红测复现了三种 pairwise 审批路径主体变化及 UserInfo 失败，以及解析服务出错
仍批准的错误。修复后的五种 public/pairwise 同 sector、跨 sector 组合，以目标
CLI 直接登录的真实 JWT subject 为对照，验证 JWT 与 UserInfo 指向同一用户。
故障用例同时检查存储未批准、poll 仍 pending，并确认服务恢复后可以批准。

| 检查 | 结果 |
|---|---|
| 修改前缺陷复现 | 已复现，见 red 日志 |
| 定向 race 回归 | 31 项顶层测试通过，exit 0 |
| `go build ./...` 与 `go vet ./...` | 通过，exit 0 |
| 维护性/架构门禁 | 未通过，exit 1；既有 docs 目录预算问题 |
| 完整 `go test ./... -race` | 未通过，exit 1；238 个包通过，失败分类见下文 |
| E2E | 5 项通过，exit 0 |
| `make -k ci` | 未通过，exit 2；race 与 SDK 漂移目标失败，其余目标已执行 |

三类失败均已在未包含本轮修改的基线 HEAD 独立复现：

- `TestArchitecture_DirectorySubdirFanout`：docs 计入 16 个目录，预算为 15。
- `cmd/sso-ctl/apiclient` 的 8 项及 `clientscmd` 的 1 项既有测试：revoke 返回 401。
- SDK 生成漂移：TypeScript/Python introspection 类型缺少生成器要求的 `tenant_id`。

没有通过放宽门禁、移动目录或改动无关撤销接口消除这些失败。`make -k ci`
继续执行其余目标，但保留失败退出码；fmt/vet/build/examples、Proto lint、所有
nested modules、现有配置校验、模块 smoke、路由契约、Proto/OpenAPI 一致性、
capabilities/SDK surface、profiles evidence 和 adapters 检查均完成。
门禁自身报告跳过不存在的 `bin/config.yaml` 与不存在的 SDK deploy static tree，
这些跳过不算部署验证。

另有隔离 worktree checkout 的 umask 将公开 fixture
`ops/deploy/audit-provisioner/desired-state.example.json` 生成为 `0664`，触发私有
配置加载器拒绝。仅在隔离目录收紧为 `0644` 后，整个对应包的 race 复测通过；
未改文件内容、Git 可执行位或主仓权限，未在此环境修正后重跑整套 race/CI。
原始整套日志仍保留此失败，不能将它们改记为通过。

本次修改涉及的完整 `interfaces/sso`、`internal/handler/tokengrant` 与 `test` 包
在全量 race 中通过；独立审查未发现本次修复新增的阻断。完整服务发布仍需处理
上述基线门禁问题。

## 留存与未执行范围

本轮没有注册或修改真实 OAuth 客户端、账号、权限，也没有启动生产 Hub/Gateway、
部署服务或向真实生态账号发送通知。没有跨公网、物理设备、Windows CLI 或新的
Console 原生平台验收。内置设备确认页的 MFA/授权码分支限制与外部确认 origin
配置见使用说明；用户 refresh token、远程撤销和浏览器账户登出不在本增量内。

私有状态的 rename 前写入失败保留旧状态；rename 后目录 fsync 失败会返回固定
错误，但磁盘可能已是新完整状态，不会为了回滚而重新写入旧 token。这是本地
提交持久性不确定的边界，不能把错误返回解释为旧凭据必然保留。

原始日志与源码/应用收据位于本机 `/tmp/hub-user-auth-20260914/`。主要文件为
`user-auth-targeted.log`、`packaging-client-tests.log`、`ci.log`、`strict-quality.log`、
`verification-results.json`、`response-close-red.log`、`response-close-green.log`、
`verification-before-response-close.json` 和 `snaplink-validation.json`、`snaplink-targeted.log`、
`snaplink-race.log`、`snaplink-ci.log`、`snaplink-baseline-gates.json`、
`snaplink-sdk-drift-baseline.log`。临时路径用于本机核对，
不是产品配置或永久部署资产。
