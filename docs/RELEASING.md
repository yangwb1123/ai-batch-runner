# 发布 `pi-batch`

产品有两个稳定名称：PyPI distribution 是 `ai-batch-runner`，用户安装后执行的
命令是 `pi-batch`。版本唯一事实源是 `pyproject.toml`。

## 最小 Linux 发行范围

最小发行面向 Linux x86_64 的本地 CLI、单批任务、pipeline 和 TUI。可执行文件
内置 Python 与规范资产，仍需单独安装并登录 provider CLI；Git 和项目自己的
构建工具按任务需要安装。`pool`、Hub/fleet、跨设备执行和手机控制台不在这份
最小发行的生产验收范围内；命令存在不代表已经获得该范围的上线证据。

正式 Linux 构建使用 `ubuntu-22.04`，其他 Linux 发行版与更老系统需另行验证。
本机构建只能证明当前机器上的行为；构建清单会记录实际系统、Python、
PyInstaller、源码输入与可执行文件摘要。模拟故障恢复和少量真实模型任务也
不能代替目标部署环境中的长期运行验收。

只发布 Linux 二进制时使用 `Release Linux binary` 工作流，无需先发布 PyPI 或
等待其他平台通过。运行前仍需关闭下文的 `LICENSE` 授权未决项，提交版本与
CHANGELOG，并把签名版本标签指向可从 `main` 到达的提交。在 Actions 中从
`main` 手动运行，输入完整的 `vX.Y.Z` 标签。工作流依次执行：

1. 核对标签、版本、授权与主分支归属，运行完整工程门禁。
2. 复用平台验证工作流，仅构建并验证 Linux x86_64，始终绑定同一源码提交。
3. 在 `binary-release` Environment 中发布前，再验证清单、二进制、LICENSE 和
   远端标签；将二进制、`.manifest.json`、`.LICENSE` 及 `SHA256SUMS` 一起发布。

仓库管理员应保护 `binary-release` Environment，并保护
`.github/workflows/release-binary.yml` 与共享验证工作流。已有版本不得覆盖。
推送版本标签仍会触发原有 PyPI/全平台流程；每个版本只选择一个发布入口，
后续扩大平台范围使用新版本。Linux 工作流不依赖 PyPI Environment 的批准。

授权未决时可以完成本地候选构建和验证，不能把候选报告当作正式发布批准。
候选验收命令、证据含义及明确限制见下文。

## 一次性设置

1. 在 PyPI 创建 pending Trusted Publisher：
   - PyPI project：`ai-batch-runner`
   - GitHub owner：`yangwb1123`
   - Repository：`ai-batch-runner`
   - Workflow：`release.yml`
   - Environment：`pypi`
2. 在 GitHub 仓库创建 `pypi` Environment，配置 required reviewers，正式上传前
   必须人工批准。
3. 保护 `main` 和 `.github/workflows/release.yml`，发布工作流的变更必须评审。
   同样保护正式发布复用的 `.github/workflows/binary-verify.yml`。
   同时用 GitHub ruleset 要求签名标签；工作流不导入维护者公钥，因此不自行冒充
   签名验证器。工作流中的 Action 固定到完整 commit SHA；更新时只从对应 Action
   的官方 GitHub 仓库解析发布 ref，并同步行尾版本注释和发布契约测试。

Trusted Publishing 使用 GitHub OIDC 短期凭证，不在仓库或 GitHub Secrets 中保存
长期 PyPI token。

## 发布版本

0. **首次公开发行阻断项**：当前 `LICENSE` 注明上游 `snaplink/ai-dev` 的许可证
   当时不可公开核验。发布者必须先确认自己拥有全部相关代码的再许可权，或取得并
   遵循上游许可证与 NOTICE 要求；将证据和正确归属写入仓库并清除该未决说明。
   在此之前，带 `--tag` 的 release check 会 fail closed。
1. 将 `pyproject.toml` 的版本更新为 `X.Y.Z`，并在 `CHANGELOG.md` 添加同版本标题。
2. 授权问题关闭后，在干净工作区执行：

   ```bash
   python -m pip install -e ".[dev,binary]" build twine
   python tools/check-release.py --tag vX.Y.Z
   make ci
   python -m build
   python -m twine check dist/*
   # `binary` extra provides PyInstaller
   python tools/build-binary.py --output dist --name pi-batch-linux-x86_64
   python tools/smoke-binary.py dist/pi-batch-linux-x86_64
   ```

3. 合并到 `main` 后创建并推送签名标签：

   ```bash
   git tag -s vX.Y.Z -m "pi-batch X.Y.Z"
   git push github vX.Y.Z
   ```

4. GitHub Actions 的 `Release to PyPI` 会先确认标签提交可从 `origin/main` 到达，
   再重新执行完整门禁、构建 wheel/sdist、在隔离虚拟环境中安装冒烟测试，然后构建
   Linux/macOS Intel/macOS ARM/Windows 二进制并分别冒烟测试。全部二进制验证通过后，工作流停在 `pypi`
   Environment 等待批准；批准后上传 PyPI，并把同一批二进制及 `SHA256SUMS` 上传到 GitHub Release。
   二进制只包含 `pi-batch` 编排器和规范资产；`pi`、Codex、Claude 等 provider CLI 仍
   由用户单独安装并配置。
5. 运行态隔离：默认发行包/CLI 将 session、memory、被拒诊断、campaign 报告、日志和
   实例锁写入 `~/.abr/projects/<workspace-id>/`，不会在被操作仓库创建
   `.md`/日志。需要迁移位置时设置 `PBATCH_RUNTIME_HOME`，或用
   `PBATCH_DATA_DIR`/`--data-dir` 指定单项目 runtime 根目录；显式 `--output`、pipeline
   `output` 和项目知识文档仍按用户指定路径写入。

从公共索引验证：

   ```bash
   pipx install ai-batch-runner==X.Y.Z
   pi-batch --version
   pi-batch doctor
   pi-batch tui
   ```

PyPI 文件不可覆盖。错误版本应在 PyPI yank，并立即发布修复版本；不要删除标签后
复用相同版本号。

## 不发布的跨平台二进制验证

在 GitHub Actions 选择 `Verify platform binaries` → `Run workflow` 可运行独立验证，
无需创建标签。`platform` 可选 `all` 或 `linux-x86_64`；`source_sha` 留空时使用
本次触发提交，显式指定时必须是完整的 40 位提交摘要。修改运行时代码、打包配置、内置资产或验证脚本的 pull request 也会
触发同一工作流。它仅使用 `contents: read`，不申请 PyPI OIDC，不写 GitHub Release；
正式发布的 `build-binaries` 直接复用该工作流，许可证门禁仍由发布流程先行执行。

单一构建矩阵固定为以下平台；这表示构建及验证的环境，不代表更老系统已兼容：

| 二进制 | GitHub runner | Python 架构 |
|---|---|---|
| `pi-batch-linux-x86_64` | `ubuntu-22.04` | x64 |
| `pi-batch-macos-x86_64` | `macos-15-intel` | x64 |
| `pi-batch-macos-arm64` | `macos-15` | arm64 |
| `pi-batch-windows-x86_64.exe` | `windows-2022` | x64 |

标签与架构依据 [GitHub 官方 runner 支持表](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
核对（2026-09-22）。旧 `macos-13` 已退役，`macos-14` 正在弃用并计划于
2026-11-02 退役，因此两个 macOS 架构均固定在 macOS 15；详见官方
[macOS 13 退役公告](https://github.com/actions/runner-images/issues/13046) 与
[macOS 14 弃用公告](https://github.com/actions/runner-images/issues/13518)。

每个平台先执行原生跨进程文件锁、只读 PID 探测、实例锁竞争/排队/代际和多参与者
冲突测试，再独立构建并执行隔离 smoke；Linux 额外执行同一 TUI 进程的短时故障恢复
及活动 provider 的 SIGTERM/SIGHUP 退出验收、持久化 UNKNOWN 恢复检查，
以及批处理 SIGTERM/SIGINT 的子进程回收与实例锁交接。
构建、验证及各 job 均有超时。成功与失败都会尝试保留 14 天的
`pi-batch-verification-<platform>` 工件，包含平台与源码提交、步骤结局、文件锁与构建日志、
`smoke.json`、`smoke.log`，以及 Linux 的 `soak/`、`signals/`、`recovery/` 和
`batch-signals/` 证据。未启动的步骤在结局中显示
`skipped`，不会伪装成通过；job 被强制终止或 runner 丢失时可能无法上传诊断。

只有全部必要检查通过的平台才上传 `pi-batch-binary-<platform>` 工件。正式发布仅
下载这个名称前缀，工件包含二进制及其构建清单、LICENSE；失败诊断不会混入
Release 资产。Windows smoke 明确
记录 POSIX PTY 跳过，不能据此声称 Windows TUI 交互或整个进程树回收已经验收。
工作流配置和本机契约测试不代表远端四个平台已经通过；以对应 Actions 运行工件为证。
当前这轮本机验收只覆盖 Linux，macOS 与 Windows 仍缺少本轮真机通过证据。
Windows job 也用于暴露实际移植缺口；其失败会阻止发布，不设置允许失败或假跳过。

## 二进制安装与移动端

从 GitHub Release 下载与系统匹配的 `pi-batch-<platform>-<arch>` 后即可运行：

```bash
chmod +x pi-batch-linux-x86_64
./pi-batch-linux-x86_64 --version
./pi-batch-linux-x86_64 ops serve --help
```

二进制不是 provider 的替代品；执行真实任务前仍需安装 `pi`/Codex 等 agent。升级时
只替换二进制，不会删除 `~/.abr/projects/` 中的会话和日志。
流水线按阶段和任务实际使用的 agent 预检；已全部指定可用执行程序时，无需安装
未使用的默认 agent。默认回退及 gate-fix 会使用的程序仍须可用。
使用 `--validate gofmt` 时需要目标环境安装 `gofmt`；缺少工具、工具执行失败或
存在未格式化文件都会拒绝产物落盘。
Codex 不在 PATH 中时，可用 `pi-batch tui --runtime app-server --agent-bin
"/absolute/path/codex"` 指定安装位置；路径作为单个参数传给子进程，支持空格。

在没有项目 `pi-batch.yaml` 的目录启动时，包内配置属于默认层，用户
`settings.yaml` 中的 provider/agent 设置可以覆盖它。优先级为 CLI 参数、项目配置、
用户配置、打包默认。现存配置读取或 YAML 解析失败会拒绝普通执行；使用
`pi-batch config check --json` 或 `pi-batch doctor --json` 查看文件位置和诊断，
错误信息不展开可能含凭据的 YAML 正文。

配置只接受普通文件（含指向普通文件的符号链接）；FIFO 等特殊文件及打开时被替换
成特殊文件的情况立即拒绝，不等待写入端。每份配置最多读取 2 MiB，
`config check --max-bytes` 只能收紧这个上限。YAML 树
最多 64 层，按引用展开后的节点与文本分别限制为 100,000 个和约 8 MiB，并在构造
合并引用前检查；正常共享引用与 `<<` 合并仍受支持。配置值使用 JSON 兼容类型与
有限数值，键必须为文本；日期若作为文本使用应加引号。循环引用、过大整数和非法
类型标记会产生不包含原值的诊断。数值设置里的非法字符串或超出平台计数范围的
整数沿用警告后回退默认值的行为，`config explain --json` 可核对实际解析结果。

本机可从当前源码构建，无需先发布标签：安装 `.[binary]` 后运行
`python tools/build-binary.py --output dist`，再执行
`python tools/smoke-binary.py dist/pi-batch`（Windows 使用 `dist/pi-batch.exe`）。
需要机器可读证据时添加 `--report dist/binary-smoke.json`；报告包含实际通过、跳过和
失败项目以及二进制摘要，验证失败也会写报告并返回非零退出码。
从目标项目目录运行该文件即可进入 TUI；构建结果只适用于构建平台，Linux 系统库
兼容性也需在目标机器验证。未提交工作区构建不等于经正式发布流程验收的版本。

构建成功时同目录生成 `<binary>.manifest.json` 与 `<binary>.LICENSE`。清单绑定
二进制摘要、完整许可证、实际项目打包输入和构建环境；构建期间输入变化会失败，
开始重建时旧的同名成功清单会失效。它证明构建来源，不证明工程测试或生产验收
已经通过。源码修改后应重新构建，不能继续引用旧二进制的通过报告。

每次编译在独立临时目录内生成新产物，编译器退出成功但没有产物也会拒绝。
同目录同名称的构建使用系统锁互斥，冲突立即返回 5；不同名称可独立构建。
二进制和 LICENSE 发布并复核后才发布清单，发布失败会移除成功清单。
构建目录中的 `.build.lock` 文件保留用于维持锁身份，不属于发布资产。

```bash
python tools/build-binary.py --output dist/linux-candidate --name pi-batch-linux-x86_64
python tools/binary_manifest.py verify \
  --manifest dist/linux-candidate/pi-batch-linux-x86_64.manifest.json --source-root .
python tools/smoke-binary.py dist/linux-candidate/pi-batch-linux-x86_64 \
  --report dist/linux-candidate/smoke.json
```

清单校验同时检查项目输入与二进制是否改变。测试报告应引用相同二进制摘要；
清单未覆盖的部署行为必须分别保留验收证据，不能仅凭版本号相同合并旧报告。

内部 CLI 子进程直接重入当前二进制；内置 Python 验证器使用包内解释器，
不要求目标机器另装 Python。工程质量、能力注册表与依赖图使用随包携带的源码
快照检查，不能因冻结包内缺少 `.py` 而把空扫描当通过。项目自定义 Python
命令、Git、provider CLI 以及目标项目的构建工具仍是外部依赖。
构建器通过 `--python-option "X utf8=1"` 固定冻结解释器使用 UTF-8，保证中文诊断
在 Windows 重定向输出中可用。PyInstaller 冻结解释器忽略 `PYTHONUTF8` 等环境选项，
因此该设置写入可执行文件，详见 [PyInstaller 解释器选项](https://pyinstaller.org/en/stable/spec-files.html#specifying-python-interpreter-options)。

二进制验收在源码目录之外、隔离用户配置和 runtime、空 `PATH` 下执行内置门禁、
health 探测、模拟 provider、schema 拒绝零产物及流水线；另外验证 Git worktree
子调用和 POSIX 终端的启动/退出。Windows 明确跳过 POSIX PTY。模拟 provider 的
验收不替代真实模型接入、目标平台兼容性和长期运行验收。

普通任务验收使用默认实例锁；额外启动同一二进制的并发进程，验证同输出与同会话
冲突返回 5、`ps --json` 不伤害活跃持锁进程、`--wait-lock` 在释放前保持排队，
释放后完成任务且锁可再次使用。模拟 provider 通过 ready/ping/release/done 握手
协调，不用固定等待时间假定持锁成功；失败和超时会释放 fixture 并清理本次创建的进程。

stdin prompt 与任务文件受 `input.max_bytes`（默认 2 MiB）约束，按 UTF-8 字节
计数且不先去除空白；损坏的 UTF-8 不替换后执行。stdin 拒绝发生在创建日志和
实例锁之前。文件输入只接受普通文件（可通过符号链接引用）；命名管道和打开时
变成特殊文件的路径会立即拒绝，避免等待不存在的写入者。

任务和流水线 YAML 的解析也受构造预算约束：最多 64 层、100,000 个展开节点和
约 8 MiB 展开文本。解析节点图时即限制深度和节点数，并在合并引用构造前检查
展开量；循环引用及非 JSON 兼容
类型会拒绝；正常共享锚点、`<<` 模板和顶层任务列表仍可使用。自动流水线检测与
输出锁预扫描使用相同边界，解析错误只报告位置与错误类别，不回显输入正文。

Linux 可继续执行同一个 TUI 进程的故障恢复验收：

```bash
python tools/tui-soak.py dist/pi-batch-linux-x86_64 --cycles 3
python tools/tui-soak.py dist/pi-batch-linux-x86_64 --cycles 0 --duration 3600
```

脚本通过真实 PTY 操作二进制，循环发送成功、失败、超时与取消任务，并通过
`/new` 恢复；provider 是本地确定性 Pi RPC 模拟，不消耗模型额度。报告记录实际
运行时长、二进制摘要、进程身份、文件描述符与 RSS，以及实际 journal 证据。
默认短验收也纳入 Linux 二进制发布门禁；其他平台及真实 provider 长期稳定性
仍须分别验收。运行脚本需要 Python，交付二进制的日常使用不因此增加依赖。
验收使用新建的输出目录；发现首个本地 journal 后，通过有界目录通知及时发现
后续会话。通知丢失或超限明确失败，收尾仍完整流式检查全部 journal。

POSIX TUI 收到 SIGTERM/SIGHUP 时执行取消与正常清理，退出码为 `128 + 信号值`；
未获 provider 终态确认的本地任务记录 UNKNOWN，不自动重放。SIGKILL 无法处理。
可用本地模拟 provider 验证活动任务期间的信号退出、日志收尾与进程清理：

```bash
python tools/smoke-tui-signals.py --binary dist/pi-batch-linux-x86_64
```

已持久化 UNKNOWN 的会话重启后保持只读；QUEUED/RUNNING/INTERRUPTING 等未确认
状态也按 UNKNOWN 恢复。`/resume` 不绕过限制，显式 `/new` 可开始新会话。
以下检查预置这些 journal 状态，通过真实 PTY 验证拒绝执行、新会话恢复、
已确认 SUCCEEDED 续接和原日志不变；它不代表实际 SIGKILL 注入：

```bash
python tools/smoke-tui-recovery.py --binary dist/pi-batch-linux-x86_64 \
  --output-dir validation/recovery
```

批处理收到 SIGTERM/SIGINT 时返回 130，在释放实例锁前回收本次 agent、阶段命令
和验证器进程组，停止排队中的验证器。以下本地命令检查并行阶段、串行/并行验证器、
临时产物清理，以及同工作区立即重启；不调用模型：

```bash
python tools/smoke-batch-signals.py --binary dist/pi-batch-linux-x86_64 \
  --output-dir validation/batch-signals
```

已登录真实 provider 后，可以显式运行下面的有限模型验收。它会消耗模型额度，
每次最多发送四个 prompt，使用独立项目和 pbatch runtime，沿用 provider 的账户
配置，不改登录凭据。输出目录必须是新目录：

```bash
python tools/smoke-provider.py dist/linux-candidate/pi-batch-linux-x86_64 \
  --runtime codex-app-server --allow-model-calls --turn-timeout 120 \
  --output dist/linux-candidate/provider-codex
python tools/smoke-provider.py dist/linux-candidate/pi-batch-linux-x86_64 \
  --runtime pi-rpc --allow-model-calls --turn-timeout 120 \
  --output dist/linux-candidate/provider-pi
```

该验收检查真实回答、同会话连续性、生成中的取消、`/new` 后恢复与退出清理。
取消必须发生在已观察到模型流式输出且尚未完成的回合；已完成的竞态不能冒充取消
通过。报告区分 provider 确认的 INTERRUPTED 和本地保守记录的 UNKNOWN，包含
实际二进制摘要与 journal 证据。连接成功、认证失败或未执行的步骤都不能替代
模型任务通过证据；这些短任务也不构成长时间生产稳定性保证。

在运行 agent 的机器上启动手机浏览器控制台。默认只读、只监听本机：

```bash
pi-batch ops serve --port 8731 --token-file ~/.config/pbatch/ops-token
```

浏览器打开 `http://127.0.0.1:8731/?token=...` 可查看 runs、日志和本地会话历史。
要从手机发送指令，必须显式开启服务端托管会话：

```bash
pi-batch ops serve --control --token-file ~/.config/pbatch/ops-token \
  --agent pi --runtime auto
```

页面中的“移动端会话”可以创建/选择服务端会话、发送有界 prompt、轮询结果并停止
当前本地 turn。历史 memory session 永远只读，不会因为手机请求而隐式 resume 或切换
provider thread。跨局域网监听必须同时配置 token 和 TLS 证书：

```bash
pi-batch ops serve --host 0.0.0.0 --control \
  --token-file ~/.config/pbatch/ops-token \
  --tls-cert server.crt --tls-key server.key
```

不要把 token 放在不可信网络或公共端口；优先使用 VPN/SSH 端口转发。服务端不接受
手机传入的工作目录、agent 可执行文件或任意 shell 命令，所有控制请求均绑定到服务
启动时的 workspace 和 agent。
