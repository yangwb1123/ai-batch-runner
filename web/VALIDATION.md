# 前端交付验证记录

## 任务控制增量（2026-11-03）

源码增加显式 `web.control`、本人精确授权的任务 facade，以及 Iris/Svelte 的创建、指令、
事件、取消/关闭和请求历史流程。旧配置默认只读，租户只读策略不能获得正文或写权限。
使用与验收边界见 [WEB_TASKS.md](../docs/agent-hub/WEB_TASKS.md)。没有改线上配置、写真实grant、
启动真实项目Gateway、切服务、提交或推送；原只读候选镜像不包含本次增量。

本轮完整脏工作区基线、每次失败/成功日志、截图和最终命令结果在
`/tmp/abr-web-control.5mmCKC/`；最终交付报告为该目录的 `README.md` 与 `completion.json`。
源码使用原隔离SDK测试venv，不把它当成新镜像依赖/安全验收。
真实Chrome在桌面和手机尺寸通过真实Hub/Gateway/AgentSession操作；issuer/provider是替身，
不是公网本人身份、真实模型或生产项目验收。

首轮完整CI为 **8991 passed in 1214.32s，exit0**（`make-ci.log`，1800秒窗口，源码前后摘要一致）。
收尾复核随后发现：新输入本地校验失败可能保留上次成功回执，导致表单误以为新提交成功。
新增Bun回归先失败，再清除旧回执并标记拒绝；补充真实浏览器断言，保留无效输入且不创建
第二条请求。首轮CI不能替代这次前端修复后的完整验证，复验日志独立记为 `make-ci-02.log`，
不拼接、覆盖或追认旧结果。以下为先前只读阶段的历史证据。

## 只读接入工具（2026-11-02，历史）

**本地实现与完整门禁通过；没有执行真实授权或部署。**
新增`fleet agent --observe-only`（固定项目，仅登记/心跳，无Agent/命令轮询）和
`fleet serve --check-config --json`（共用启动验证器，无网络/数据库/监听）。配置严格读取、
观察/执行状态隔离和Gateway先锁后打开SQLite同步补齐；操作见
[READ_ONLY_ONRAMP.md](../docs/agent-hub/READ_ONLY_ONRAMP.md)。

| 最新实际命令/范围 | 结果 |
|---|---|
| `timeout --signal=TERM --kill-after=10s 1800 make ci`（相同SDK测试venv/报告选项） | **exit0，8948 passed in 1349.15s**；全部8个注册门禁、53 eval、quality/org完成，无pytest清理警告 |
| 观察/离线预检/真实本地HTTP/既有Hub/打包等定向回归 | **378 passed** |
| `make web-check web-browser` | typecheck 0错误/0警告；**29 Bun、5实际本地Chrome通过**；编译资源逐字节一致 |
| `python -m pytest tests/test_tui_review.py -W error::pytest.PytestUnraisableExceptionWarning` | **7 passed**，原断言及严格警告策略保留 |
| `python quality.py --strict .`、`python -m pytest checks/`、`git diff --check` | passed；**61 checks** |
| 实际`python -m pbatch fleet serve ... --check-config --json` | 合成配置exit0，目标数据库目录未创建；不是现场SSO/账号验证 |
| `python tools/check-release.py --tag v0.3.0` | **exit1**：上游许可未决，公开发行仍被阻断；未创建tag |

新测试先失败后实现。第一轮完整CI为8947通过但有1条Textual清理警告；把该警告转为错误
独立诊断得到exit1，没有称为flaky。发现review测试fixture直接关闭循环而未等待自有任务；
补回归复现异步任务/生成器未清理后，以`asyncio.run`完成清理。严格7项通过后，重新跑完
上述整套8948项，不将局部结果拼成全量通过、不修改Textual/产品TUI或屏蔽警告。

两次完整CI各自保持1800秒窗口且期间源码摘要未变；旧结果与失败日志保留于
`/tmp/abr-web-onramp.3hlPZd/`。6个既有鉴权文件及Iris源码保持原样。观察模式的JWT/实例
测试使用本地合成身份，真实账号授权、生产Gateway、服务切换、当前制品安全扫描、远端
Actions、提交/推送和公开发行均未执行；下文旧环境/安全限制仍适用。

## 上一轮发布收尾（历史）

**本地工程门禁已通过；公网真实接入、生产安全验收和公开发布仍未完成。**

本轮实现：
- `pbatch/worktree_creation.py`，由`isolate.py`与`campaign_worktree.py`复用：串行共享Git
  元数据创建，不串行Agent执行；一次创建、30秒共享预算、失败保留现场、不重试不破锁。
- `web/scripts/{build,sources,assets}.ts`与`pbatch/hub_web_provenance.py`：内部资产清单v2
  绑定源码/锁文件/脚本/测试/Bun/Iris；构建前后核对输入，支持不回写的精确重建比较。
- `tools/check-web-build.py`、`Makefile`、`.github/actions/verify-web/`及三条CI/发行工作流：
  默认CI拒绝旧资源；Web与发行流程额外要求固定Bun/Iris重建和本地真实Chrome回归。
- 新回归`tests/test_worktree_creation.py`、`tests/test_web_build.py`、`web/tests/build.test.ts`；
  同步打包文档与[只读MVP发布清单](../docs/agent-hub/WEB_RELEASE.md)。

| 本轮实际命令 | 结果 |
|---|---|
| `timeout --signal=TERM --kill-after=10s 1800 make ci`（SDK测试venv；仅追加短traceback/durations报告选项） | **exit0，8899 passed in 1163.27s**；release/source/check/self-test/eval53/quality/org全部完成 |
| `make web-check web-browser` | Svelte 0错误/0警告；Bun **29 passed**；完整重建与包内字节一致；Chrome **5 passed** |
| Hub/授权/打包/发行/worktree组合定向回归 | **321 passed**；后续完整CI包含最新全部Python源码 |
| `python quality.py --strict .` | passed |
| `python scripts/check-frontend-quality.py --strict <web/src、scripts、tests中全部TS/Svelte>` | passed；同范围复验，无baseline/skip |
| `python scripts/check-ui-spec.py --mode all --dir web/src --ext .svelte --ext .css` | passed |
| `python -m pytest checks/` | **61 passed** |
| `git diff --check` | passed |
| `python tools/check-release.py --tag v0.3.0` | **failed / exit1**：LICENSE中的上游许可未决，仍阻断公开发行；没有创建tag |

完整CI期间源码逐文件摘要未变；没有增大原1800秒窗口、删测试、放宽门禁或更换真实provider
用例。上述Python环境仍是下文的测试venv，不是修复镜像的安全验收。新Actions仅完成本地契约
验证，未推送/触发，不能称远端CI成功。

本轮诊断与失败保留：
- 原失败合成fixture的`adapter/data/sessions.jsonl`记录`worktree add`读取并发创建者的
  `.git/worktrees/core/commondir`失败。首次只有空目录的人工试验未复现（exit0），已有登记
  worktree的空commondir试验复现拒绝（exit128）；再用受控并发窗口验证修复，不追认旧CI成功。
- 新worktree和构建来源测试先缺模块失败，再实现通过。源码保护与所有旧回归保留。
- 扩大到scripts/tests的前端静态扫描首次4项诊断；命令工具明确写stdout，测试参数化为独立
  具名负例并批量建立互不依赖的fixture文件。同范围复验通过；29项Bun仍保留全部原断言。

私有基线、上一轮失败fixture副本、日志与截图：`/tmp/abr-web-release.NJrXc8/`。
既有6个鉴权文件与Iris源码未改。未提交/推送、未替换公网服务、未写grant/注册Gateway。

## 初次实现记录（历史，失败不追改）

### 变更与范围

- `web/`：Svelte5 + Bun，直接消费已定版的 `~/iris-ui` 原生组件和主题；三个只读视图，
  同源 Snaplink 登录/本地退出、窗口筛选、主题切换、桌面/手机布局及完整读取状态。
- `pbatch/hub_web_page.py`、`hub_web_http.py`、`hub_web_assets.py`：同源提供精确资源白名单；
  构建摘要、CSP/SRI 和有界读取，不新增业务/执行/授权规则或任意文件服务。
- `pbatch/hub_web_assets/`：编译资产、来源清单、Iris及运行依赖许可证；安装后不需要Bun/Iris。
- `pyproject.toml`、`MANIFEST.in`、`tests/test_packaging.py`：同步发行资源，仓库外安装检查前端加载。
- 新增 Bun、Hub静态边界及显式浏览器回归；文档见 `docs/agent-hub/WEB_FRONTEND.md`。

开始时 ABR 已有未提交鉴权修改（含租户读取policy及测试/文档/示例），已完整备份并原样保留。
本次没有修改 `hub_auth.py`、`hub_web.py`、原有 `test_hub_web.py` 或这些权限策略，没有改Iris源码。
不建立grant/Gateway、不启用租户全员权限、不重启FRPC，也没有替换公网运行服务。

### 初次实际通过

| 命令/范围 | 结果 |
|---|---|
| `cd web && bun run check` | 0错误、0警告 |
| `cd web && bun test tests` | 13 passed |
| `cd web && bun run build` | Vite生产构建、CSP摘要、SRI、运行资源发布成功 |
| `python -m pytest tests/test_hub_web_assets.py tests/test_hub_auth.py tests/test_hub_web.py tests/test_hub_web_http.py tests/test_hub_web_transport.py tests/test_hub_web_read_grants.py` | 152 passed |
| `python -m pytest tests/browser_hub_web.py tests/browser_hub_web_ui.py` | 5 passed；真实Chrome、实际本机Hub/TLS/SQLite/SDK，issuer/API场景按测试使用替身 |
| `python -m pytest tests/test_packaging.py` | 11 passed；完整CI超时之前也已完成此模块，包含新的仓库外前端加载检查 |
| `python quality.py --strict .` | passed |
| `python scripts/check-frontend-quality.py --strict <web/src下全部TS/Svelte文件>` | passed，无baseline/skip豁免 |
| `python scripts/check-ui-spec.py --mode all --dir web/src --ext .svelte --ext .css` | passed |
| `python -m pytest checks/` | 61 passed |
| `git diff --check` | passed |
| `make eval quality org`（独立执行） | 53 eval通过，quality/org/capabilities/graph通过 |

浏览器还检查原生表单退出、CSP不放宽、主题、键盘跳转、文本不解释为HTML、筛选、授权空窗口、
部分失败清除旧数据，以及375px手机下长文本不撑破文档。截图是本地/合成数据，不是公网或真实用户。

Python验证使用既有隔离测试venv（Python3.10.7、Snaplink0.3.0、PyJWT2.12.0、cryptography46.0.7），
不是已修复运行镜像，也不作其安全验收；没有修改系统安装。Bun1.4.2、Svelte5.56.8、Chrome156。

### 初次完整 CI 没有通过

`PATH=<SDK测试venv>/bin:$PATH timeout --signal=TERM --kill-after=10s 1800 make ci`
实际 **exit124**。release-check、check、self-test通过；pytest收集8865项，在约96%进度被整窗截止终止。
日志中 `tests/test_collab.py` 的第二个测试曾出现F；超时使该轮没有完整traceback/最终汇总。

随后该文件独立复跑 **3 passed**，仅是诊断，不能追认第一次成功、断言flaky或证明已修复。
截止处及之后明确选择的尾部测试独立运行 **266 passed**，也不能拼装成全量通过。
后续eval/quality/org另行执行成功，但初次当时**仍无完整make ci PASS，未获发布/生产准入**。
没有为了使其变绿延长该轮窗口、删除断言、修改collab/TUI实现或放宽质量预算。

### 初次早期失败与真实修复

- 新读状态/资源测试先于实现运行，缺模块失败记录保留。
- 系统python缺Snaplink，首次Python/浏览器检查在收集期失败；改用已有SDK测试venv，不改全局安装。
- 资源测试漏导入依赖fixture `signed`：143通过/9 setup错误；补齐fixture导入后152通过。
- 首次类型检查8错误/2警告：使用正确的可调用Fetcher类型、按Iris真实Card接口传参，去除
  非交互元素手工tabindex；后续类型检查0错误0警告，无类型忽略或规则关闭。
- 浏览器发现退出前清理store导致原生form被卸载，默认POST无法启动。修复为保持表单连接，
  由pagehide清除旧文档状态；原始登录/退出断言未改，之后完整流程通过。
- UI规范最初16项字体/间距问题，按现有规范统一字号和空间尺度后通过；没有改规则/阈值。
- 两个旧脚本不支持 `--help`，探测被当作缺失目标拒绝；该输出不是有效扫描，随后按真实CLI
  对明确的Svelte/TS/CSS范围执行并通过。

## 仍未执行（不由本地CI通过替代）

- 公网部署、真实Snaplink本人登录、固定项目/Gateway、真实越权/退出及边缘凭据日志验收。
- 当前运行镜像重新扫描、发布签名、提交/推送、版本变更、镜像发布或K8s apply。

本次实际命令输出、失败初版、完整脏工作区基线与截图保存在私有
`/tmp/abr-svelte-ui.EumVA6/`；该目录不是发行资产或独立签名证据。
