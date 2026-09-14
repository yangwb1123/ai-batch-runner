# P3 生态集成验证记录

验证对象：ai-batch-runner 的 Hub 生态集成，以及 Aero ID 成员检查服务端扩展。
外部 HTTP 契约测试使用本地替身；未向真实账号发消息或部署服务。
验证日期：2026-09-12。Runner 完整 CI 在主工作区执行；Aero ID 在隔离工作树
验证，成员接口与范围/审计修复基线为 `effadafba2beabbb787484105d65527d1657967e`。
补偿 SQL 修复随后独立验证并合入；最终版本与主工作区的一致性见下。

Aero ID 最终提交为 `b498b75ee8131c328826f3151254a40fa5e4811d`。
本轮 30 个变更文件在主工作区及暂存区均与该提交逐字节相同；原有 22 个前端及
API 文档修改的内容哈希与合入前一致。最终 Done gate 覆盖 SQL 修复；最后一次
调整仅将集成测试的过期协调租约改为同路径回收，并已通过上述版本的定向 JSON 测试。

| 检查 | 状态 |
|---|---|
| outbox / HTTP / IM / Compute 第一组 | 52 项通过，另含 2 subtests |
| Hub auth / CLI / HTTP / Compute transport / Audit | 46 项通过，另含 5 subtests |
| 崩溃恢复、分页、配额及成员门禁专项 | 18 项通过 |
| 严格代码质量 | 整合后的主工作区执行 `python quality.py --strict .` 通过 |
| 真实 Hub HTTP → 本地 IM/Audit HTTP 替身联动 | 2 项通过，覆盖状态投递、账本确认、权限隔离、分页与人工重试 |
| Hub 成员门禁与有界 HTTP 传输最终专项 | 连同上述联动共 29 项通过 |
| Aero ID 服务端 Done gate | `make test`、`lint`、`lint-strict`、全部要求的静态/契约检查、integration compile 和 `build` 通过 |
| Aero ID PostgreSQL 定向集成 | 成员投影过滤、成员判定与实际持久审计账本写入通过 |
| Aero ID 默认 `make test-integration` | 触发 20 分钟外层时限（子命令 exit 124）；已完成的 35 个包无失败，repository 及后续 12 个包未完成 |
| Aero ID 未完成集成包补跑 | 同一提交、integration 标签、每包 15 分钟上限；repository 之外的 11 个包全部通过（service 172.319 秒） |
| Aero ID repository 单包补跑 | 触发 Go 15 分钟时限；发现一处既有补偿租约 SQL 缺参，以及一次 PostgreSQL 容器启动超时；修复及补充验证见下 |
| Aero ID repository 分组与失败回归 | 未完成的 47 项分为五组全部通过；修复后两项重点回归也通过；逐项核对无缺失、无失败、无跳过 |
| Runner `make ci` | 通过：2,918 项测试（444.37 秒）、53 项规则评估、8 项工程门禁及后续组织/能力/图检查 |

本轮保留已有 P0/P1/P2 实现和用户原有 Campaign 入口修改。Aero ID 原有前端及
API 文档修改保持原样；只合入本轮新端点、适配器及其验证所需的变更。
其中 Aero ID 的四处既有严格 lint 违规通过小范围职责拆分和换行修复，
保留原有状态转换和恢复语义，并补充对应回归测试。

repository 超时日志绑定 `effadaf`。按同一提交的
`go test -tags=integration -list . ./internal/repository` 核对，164 个顶层用例串行注册，无 `t.Parallel`
或 `TestMain`；前 117 项已经返回，第 118 项
`TestEventReceiptOutOfOrderKeepsNewerVersion` 停在 PostgreSQL 启动阶段，
其后 46 项尚未运行。旧日志没有逐项 PASS 行，因此不据此声明整包通过。
显式失败是 `TestAppendConcurrentDuplicateAcrossPartitions` 的容器启动超时，
以及 `TestCompensationAnchors` 的 SQL 缺参；两项均已独立重跑通过。

SQL 修复使用数据库 `clock_timestamp()` 判断补偿租约到期，与相邻 claim
路径一致。`TestCompensationAnchors` 新增有效租约不可抢占、过期后可重新领取的
断言，同时覆盖 pending 与 unknown 补偿领取。未完成的 47 项按原注册顺序分成
10/10/10/10/7 五组，使用 integration 标签、JSON 日志和每组 6 分钟上限补跑；
五组全部通过，分别耗时 108.322、125.261、50.303、111.379、34.579 秒。
最后再以 JSON 日志验证两个重点用例：补偿回归 4.80 秒、容器重试场景 5.46 秒。
默认 `make test-integration` 的超时仍保留为未通过，没有修改仓库门禁或放宽其时限。

本地证据文件：Runner `/tmp/hub-ecosystem-ci.log`、
`/tmp/hub-ecosystem-main-quality.log`；Aero ID
`/tmp/aero-id-membership-final-done-gate.log`、
`/tmp/aero-id-membership-test-integration.log`、
`/tmp/aero-id-membership-integration-internal-repository.log`、
`/tmp/aero-id-membership-integration-internal-service.log`、
`/tmp/aero-id-membership-integration-remaining.log`；五组 JSON 日志
`/tmp/aero-id-membership-repository-group-{1..5}.jsonl`，重点回归日志
`/tmp/aero-id-membership-repository-compensation-container.jsonl`，
注册顺序核对清单 `/tmp/aero-id-repository-test-boundary.json`。

独立审查还发现一个既有恢复边界：普通补偿与未知结果协调的 claim 都接受过期的
`running` 补偿，持久状态未区分两类租约。结合 `operation_state.go` 先恢复补偿、
后查询未知结果的顺序，协调器崩溃后可能重新执行补偿副作用。这是基于源码的
风险推断；本轮 SQL 修复保留该状态机，新增过期断言只验证同一路径回收。
需要后续单独设计并验证执行与结果协调的持久租约区分，不能把本轮通过的用例
视为补偿恢复语义已全面验证。

HTTP 联动测试启动实际 Hub HTTP 服务及本地接收端替身，验证服务边界上的 JSON、
授权和持久投递流程；它没有启动完整的 Snaplink、IM 或 Audit 生产服务。

完成边界及运行配置见 [ECOSYSTEM.md](ECOSYSTEM.md)。生产身份绑定、跨公网部署、
真实通知及审计落账均未执行；这些不能由本地替身测试替代。
