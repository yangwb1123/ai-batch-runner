# CHECKS_REGISTRY — ai-batch-runner 工程门禁注册表

本仓库的工程质量门禁体系仿照 `snaplink` 的质量管理（`cli.py` + `checks/` +
`engineering.yaml` + `make ci` + git hooks + CI），针对 Python 项目做了适配。
核心原则与 snaplink 一致：

1. **零依赖可移植**：门禁只用标准库（ast/unittest），复制 `cli.py` +
   `checks/` + `engineering.yaml` 到任意 Python 项目即可复用，只改 YAML 配置。
2. **门禁约束自研代码**：与上游 `snaplink/ai-dev` 同步的引擎文件
   （`pi-batch.py`、`ai/run-review.py`）通过 `engineering.yaml` 的
   `ignore_patterns` 豁免——质量规则只约束我们自主编写的代码。
3. **门禁自身有单测**（`checks/test_*.py`，`make self-test`）：检查器坏了
   必须能被发现。

## 门禁清单

| 门禁 | 命令 | 阈值 | 说明 |
|---|---|---|---|
| G1 filesize | `make check-filesize` | 500 行/文件 | 源文件行数预算；豁免清单（`exemptions`）只减不增 |
| G2 complexity | `make check-complexity` | 圈复杂度 15 / 认知复杂度 20（每函数） | ast 实现，替代 gocyclo/gocognit |
| G3 root policy | `make check-root` | 根目录 ≤16 文件 | 根目录只允许配置/入口/文档；禁止 `*_test.py` 散落根目录 |
| G4 style | `make check-style` | 行宽 100（警告） | 行尾空白、tab 缩进、缺末尾换行、非 utf-8 为硬失败 |
| G5 quality | `make quality` | 函数 50 行 / 圈复杂度 15 / 文件 1000 行 / 重复函数体 | 参考版自带扫描器 `quality.py`（纯 stdlib），经 `pi-batch.yaml` 注册为 `validators.pyquality` |
| R0 release metadata | `make release-check` | 名称/稳定版本/Changelog/入口一致 | 普通 CI 只验证分发契约；标签发布另以 `--tag vX.Y.Z` 校验标签并在许可证未决时 fail closed |
| H0 self-test | `make self-test` | — | 门禁自身单测（56 例，unittest 零依赖） |
| T0 regression | `make test` | — | 引擎、规则系统/评估器/检查器与 Campaign 回归套件（pytest，需 dev extras） |
| E0 eval | `make eval` | 53 项 | classify/rules/assess/product/workflow/evolution 行为回归 |
| T1 coverage | `make coverage` | — | checks/ + cli.py 覆盖率报告（需 pytest-cov） |

### 扫描回归的可复现范围

`tests/test_advance.py` 用固定的 Go/Dart/TSX 项目运行全部六个真实检查器子进程，
逐维断言违规确实被发现；默认回归不再读取机器上的外部 demo 仓库，也不因其
不存在而空通过。CLI 测试同样扫描该 fixture，仍验证只读模式不写项目状态。
外部项目体检仍通过显式 `pi-batch advance --dir <project>` 执行，不是默认测试输入。

UI/前后端质量、设计智能、业务闭环检查器只提前剪枝**已有排除集合**中的目录，
不增加排除项、不改变规则或 manifest 选择；文件结果排序稳定。知识维护检查器
仍仅按原有 `.dart_tool` / `node_modules` 来源排除剪枝，未把其他门禁的 build/vendor
排除策略套给关键字段审计。模块存在性探测可在首个来源文件停止，实际审计仍
遍历所有已准入来源；业务闭环报告计数来自同一次扫描，而非再次枚举变化中的树。
`tests/test_checker_traversal.py` 对照旧准入谓词检查文件集合，并验证被排除目录
不被打开、目录符号链接不递归、显式 skip/大小写/文件链接语义保持。

Fabric 调度回归的等待条件要求真实审批账本命中和首个 Runner 心跳，而非把
已出现但无 `approved` 字段的注册记录当成授权；审批写入暂停点验证这一区别。
MCP 深度压力测试保留 2000/200000 层输入，但按当前解释器实际能否解码区分
Invalid Request / Parse error，另固定覆盖可解码的非请求与解码器递归拒绝后恢复；
不改变生产协议、递归限制或审批规则。

这些是遍历与默认测试输入的修复，不是旧 CI 超时的完整根因证明；全量 CI
是否通过必须以完整 `make ci` 结果为准，不能以局部提速或栈转储后的活动代替。

### SQLite 并发与私有状态回归

`PoolStore` 的初始化、数据库/sidecar 权限检查、SQLite 事务到 rollback/close 共用
原来初始化使用的 **POSIX 父目录 flock**；此前检查 journal 时另一个正常事务可
提交并替换 inode，使严格的「检查期间文件变化」拒绝也命中正常并发。现在协作实例
在检查前取得锁，避免该窗口；没有删 journal、忽略 inode/uid/硬链接或关闭其它 DB
描述符（后者会丢失本进程的 POSIX SQLite record locks）。守卫等待与 SQLite busy wait
共用原有 10 秒争用预算，不把两段等待叠加；I/O/事务执行仍不是硬实时截止。

独立的 Linux `tui-soak` 合成 provider 证据库也在其自有 scratch 目录锁内打开、读取/
提交和关闭 SQLite 连接，避免多个 fixture 进程同时抢 journal 与读锁。锁等待与 SQLite
busy wait 共用原有 **3 秒**；锁忙、坏 JSON、遗漏序号、超限、导出失败仍阻断验收，
不返回伪造空记录或提前推进 cursor。SIGKILL 仍只留下「完整已提交或无记录」的证据，
内核释放锁，不加 lock-breaking、cleanup、模型调用或延长 Agent/验收/任务期限。

`test_llm_pool_store_guard.py` / `test_tui_soak_store_guard.py` 验证检查/连接/关闭时锁
仍持有、等待剩余预算、异常 rollback、拒绝路径替换与未知所有者，以及异常/被杀释放。
原有多进程、16-thread admission、quota/暂停/配置快照、SIGKILL、固定字节/记录预算、
完整 export 和原 SQLite busy 断言保留。锁只协调采用此机制的实例；不保证其它 SQLite
客户端、旧进程或恶意同 uid 写入者安全，也不是账户授权/lease 或真实 provider 验收。
受控双写事务重现了 journal 检查窗口；这不是所有历史 CI 失败/超时的完整归因。
全量 `make ci` 仍须独立报告，不能以离线通过或去掉原有真实 provider 测试冒充全绿。

Fixture 的有界 SELECT 现在仅在连接/目录 guard 内复制 `(id, bytes)`；保留原行数/
字节/序号准入，SQLite 关闭后才释放 guard，再解码已复制的完整 batch。JSON 校验
不再占据数据库锁，但**全批通过才导出/推进 cursor**；坏 JSON、非 UTF-8、缺序、
超限或导出失败仍拒绝，不返回空成功、不重查/拼接新写入。schema/顺序/read API、
128 条/1MiB/3 秒预算和 SQLite-close-before-unlock 不变，未触碰 PoolStore/Agent。
`tests/test_tui_soak_batch_decode.py` 确定性验证关闭/解锁早于解码，解码期间的合法写入
可提交且本批仍只返回选中的字节、坏批不部分导出，以及 raw 类型/字节准入不后移。
旧作用域的受控用例会复现同类 `cooperative store is locked`；这证明一个可消除的
锁占用机制，**不证明所有历史 CI 锁超时的根因**，也不保证调度公平或锁永不超时。

`tests/test_state_guard_deadlines.py` 固定时钟验证：flock 即使成功，若已到共享等待
期限也须拒绝；获锁后目录/私有 sidecar 检查耗尽预算，不得以 `timeout=0` 打开
SQLite 或开始事务。PoolStore 仍 10 秒、fixture 仍 3 秒，不重置/叠加等待期限；
无效布尔/零/负值/非有限/非数值预算在时钟/文件访问前拒绝。fixture 同时检查 fd
与命名目录 uid，变化或未知边界不授予准入。拒绝后关闭自有 guard，保存已有库和
导出游标；不破锁、不删 sidecar、不恢复账户。此为准入检查而非文件 I/O/事务的
硬实时保证，也不是旧并发锁超时的完整根因或公平性证明。

Linux fixture guard 的争用等待在 10/11ms 两个间隔间交替，首个仍 10ms；每次按
原 deadline 的剩余时间截短，不增加轮询频率、重置预算或准许过期锁。固定 10ms
轮询可能与周期性重复持锁同相位，持续错过存在的空闲窗口；
`tests/test_tui_soak_poll_phase.py` 用整数纳秒时钟/受控可用区间重现旧行为，并验证
漂移后在原预算内取得窗口、永久占用仍拒绝、短剩余预算不被延长。此模型证明一个
轮询别名机制，不证明机器上历次超时的唯一根因，也不是 FIFO、实时或公平性保证。
只调整合成 fixture；PoolStore/account/Agent/provider 等待和执行策略不采用此漂移。

Fixture JSONL 导出现在由 `tools/tui_soak_export.py` 检查完整写入，初次独占创建为
0600，后续只追加仍与已记录 dev/inode/uid/类型/单链接/大小/mtime/ctime 一致的文件。
既存导出不自动接管（含空库/新 reader），目录在 reader 创建时绑定并在实际打开/
追加前后复核，路径替换/链接/未知所有者/截短/额外写入拒绝，不读取旧正文来猜测恢复。
短写或未知 write 返回值、write/flush/close 失败均不推进 SQLite cursor、不返回本批
成功；reader 锁定导出拒绝，后续在 DB 读取前停止，不将整批再次追加到已损坏前缀。
保留已写字节、数据库、别名/替换现场和可能的空文件，不 truncate/unlink/自动重试；
原目录阻挡仍抛 OSError。`tests/test_tui_soak_export.py` 固定注入短写/部分写后异常/
flush/close、FD 包装失败、旧导出/二次 reader、文件与真实目录替换、FIFO/链接、
空查询前损坏拒绝及正常顺序/权限；原全批校验、128 条/1MiB/3 秒、SIGKILL/导出
门禁不变。样本只涉及合成 fixture；顺序 stat/独占创建不证明独占 lease、敌对同 uid
隔离或原子快照，也不是掉电持久性、文件 I/O 硬期限、数据库/整套 worktree 恢复。

### 多会话实例观察回归

`tests/test_instance_observation.py` 固定覆盖同会话终态替换、多个会话交错、追加
顺序而非时间戳排序、损坏/未完成尾行不回退到旧状态、未知计数及 CLI 只读边界。
`tests/test_instance_observation_io.py` 检查大账本确实只读有界尾部、行对齐/单行和
记录预算、严格 UTF-8/重复键/非有限 JSON、路径/类型/uid/别名拒绝、读取变化及
有界目录枚举。原有 `test_instance_view.py` / `test_instance_alerts.py` 保留。
`tests/test_instance_alert_coverage.py` 验证部分/损坏/空/缺失覆盖不解除旧告警，完整
终态窗口才能解除，并保留投递失败重试语义。均为私有 synthetic fixture，不读取
正在运行的 Campaign/实例、不调用模型，不证明所有历史错误或真实多设备验收。

### 注册进程只读查询回归

`tests/test_registry_observation.py` 覆盖空目录查询无写入、已有 fallback 选择、不从
拒绝目录扩大读范围、关闭 argv/body/内部路径投影、文字控制安全、覆盖诊断及
声明状态/保守 PID 观察与进展的分离。`tests/test_registry_observation_io.py` 用私有
fixture 验证真实字节/目录/记录预算、读取变化/描述符关闭、别名/类型/uid/JSON 拒绝，
以及活动视图的原始主机比较和不同注册路径不按 PID/时间猜测合并。原有 registry
登记/心跳/显式 prune 测试保留；新测试不读取或清理真实实例登记、不调用 provider。
`tests/test_activity_observation.py` 再覆盖目录准入早于枚举/PID 探测、读取后目录消失/
替换丢弃旧事实而不伪装成初始缺失、解码后的孤立 Unicode 代理字符拒绝与合法代理
对保留、C1/bidi 文字安全、未来开始/心跳未知而非零秒、未知 PID 行可见为缺口、
缺失/预算未读覆盖与固定无权字段。既有 PID 保守探测与进程控制/锁门禁不改写。

`tests/test_tui_signal_wait.py` 对原 PTY smoke 的轮询做确定性竞态回归：先检查预期
退出谓词、再第二次 poll，可能将两次 poll 之间的正常退出误报为“提前退出”。修复
为先缓存本轮退出观察、再核对谓词；原 10 秒共享 deadline、预期退出码、两条自有
进程身份、零活后代及 durable UNKNOWN/不伪造成功的检查均保留。该受控复现只证明
一个轮询机制，不解释所有历史 PTY/取消失败；原 `worker.join(2)` 取消门禁未放宽。

### TUI 补全时序与真实 Codex 诊断

`tests/test_tui_composer_events.py` 固定注入迟到的 `Input.Changed`：旧值不得失效
当前补全上下文，当前值和真实清空仍须生效。`test_tui_dynamic_completion.py` 在
实际无头 TUI 中验证旧事件不关闭 picker、不丢弃冻结快照、不额外调用 RPC/setter；
真实新输入仍关闭旧 picker。等待条件核对 Compose/Mount 完成，而非仅看到已入栈的
screen 类型；没有加长原等待预算、吞掉挂载异常或删除只读/次数/会话断言。

真实 Codex smoke 仍在 CLI 已安装时运行，不新增 skip、替换 provider 或改写账号。
失败断言显示有界脱敏的状态、退出码及原因，不再被 pytest 的整份 snapshot 截断
隐藏认证错误；timeline 用例还核对真实 assistant 文本，用户 prompt 内的 `LISTED`
不能单独充当成功证据。`tests/test_tui_codex_diagnostics.py` 验证失败仍失败、诊断
脱敏/截断且不回显私有 workspace。令牌过期、refresh-token reuse 或 provider 拒绝
必须恢复实际认证/服务后重跑；不能以这些本地回归通过冒充完整 `make ci` 全绿。

取消进程回归的 `/proc/<pid>/stat` 观察不先 `exists()` 再读，避免进程在两步间
退出造成假失败；只将 ENOENT/ESRCH 视为已消失，存在的进程仍必须是 `Z`。
`tests/test_review_process_observation.py` 验证活动状态不被接受、权限/IO/坏格式不
被吞掉；原有子进程停止、无关进程存活及等待预算断言不变。

### Codex exec 启动/轮次/中断栅栏回归

`tests/test_tui_codex_fencing.py` 用自有 spawn/JSONL 推送进程重验：并发
`start_turn` 只允许一个 provider，竞争方在被拒绝时不停止已接受进程；spawn 期间
close 拒绝启动并只回收本次返回的进程；worker 部分启动失败保留原错误并回收；
陈旧 reader 不能改写 resume 身份/usage/artifact，也不能向当前轮次注入事件；
`thread.started` 必须在本轮次 turn.started 之前且只登记一次；中断省略字段可作
当前轮简写，但显式不匹配/空值/别名必须拒绝，且 header/start 交接后目标变更时
于设置标志与停止前拒绝。

这些用例无真实 provider/凭据/区域参与，不证明历史启动/轮次/中断提案实现、真实
Codex 失败根因或平台验收；完整 CI 仍保留原真实 provider/PTY 测试，不以局部通过
代替完整 CI、集成或发布验收。中断失败不代表副作用已回滚。

### 快照呈现合并与最终交接回归

`tests/test_tui_snapshot_delivery.py` 用自有 worker/UI 时钟与 typed controller 重验：
UI 阻塞不能把已确认终态拖出原 deadline；1000 个连续 delta 只产生一条通知且应用
最新不可变快照（事实不丢失）；陈旧通知重放不能消费更新的待处理通知；被拒绝的
通知不回滚事实且允许后续通知；卸载后不再应用迟到快照；操作完成后的最终同步交接
不依赖待处理通知。`SnapshotDelivery` 以 controller 身份、delivery、ticket 与已应用
revision 栅栏精确通知，`reidentify/close` 使旧通知失效。

这些用例只证明本地呈现准入契约：无真实 provider、Codex 凭据或设备参与，不证明
历史 presentation 候选实现、真实 PTY 之外的平台验收或执行授权；完整 CI 仍保留原
真实 provider 与 PTY 测试，不以局部通过代替完整 CI、集成或发布验收。

### Structured 与托管必需折叠准入回归

`tests/test_tui_structured_admission.py` 与 `tests/test_tui_fold_admission.py` 用自有
时钟/adapter/typed controller 和有界 journal 重新复现原期限与准入边界，不把
临时目录中已经不可用的历史源码、日志或 PASS 重新认定为证据。Structured 在
初始化/会话/handle/消费与必需折叠后复核同一 RuntimeOperation 期限；done/stream
关闭不能绕过，及时终态不被迟醒协调/timer 反转。明确 False 的正文、warning/error
不重新进入结果，也不耗尽 item 容量或替换已接受部分正文；终态 False 必须拒绝。
旧 void/无 sink 与非 False 历史行为保留；呈现不可用不同于必需折叠拒绝。

托管 turn 与 App Server review 先折叠，再在原期限内完成；review capture 只取
准入事件且仍为必需工作。已保存 provider 成功与本地失败并列保留，结果/异常
收尾不得显示 Ready/succeeded，也不另造 provider 失败或宣称副作用回滚。
原 RuntimeOperation 类、状态表、timeout、stop/grace/join、输出/item 上限和既有
真实 provider/PTY 断言未修改。同步呈现成本未消除；本次不声称恢复所有消失的
runtime fencing/呈现提案，不证明真实 Codex、历史 journal 故障或平台验收根因。
这些受控用例无真实 provider；完整 CI 仍保留原真实 provider 测试，不以局部通过
代替完整 CI、集成源码或发布验收。

### Agent 结构化产物门禁

`jsonschema=<path>` 是任务/阶段级 validator，不默认加入本仓库 CI。它调用
`scripts/check-json-schema.py`，在产物原子落盘和 `--reuse` 复验前校验纯 JSON；
非法 JSON、未知约束关键字、外部 `$ref` 和 schema 不匹配均 fail closed。

### 本地 Web 刷新失效回归

`tests/test_task_center_refresh_browser.py` + `tests/fixtures/task_center_pending_browser.js`
在隔离 Node VM 中运行交付的 page script，验证普通定时/强制刷新发出共享 GET 前
清除旧任务/目标/活动、选择详情和旧时间/计数；完整新响应才能恢复同 ID 的选择。
失败、乱序成功/错误不能复活旧事实，literal markup 不创建 DOM 或额外请求。
原历史/反馈/资源/请求/关注与 HTTP 认证回归不改写；无模型、现场历史或实际实例
读取。VM 不是实际浏览器/设备或可访问性验收，客户端 abort 也不是任务取消证明。

### 本地 Web 关注投影准入回归

`tests/test_task_attention_web_admission.py` + `tests/fixtures/task_attention_admission_browser.js`
运行交付脚本，验证显式 null/false/零与真正缺字段 legacy 的区分，关闭字段/证据/
权限、128 引用预算、计数/覆盖/汇总、完整身份/来源过滤、歧义重复、稳定顺序、
合法/孤立 Unicode 对、任务显示窗口外引用、损坏后旧响应/错误 fence 与 GET-only。
未知 shape/额外私有字段不能回显；根因/操作/权限不能借 UI 升级。
原反馈/资源两个 source-switch fixture 只重建新空窗口的 attention 覆盖（旧 fixture
删除 sources 却留 source_records=1）；原断言、认证、控制拒绝和晚到响应测试保留，
另有显式回归确认旧覆盖冲突仍被拒绝。VM 是私有合成证据，非 provider/真实浏览器/
设备/部署验收，不核验 digest/进程活性，不补读未显示目标或原始正文。

## 使用

```bash
make check          # G1-G4 零依赖门禁
make self-test      # H0 门禁自身单测
make quality        # G5 组织质量扫描（quality.py，上游同步）
make release-check  # R0 分发元数据契约（不触发发布）
make test           # T0 回归套件（pip install -e ".[dev]"）
make ci             # R0 + G1-G5 + H0 + T0 + E0 + organization contracts
python cli.py check-filesize   # 单独跑某门禁
```

## 配置（engineering.yaml）

各门禁阈值与豁免均在 `engineering.yaml` 中声明：

- `filesize.max_lines` / `ignore_patterns` / `exemptions`
- `complexity.max_cyclomatic` / `max_cognitive` / `ignore_patterns`
- `root_policy.max_files` / `allowed_files` / `banned_patterns`
- `style.max_line_length` / `ignore_patterns`

`ignore_patterns` 为子串匹配（snaplink 语义）。新增业务代码必须通过全部门禁；
需要放宽阈值时先改 YAML 再提交，并在 commit message 中说明理由。

## Git hooks

```bash
git config core.hooksPath .githooks
```

- `pre-commit`：对暂存的 `.py` 文件跑 filesize + style 门禁（豁免规则同上）
- `pre-push`：跑 `cli.py harness`（= `make check` + `make self-test`）

## 上游同步豁免

`pbatch/`（引擎包）、`pi-batch.py`（入口 shim）、`ai/run-review.py`、`quality.py`
均为与 `snaplink/ai-dev` 同步的上游文件，经 `engineering.yaml` 的
`ignore_patterns` 豁免（这些模块仍须通过 `quality.py` 的 1000 行预算；
参考版自带的行尾空白与复杂度超过本仓库更严预算的少数函数一并豁免）。
质量规则实际约束自研代码：`checks/`、`cli.py`、`Makefile` 及新增业务代码。

## CI

`.github/workflows/ci.yml`：Python 3.11 + `pip install -e ".[dev]"`，依次执行
R0、G1-G5、H0、T0、E0、organization contracts、T1。推送到 `main` 或 PR 时触发。
`.github/workflows/release.yml` 只响应 `vX.Y.Z` 标签；它执行带标签的 R0、确认提交
可从 `main` 到达、重跑 `make ci`、构建并冒烟测试分发包，然后经受保护的 `pypi`
Environment 和 OIDC Trusted Publishing 上传。
