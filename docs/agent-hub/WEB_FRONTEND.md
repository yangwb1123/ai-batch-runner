# ABR Web 前端：Iris UI + Svelte + Bun

## 范围

面向浏览器中的项目操作者：通过已有 Snaplink SDK 登录，查看授权实例与会话，并在
显式开启任务控制且精确授权后创建任务、连续发指令、跟踪输出、查历史、处理失败及取消/关闭。四个视图是工作台、
授权实例、会话记录、Agent任务。流程和授权要求见 [WEB_TASKS.md](WEB_TASKS.md)。
Agent任务可按最近执行结局、项目和关键词在服务端筛选并分页，失败结局与会话空闲状态分开显示。
明确分辨未登录、无权、加载、空窗口、超时、限流、冲突和结果未知；不扩大权限、不扫描项目。

使用本机 `~/iris-ui` 的原生 Svelte 组件：Button、Badge、Card、Icon、ThemeProvider。
不复制 Iris 组件、不修改该仓库，不依赖尚未授权的 npm 首次发布。Iris 的 MIT 许可证、
Svelte/运行依赖许可证及源码 revision 随构建资产携带。此举不解除 ABR 原有发行许可门禁。

**前端不决定授权、不持有 token；同源任务 facade 复用原 Hub 执行接口，不代表已完成公网部署。**
登录/本地退出仍是同源原生 POST；官方 Python SDK 负责 PKCE/state/兑换，Hub 逐次鉴权。
没有密码表单、静默续签、自动重试写入、任意文件读取、修复或 Gateway 注册按钮。
任务正文与控制要求精确 principal grant，租户只读策略不会获得这些能力。
权限策略见 [WEB_LOGIN.md](WEB_LOGIN.md)，前端构建不改变已有配置或授予权限。

## 构建

从源码 checkout 构建，Bun 1.4.2；Python 安装后的服务不需要 Bun、Vite 或 Iris checkout。

```sh
cd web
bun install --frozen-lockfile --ignore-scripts
bun run check
bun test tests
bun run build
bun run build:check   # 重新编译并逐字节比较；不修补被检查的发行资产
```

默认 Iris 位置是 `~/iris-ui`，可显式指定 `IRIS_UI_DIR=/path/to/iris-ui`。
`web/iris-source.json` 记录准确源码 commit；构建前要求对应源码干净并匹配该 commit。
`setup:iris` 只创建忽略的 `web/.iris` 本地链接，不在 Iris 仓库安装依赖或生成 dist。
更新 Iris 必须显式评审并更新来源 revision，再重跑前端及 Hub/浏览器门禁。

- `bun run dev`：仅127.0.0.1上的 Vite 开发页，不是公网服务，不代理其它产品凭据。
  脱离 Hub 的开发页没有真实同源 API/登录服务；不能冒充可用 SSO。
- `bun run build`：Vite 构建后发布到 `pbatch/hub_web_assets/`，HTML/JS/CSS 文件固定命名，
  无 source map。内部资源清单 v2 绑定源码、构建脚本、测试、锁文件、Bun版本和Iris revision；
  构建前后核对输入，manifest 最后提交，不隐式应用到运行服务。
- `bun run build:check`：使用同样的编译流程，只比较已交付资源的完整字节集合，不修改它们。
  两种构建共享180秒执行预算与独占 `.build-lock/`；崩溃遗留锁须核对拥有者后人工处理。
  摘要不是独立签名，源码一致检查也不能替代实际重建、测试或授权。
- 正式使用仍是 `abr fleet serve --config /private/hub.yaml --host 127.0.0.1 --port 8788`。
  配置真实身份/授权，反代沿用现有 HTTP/customDomains；这不是另建 Bun 执行后端。

`pyproject.toml` 与 `MANIFEST.in` 明确携带编译资源；wheel/sdist 安装不现场下载组件或构建。
缺失、超限、链接、坏清单或摘要不匹配时不降级为伪成功页。只公开 `/web/assets/app.js` 和
`/web/assets/app.css`，没有任意路径静态文件服务器，静态文件也不赋予 Bearer API 权限。

## 数据和安全边界

- 实例/概况列表每类最多100条，任务目录每页20条，单响应2MiB、最多1024个读取块，每次完整读取窗口12秒。
  任务页可见时有界只读同步，隐藏时停止；默认2秒间隔，失败退避/停止，不自动重放写入。
- 同源 Cookie facade 使用白名单 GET/显式 POST，禁跟随重定向；不把 Cookie 转成 Bearer，不读取或保存 token。
  POST 有提交锁、冻结的幂等key/目标/正文和登录context绑定；未知结果阻断新写入，不自动重试。
  任务窗每页50条事件、20条请求/轮次记录；事件增量最多保留500条/1MiB正文，明确标记截断。
- 概览刷新先清除旧事实；任务同步期间保留当前内容并标记同步状态，前后核对同一授权上下文。
  读取失败清除事实窗口；撤权/换账号另清草稿与选择，不把部分成功伪装成完整成功。
  任务目录游标绑定授权和筛选，分页替换列表并保留当前任务详情；只保留32个返回游标，
  无效分页在显式刷新时从首页恢复。搜索输入只在提交时查询，不自动重试写入。
  轮次历史复用已有会话接口提供脱敏文本/状态搜索、精确 ID 定位和有界返回分页。
  搜索结果是带读取时间的手动窗口，当前已选轮次仍独立刷新；坏条件重置后可手动恢复。
  卸载/pagehide 取消自有请求，BFCache 恢复重新鉴权，旧响应不能覆盖新状态。
- 未读取不是0；空授权窗口不是项目已接入；未知心跳不是离线。费用、独立验收、审计为未追踪。
  结果检查只显示当前轮次的结构化执行记录；未知退出码、缺少终态和矛盾证据不会变成通过。
  结果工作区对已读取记录做有界搜索、状态筛选和每页20条分页，异常数量不替代目标验收。
  引用草稿绑定身份、轮次及完整记录字段；用户编辑的是新增段落，根草稿 owner 同步复核后
  追加到当时的原稿，16KiB超限拒绝。稳定轮询保留编辑，依据变化或切换清除引用；不自动发送。
  记录下载仅序列化当前已读取窗口，按字段白名单排除登录上下文/待提交内容，2MiB超限拒绝。
  命令输出仅在Gateway显式开启共享后显示终态快照，默认不发布；每份最多8KiB，
  界面全部输出共享256KiB预算。可展开并复制，未提供/空文本/截断保持区分；不自动读取文件或完整日志。
- 服务端名称和消息仅作有界文本渲染，清除控制/bidi字符，前端不解释 HTML 或服务器错误正文。
  回复按匿名消息标识合并增量/最终快照；命令/文件事件是执行摘要，不是文件浏览或独立交付验收。
- 文件检查使用独立 `agent.artifacts:read` 和默认关闭的 `web.file_snapshots`。
  Gateway 精确 allowlist 采集的不可变文件通过独立接口读取，网页验证绑定、字节数和 SHA-256 后
  纯文本预览或下载。每次下载重新鉴权，不复用旧授权；切换上下文及 pagehide 中止请求并清除内容。
  原始文件不经过事件正文脱敏再冒充摘要一致。大小、路径、存储容量及采集期限见
  [文件检查契约](WEB_TASKS.md#文件检查快照显式启用)。
  同一任务可显式选择另一轮作基准，读取双侧不可变快照进行有界逐行比较及 JSON 导出；
  每次导出重新取双方内容并鉴权，保留原始换行/BOM，单侧缺失不推断文件新增或删除。
  计算上限、历史窗口与证据语义见 [文件版本对比](WEB_TASKS.md#同一任务的文件版本对比)。
  回复格式预览采用有界、转义的Svelte结构节点，保留原文/代码复制；不使用HTML注入、远程图片、
  新Markdown依赖或宽松CSP。超出格式预算回退当前原文，不改变既有对话正文预算。
- JavaScript 使用精确 bundle hash 的 CSP 与 SRI；CSS 仅允许同源文件和两段被准确哈希的
  Iris 单例 stylesheet。没有 unsafe-inline/unsafe-eval，不因组件库放开脚本/样式权限。
- 主题与草稿仅保存在有界内存；没有 browser storage、Service Worker 或认证数据离线缓存。
  草稿文件入口仅在用户点击时下载编辑框文本、读取选择的本地 UTF-8 `.txt` 并预览。
  文件读取最多 64KiB/128块/5秒，身份变化与卸载取消自有 reader；稳定刷新保留预览。
  加入操作由根草稿 owner 同步核对当前目标并追加到最新原稿，同一预览成功后消费；
  原64KiB草稿预算与16KiB发送预算不变，无上传、自动恢复、自动发送或授权状态序列化。
  任务深链接只包含会话及可选轮次标识，刷新/前进后退重新读取授权数据，不携带token、prompt或可重放写入。
  task owner 区分 live/fixed：任务级链接跟随当前活动或未筛选最新轮次，精确轮次链接固定阅读。
  历史搜索/分页与现场目标分开；同会话回到现场保留草稿和查询。单轮视图只读取选中轮次的事件，
  切换时清除旧事件窗口。多轮回顾显式读取独立的五轮窗口，每轮最多两页共100条事件，
  整个回顾窗口最多500条/1MiB，按轮分配字节预算并保留各自的后续/裁剪/缺片标记。
  逐轮复用对话投影，不跨轮合并相同消息标识；回顾分页和读取时间独立于当前轮次及历史查询，
  普通现场同步不替换回顾快照。所有读取仍共用原授权前后核对、取消和12秒执行窗口。
  任何导航请求立即卸载旧详情的文件 reader，不等待主请求结束。
  尚未确认的写入继续受原目标/幂等账本约束，不因导航撤销或重发。

## 验证入口

```sh
cd web && bun run check && bun test tests && bun run build
cd ..
python -m pytest tests/test_hub_web_assets.py tests/test_hub_web.py tests/test_hub_web_http.py tests/test_hub_web_transport.py
python -m pytest tests/browser_hub_web.py
python -m pytest tests/test_packaging.py
make web-assets-check # 标准库：当前源码与包内摘要一致；不需要Bun/Iris
make web-check        # Svelte检查、Bun测试、精确重建比较
make web-browser      # 本地真实Chrome，合成issuer
make web-ci           # make ci + web-check + web-browser；不部署
python quality.py --strict .
python -m pytest checks/
git diff --check
```

`make ci` 必含离线 `web-assets-check`，但它不替代 Bun 编译或浏览器。
CI 的独立Web job及两条公开发行流程复用 `.github/actions/verify-web`：固定Bun和Iris commit，
冻结锁文件安装、精确重建及浏览器回归，任何失败阻断后续；无权限扩展、Secrets或部署步骤。
工作流声明不代表远端运行成功；完整生产准入另见 [WEB_RELEASE.md](WEB_RELEASE.md)。

浏览器测试使用真实 Chrome/HTTP/SQLite/官方 SDK，但身份服务是测试替身；自签名本地证书的
TLS忽略仅属于该测试上下文，不能当作生产 TLS/FRP/SSO 验收。构建、单测、静态扫描、截图与
合成 issuer 均不能证明真实账号登录、工作区/Gateway、边缘日志或生产验收已经完成。
