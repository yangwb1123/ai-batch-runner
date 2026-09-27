# 发布 `pi-batch`

产品有两个稳定名称：PyPI distribution 是 `ai-batch-runner`，用户安装后执行的
命令是 `pi-batch`。版本唯一事实源是 `pyproject.toml`。

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
   实例锁写入 `~/.codex/pbatch/projects/<workspace-id>/`，不会在被操作仓库创建
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
无需创建标签。修改运行时代码、打包配置、内置资产或验证脚本的 pull request 也会
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
冲突测试，再独立构建并执行隔离 smoke；Linux 额外执行同一 TUI 进程的短时故障恢复。
构建、验证及各 job 均有超时。成功与失败都会尝试保留 14 天的
`pi-batch-verification-<platform>` 工件，包含平台与源码提交、步骤结局、文件锁与构建日志、
`smoke.json`、`smoke.log`，以及 Linux 的 `soak/` 证据。未启动的步骤在结局中显示
`skipped`，不会伪装成通过；job 被强制终止或 runner 丢失时可能无法上传诊断。

只有全部必要检查通过的平台才上传 `pi-batch-binary-<platform>` 工件。正式发布仅
下载这个名称前缀，诊断文件不会混入 Release 二进制及校验和。Windows smoke 明确
记录 POSIX PTY 跳过，不能据此声称 Windows TUI 交互或整个进程树回收已经验收。
工作流配置和本机契约测试不代表远端四个平台已经通过；以对应 Actions 运行工件为证。
当前这轮本机验收只覆盖 Linux，macOS 与 Windows 仍缺少本轮真机通过证据。
Windows job 也用于暴露实际移植缺口；其失败会阻止发布，不设置允许失败或假跳过。

## 二进制安装与移动端

从 GitHub Release 下载与系统匹配的 `pi-batch-<platform>-<arch>` 后即可运行：

```bash
./pi-batch-linux-x86_64 --version
./pi-batch-linux-x86_64 ops serve --help
```

二进制不是 provider 的替代品；执行真实任务前仍需安装 `pi`/Codex 等 agent。升级时
只替换二进制，不会删除 `~/.codex/pbatch/projects/` 中的会话和日志。

在没有项目 `pi-batch.yaml` 的目录启动时，包内配置属于默认层，用户
`settings.yaml` 中的 provider/agent 设置可以覆盖它。优先级为 CLI 参数、项目配置、
用户配置、打包默认。现存配置读取或 YAML 解析失败会拒绝普通执行；使用
`pi-batch config check --json` 或 `pi-batch doctor --json` 查看文件位置和诊断，
错误信息不展开可能含凭据的 YAML 正文。

每份配置最多读取 2 MiB，`config check --max-bytes` 只能收紧这个上限。YAML 树
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
