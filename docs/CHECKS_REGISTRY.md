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

### Agent 结构化产物门禁

`jsonschema=<path>` 是任务/阶段级 validator，不默认加入本仓库 CI。它调用
`scripts/check-json-schema.py`，在产物原子落盘和 `--reuse` 复验前校验纯 JSON；
非法 JSON、未知约束关键字、外部 `$ref` 和 schema 不匹配均 fail closed。

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
