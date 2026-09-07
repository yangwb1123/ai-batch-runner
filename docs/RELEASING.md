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
   python -m pip install -e ".[dev]" build twine
   python tools/check-release.py --tag vX.Y.Z
   make ci
   python -m build
   python -m twine check dist/*
   ```

3. 合并到 `main` 后创建并推送签名标签：

   ```bash
   git tag -s vX.Y.Z -m "pi-batch X.Y.Z"
   git push github vX.Y.Z
   ```

4. GitHub Actions 的 `Release to PyPI` 会先确认标签提交可从 `origin/main` 到达，
   再重新执行完整门禁、构建 wheel/sdist、在隔离虚拟环境中安装冒烟测试，然后停在
   `pypi` Environment 等待批准。批准后才上传 PyPI。
5. 从公共索引验证：

   ```bash
   pipx install ai-batch-runner==X.Y.Z
   pi-batch --version
   pi-batch doctor
   pi-batch tui
   ```

PyPI 文件不可覆盖。错误版本应在 PyPI yank，并立即发布修复版本；不要删除标签后
复用相同版本号。
