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
   dist/pi-batch-linux-x86_64 --version
   ```

3. 合并到 `main` 后创建并推送签名标签：

   ```bash
   git tag -s vX.Y.Z -m "pi-batch X.Y.Z"
   git push github vX.Y.Z
   ```

4. GitHub Actions 的 `Release to PyPI` 会先确认标签提交可从 `origin/main` 到达，
   再重新执行完整门禁、构建 wheel/sdist、在隔离虚拟环境中安装冒烟测试，然后构建
   Linux/macOS Intel/macOS ARM/Windows 二进制并分别冒烟测试。工作流停在 `pypi`
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

## 二进制安装与移动端

从 GitHub Release 下载与系统匹配的 `pi-batch-<platform>-<arch>` 后即可运行：

```bash
./pi-batch-linux-x86_64 --version
./pi-batch-linux-x86_64 ops serve --help
```

二进制不是 provider 的替代品；执行真实任务前仍需安装 `pi`/Codex 等 agent。升级时
只替换二进制，不会删除 `~/.codex/pbatch/projects/` 中的会话和日志。

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
