# Gateway 机器认证验证记录

验证日期：2026-09-14。范围为 Runner 的机器 token 获取、续签、凭据轮换及
Gateway 现有会话恢复路径。配置方式见 [MACHINE_AUTH.md](MACHINE_AUTH.md)。

## 代码与环境

本轮从 Runner `2a2ad9ec79ce8e9dba3099289796f50b457c4c04` 开始，在独立
worktree 中实现。修复打包故障前的 CI 对应 `fccab0776e32f4e9d7eef982347b437da9a8e2fb`。
其后合并主仓并行产生的 i18n 门禁提交，并修复资源定位故障；最终组合代码的 CI 对应
`d4117670b70bb67fd209ba87def8d3c1d8c60559`。本记录仅追加验证结果，不改变已测试的运行代码。

使用独立 Python 3.12.9 环境，pytest 9.1.1、PyJWT 2.14.0、cryptography 50.0.1。
仓库以 editable 方式安装，`PATH` 指向对应虚拟环境，保证临时外部 worktree
启动的 `pi-batch` 也是同一份源码。没有改写系统级 Python 安装。

## 检查结果

| 检查 | 结果 |
|---|---|
| 改动前 Hub 客户端、CLI、Gateway 与集成基线 | 23 项通过 |
| 最终认证专项组合 | 201 项通过，包含 139 项 OAuth provider 测试、7 项机器认证集成测试，以及客户端、CLI、环境过滤与 Hub 授权回归 |
| 修复打包故障前的 `make ci` | 3,243 项通过、1 项 wheel 安装资源定位测试失败；不是完整通过 |
| 打包修复专项 | 18 项通过，含 7 项路径回归、OAuth 配置示例分发及现有真实 wheel 构建、安装、验证器执行 |
| 合并最新主仓后的 `make ci` | 通过：3,256 项回归、56 项门禁自测、53 项规则评估；8 项工程门禁、发布、组织规范、能力注册表及依赖图检查通过 |
| 最终组合 `python quality.py --strict .` | 通过 |
| `bash -n scripts/sverp/check-engineering-gate.sh` | 通过 |

专项测试已经包含在全量回归中，不能把两组计数相加当作不同测试。

专项命令：

```sh
python -m pytest tests/test_hub_oauth.py \
  tests/test_hub_machine_auth_integration.py tests/test_hub_client.py \
  tests/test_hub_cli.py tests/test_provider_env.py tests/test_hub_auth.py -q
make ci
python quality.py --strict .
bash -n scripts/sverp/check-engineering-gate.sh
```

首次全量测试发现隔离环境未安装当前仓库的 CLI 入口，导致外部 worktree 的
模块导入和 installed-console-script 两项失败。修复 editable 安装后，两项
复测通过；原运行已中断，不能算作完整通过。

随后完整回归暴露 `pip --target` 资源定位故障：Python 的包元数据接口过滤了
搬移后已失效的 RECORD 路径，导致包旁 `share/ai-batch-runner` 的回退查找没有
执行。将包旁候选移出 metadata 文件循环，保留原有完整目录 marker 检查。
新回归先在旧代码上复现失败，修复后连同真实 wheel 用例通过。发现此原因时，
另一个尚在运行的组合 CI 已停止并保留中断证据。随后发布清单核对补齐了新增
OAuth JSON 示例的 wheel 分发和必含文件断言；该修改也在新一轮完整验收启动前
完成，提前停止的运行均单独留痕。最终重新执行全套检查。
没有删除用例、跳过失败或放宽门禁。

## 行为证据

- 私有配置与 secret 的权限、归属、符号链接/FIFO、文件大小、重复 JSON 字段、
  URL、origin 绑定和 token 响应校验都有回归；配置冲突在访问网络前拒绝。
- 本地 HTTP token endpoint 验证真实 Basic 和表单请求；覆盖单次并发续签、
  单调时钟有效期、临时错误退避、过期拒用、原子 secret 轮换、禁止重定向、
  代理隔离、响应大小及正文读取截止。自签名 TLS 负例验证证书失败时未发送
  HTTP 请求和 Basic 凭据。
- 7 项集成测试经过真实 Hub HTTP 服务、SQLite、JWT 签名与 claims 验证、
  Gateway、AgentSession 和幂等收据。两个机器主体保持实例隔离；续签不会
  更换 generation 或 session；secret 错误后队列保留，修复后继续执行一次。
- 已处理业务写入后返回 401 的注入场景确认客户端不自动重放写入，原有
  Gateway 收据和 outbox 可恢复。401 后冷却 5 秒，迟到的旧 token 拒绝不清空
  新缓存或延长冷却。403 不触发 token 失效。
- 真实 AgentSession adapter 启动本地 Python 子进程，验证 `PBATCH_HUB_*`
  不进入 agent 环境。token、secret 和原始 OAuth 错误正文不出现在对外错误中。

集成测试中的 OAuth issuer、JWKS 获取和模型运行使用可控替身。JWT 使用真实
Ed25519 签名，验签和授权使用产品代码。issuer 的 JWT 寿命为 60 秒，响应中的
provider 测试寿命为 10 秒，配合注入的 provider 时钟触发续签，避免依赖长时间
等待；这不是线上 issuer 的真实时间推进验收。

## 范围与留存

未部署或连接实际 Snaplink OAuth client/resource/grants，未启用真实机器凭据，
也未进行跨公网或物理设备验收。新增机器认证测试使用模型替身；完整 `make ci`
包含已有真实 Codex CLI 测试，本机安装 Codex 时会使用配置的账户和模型，
因此完整 CI 不代表没有模型服务调用，计费未核验。本轮未改变 Console、Aero
ID/IM/Vault/Audit 的独立凭据、人类登录和 Fabric 身份协议。

机器私密文件读取仍依赖 POSIX `O_NOFOLLOW`，未补齐 Windows Gateway。
HTTP 连接和响应头阶段沿用 socket 超时；正文另有单调时钟读取截止，不能将
本轮验证解释为覆盖 DNS、连接、首部阶段的完整请求硬截止。

本机原始日志位于 `/tmp/hub-machine-auth-20260914/`：`baseline-tests.log`、
`final-targeted.log`、`ci.log`（首次中断）、`environment-recheck.log`、
`ci-final.log`、`ci-final-result.json`（打包修复前）、`packaging-fix.log`、
`ci-merged-interruption.json`、`ci-merged-example-interruption.json` 和
`ci-merged-make-ci.log`、`ci-merged-strict-quality.log`、`ci-merged-sverp-shell.log`、`ci-merged-result.json`。
临时路径仅为本机审计证据位置，不作为可移植部署配置。
