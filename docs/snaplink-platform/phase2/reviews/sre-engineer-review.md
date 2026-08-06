# SRE 工程评审 — 第二阶段（可检测 / 可承受 / 可恢复）

> 角色：sre_engineer（`prompts/README.md` 应用）。评审对象：`docs/snaplink-platform/phase2/kickoff.md`
> + 部署实录 `DEPLOYMENT_192.168.123.52.md` + `TESTING_GUIDE_192.168.123.52.md` +
> `ERP_SCENARIO_ANALYSIS.md` + 同轮 devops/security/database 评审（交叉引用，不重复罗列）。
> **本会话实际执行的检查**：sverpweb-stable `abd8801`、snaplink `d19838d8`、
> snaplink-audit-governance `476d083`、aero-vault `c0ce6db`、snaplink-console `0c21dca`
> 的静态读码（health/readiness/metrics/graceful shutdown/retry/backoff/超时/限流）；服务器
> 运维资产实测副本 `/tmp/deploy-build/test-platform.sh`（151 行，与服务器版本一致）；
> `examples/snaplink-platform/platform-247.sh`；grep 扫描（setInterval/setTimeout/
> metrics/healthz）。**未执行**：服务器侧 systemd 单元内容、journald、真实流量观测。
> 结论标注 **Verified**（本会话实证）/ **Partial** / **Unknown** / **Proposed**。
> 本评审为咨询性分析，不构成发布批准。

## 1. 服务/依赖图与运维假设

```
[浏览器] ─HTTP→ nginx/snaplink-console :13017（Flutter Web 静态，无代理无 TLS）
[浏览器] ─HTTP→ sverpweb-stable BFF :13014（Hono/Bun，唯一业务入口 + 静态托管）
              │  ├─ snaplink-sverp :13015/:13016（OIDC/认证，重启即轮换签名密钥）
              │  ├─ audit-governance :13018（审计事实源，单节点 state.json）
              │  ├─ aero-vault :13019（媒体 + presign）
              │  └─ aero-im :13022（通知，best_effort）
              └─ 本地存储：app.sqlite（业务+outbox+媒体元数据）、auth.sqlite（会话）
[脚本/curl] ─HTTP→ :13015 / :13018 / :13014（运维探测面）
基础设施：PG15(aero_id/aero_im)、Redis、NATS JetStream :13023 —— 同一台主机
```

**运维假设（当前唯一受证据支持的部署形态，Verified）**：
- 单主机 192.168.123.52 全栈（8 服务 + 基础设施全在一台物理机/VM）；capistrano 风格
  current/shared；systemd 加固单元 + GOMEMLIMIT 有界（Verified，DEPLOYMENT §2）。
- **检测面**：唯一"告警"渠道 = 人工跑 `test-platform.sh`；7×24 循环（platform-247.sh）
  只做滚动分析 + 失败任务重试，**无运行时健康阶段**（Verified，脚本全文）；无 Prometheus、
  无告警 sink。governance 有 /metrics（Verified `server.go` metrics handler），vault 有
  /metrics，但**从未被采集**。
- **恢复面**：重启 = 全链认证中断（1.2.1 根因）+ 审计 401 窗口（2min timer 兜底，
  Verified TESTING_GUIDE §5）；备份零脚本；回滚 = 切 previous symlink（模型成立，
  devops C1/H3 已述，未脚本化）。
- **SLO/RTO/RPO**：部署平台**无任何已文档化的 SLO/RTO/RPO**。governance 仓
  `docs/SLO_ALERTS.md` 是**另一架构**（Kafka/ClickHouse 多节点）的 phase-0 草稿，
  且明确声明"跨区 RPO/RTO ≤60s/≤15min 未在本机验证、不得宣称"（Verified 文档原文）——
  单节点文件账本部署不继承该文档任何数值。

## 2. 就绪度表（信号 × 依赖 × 失效行为 × 告警 × runbook）

| 服务 | 信号（现状） | 依赖 | 依赖失效行为（Verified） | 告警（现状） | runbook（现状） |
|---|---|---|---|---|---|
| sverpweb BFF :13014 | `/health` = 静态回显（app.ts:172，不碰 DB/下游）；**无 /readyz、无 /metrics、无请求日志中间件**；运维脚本探 `:13014/healthz` → **SPA catch-all 返回 200 index.html = 假阳性探针**（app.ts:197-205，Verified） | snaplink(认证)、governance(审计)、vault(媒体)、im(通知) | 全部**惰性异步**：启动不检查下游；governance 挂 = outbox 积压（无告警）；snaplink 挂 = 登录失败（页面可见）；im 挂 = 通知静默丢弃（console.warn 仅日志） | 无 | 无；`check-outbox.py` 人工执行 |
| snaplink-sverp :13015/:13016 | **minimal 形态无 /healthz、无 /readyz、无 /metrics**（flow_test.go 断言 /metrics→404，Verified）；脚本只查端口 | 无（自含 sqlite/内存） | 重启 = 密钥轮换 → governance/vault 验证失败 + 审计 401 窗口（2min timer 收敛，客户端 401 强制重试兜底） | 无（端口掉了 systemd 无感知？有 Restart=on-failure 未知——服务器侧 Unknown） | sync-sso-key.sh + timer（未版本化）；TESTING_GUIDE §5 FAQ |
| governance :13018 | /healthz + /readyz（检查 archive 依赖）+ **/metrics（有界计数器+延迟直方图，Verified）**；脚本只测 401 门禁，**不采集 /metrics** | 无外部依赖；自身 = 单节点 state.json | 挂 = 审计投递 401/连接拒绝 → outbox retryable 退避（2s→5min 上限，Verified client.ts）；**静默**：无人知道 pending 在涨 | 无 | 无（"查 check-outbox.py"） |
| aero-vault :13019 | /healthz+/readyz+/metrics + 限流中间件（探针旁路，Verified auth_middleware.go:107） | snaplink JWKS | snaplink 重启 → JWKSCache 过期 → 401 → BFF 侧 presign 签发失败（用户可见） | 无 | 无 |
| aero-im :13022 | 无探针（脚本只查端口）；配置错误会启动 OTLP exporter（DEPLOYMENT 注明） | PG/Redis/NATS | 挂 = 通知静默丢失（best_effort，业务不受阻，设计如此） | 无 | 无（设计上可容忍） |
| console :13017 | **无任何探针**（脚本 9 单元 11 端口清单无 console HTTP 检查，Verified）；nginx SPA `try_files → /index.html`（任何路径 200） | 无 | **曾 inactive 一次，根因未记录**（kickoff 2.5）；无 Restart=on-failure 证据 | 无 | 无（kickoff 2.5 处置 Proposed） |
| 审计 outbox（BFF 内） | **无进程内周期 drainer**（client.ts:285 注释明示"deliberately does not start a timer"）；flush = append 触发单飞 + 启动一次 + 手动 `/api/admin/audit/outbox/flush`（audit-admin.ts:214） | governance | governance 恢复后**需下次业务写或人工 flush 才重试**——"drainer 60s 清空"在代码中无定时器支撑（Verified） | 无 | 手动 flush 端点（权限门禁） |

## 3. Findings（按严重度）

### Critical

**C1 — "检测"层整体缺失：BFF 唯一探针为假阳性，console/snaplink 零探针，全平台零告警（Verified）**
- 证据：① `test-platform.sh` status 对 `:13014/healthz` 断言 200，而该路径落入
  `app.get("*")` SPA fallback 返回 index.html（app.ts:197-205）——API 进程内 DB 损坏、
  认证循环故障、outbox 积压时该探针**依然 200**；② console 不在任何探针清单
  （脚本 9 单元 11 端口 + 4 健康端点，无 :13017）；③ snaplink minimal 无健康端点
  （flow_test.go:52 断言 /metrics 404）；④ governance /metrics 存在但无人采集；
  ⑤ platform-247.sh 每轮只做分析+重试，无健康阶段（脚本全文 Verified）；⑥ 全平台无告警
  sink（aero-im 告警通道是 kickoff 4.1 的 Proposed）。
- 影响：SRE 三职责之首"detect"不存在。审计链路中断（L2 的核心承诺）只能靠人工
  `check-outbox.py` 发现；BFF 半死（web 正常、API 失效）时 status 全绿——**假阳性
  探针比没有探针更危险**（提供错误的安全感）。
- 修复：① 探针改用真实端点：BFF 加 `/readyz`（检查 app.sqlite 可写 + outbox pending
  数/最大年龄），脚本改探 `/health` 并断言 JSON `status=="ok"`，删掉对 `/healthz`
  的 200 断言；② console 加 HTTP 200 + 静态资源指纹探针（nginx 层加
  `location = /healthz { return 200; }` 或检查 index.html sha256）；③ snaplink 加
  最低限度 `/healthz` 或脚本用真实协议探针（POST /token 期望 401）；④ platform-247
  每轮追加 `test-platform.sh status` 阶段（kickoff 4.1#2）；⑤ governance /metrics
  接入逐轮抓取（一行 curl 落 summary）。
- 恢复验证：人为 `systemctl stop audit-governance` → 2 轮巡检内 summary 出现告警记录
  （kickoff 4.1 验收）；BFF DB 锁死注入 → status 必须红。

**C2 — 审计真源（governance state.json）无 fsync、无备份、无恢复演练（Verified；与 devops C3 / DB C1 合并）**
- 证据：`store.go:251-254` WriteFile+rename **无 Sync()**（其 archive 侧有 fsync，
  archive.go:52——对照鲜明）；零备份脚本；SLO_ALERTS.md 的 RPO 承诺属于未部署架构。
- SRE 视角补充：**"恢复"是审计承诺的组成部 分**——L2 宣布的隐式前提是"账本可恢复"，
  当前连"备份文件存在"这个最低信号都没有。RPO 事实上 = ∞。
- 修复：4.3 最高优先级 + fsync 补丁（DB C1 已给出代码级方案）；`test-platform.sh
  backup` 子命令 + 月度恢复演练；**单写者纪律文档化**（禁止第二个 audit-api 进程）。
- 恢复验证：kill -9 注入 ingest 循环 → 重启后 VerifyIntegrity 通过且事件数=杀死前；
  从备份恢复后查询/E2E 全绿。

**C3 — 密钥轮换语义缺陷使"回滚/重启"这一恢复动作本身成为故障注入（Verified；与 devops C2 / DB C2 合并，补充恢复面视角）**
- 证据：重启即轮换（05 §4）；回滚机制 = 切 previous + **重启服务**（devops 4.6）——
  即**每次回滚都触发全链认证失效 + 审计 401 窗口**；治理依赖 2min timer（未版本化
  资产）。sso-minimal 有优雅停机（signal.NotifyContext + Shutdown 15s，server.go
  Verified）——但优雅停机不解决密钥轮换问题。
- 影响：恢复操作自带破坏性副作用；"回滚止损"在 1.2.1 前不是净收益操作（回滚本身
  造成新一轮中断窗口）。
- 修复：1.2.1（新代码，密钥文件 load-or-generate）+ 吊销集持久化（security F2，
  验收必须并入）+ 重启演练固化为 test-platform.sh `rotation` 子命令；**1.2.1 落地前
  运维纪律：非必要不重启**（devops 已述，本评审从恢复面再次确认）。
- 恢复验证：重启后 kid 指纹不变 + 三套件全绿 + 吊销 token 仍被拒（F2 断言）。

### High

**H1 — 审计 outbox 无周期 drainer：重试被"下一次业务写"门控（Verified，本评审独有发现）**
- 证据：`client.ts:285` 注释明示 publisher 不开定时器；flush 仅由 append 触发
  （ForwardingAuditSink.trigger，forwarding.ts:35-53）+ 启动一次 + 手动 flush 端点；
  全仓 grep 无 setInterval/setTimeout 定时循环（Verified）。kickoff/测试文档中的
  "drainer 60s 清空"（ERP_SCENARIO §2、kickoff 1.2.3 验收）**在本修订版代码中无
  定时器支撑**——e2e 的 sleep 5 之所以能过，是因为创建质检单的 append 本身触发了 flush。
- 影响：governance 停机恢复后，若恰逢低流量时段（无新业务写），积压事件可滞留数小时；
  无人告警（C1）。审计交付 SLO（若定义 60s）无法达成。
- 修复：进程内周期 drainer（30–60s 间隔，幂等 + 单飞已具备）或 systemd timer 调
  flush 端点；把 pending 数/最大滞留年龄暴露为 `/readyz` 的一部分（C1 联动）；
  L2 验收"pending=0 24h"依赖此机制存在。
- 恢复验证：停 governance 5min → 重启 → 60s 内 pending=0（无需任何新业务写）；
  故障注入测试写入 test-platform.sh e2e 断言。

**H2 — 部署平台无 SLO/RTO/RPO 定义，恢复目标不可度量（Verified 现状 + Proposed 处置）**
- 证据：全平台文档无 SLO；governance 仓 SLO_ALERTS.md 是未部署架构的草稿且自声明
  "不得宣称"（Verified 原文）；kickoff 4.4 唯一量化 = e2e 最大失败窗口 ≤2min（timer
  同步前），1.2.1 后为 0。
- 处置（Proposed，需 owner 决策）：① 可用性 SLO：BFF 入口（:13014）月可用性 ≥99.5%
  （单机内网量级，先定义再测量，勿虚构更高值）；② 审计交付 SLO：事件 accepted→ledgered
  p99 ≤60s（把"drainer 60s"从口头承诺变为可测量指标，H1 落地后成立）；③ RPO/RTO
  决策：governance RPO = 备份频率（日备份=24h 丢失上限，或加实时拷贝降到分钟级——决策
  项）；RTO = 恢复演练实测值（目标 ≤1h，含机器重建 ≤4h——需版本化资产支撑，devops C1）；
  ④ 重启窗口预算：1.2.1 前 ≤2min/次、1.2.1 后 0。
- 验证：上述数值写入部署文档后，test-platform.sh 新增对应断言（如 drainer 窗口）。

**H3 — 本地 outbox 无界增长：每笔变更请求都落一行且永不清理（Verified）**
- 证据：request-middleware.ts:95-135 对每个变更类请求写 http.request.* 事件 →
  transport 跳过 → 本地标 published（client.ts:148-155）；sqlite-outbox.ts 无任何
  DELETE/清理路径（DB 评审 M2 同源）；BFF 无 /metrics 无法观测行数。
- 影响：app.sqlite 按请求量无界增长（磁盘饱和是单机平台的终局故障）；"pending"健康
  信号被噪音行污染（清理前 pending 是只读信号）。
- 修复：published 行保留策略（如 90d）清理 job（计数入观测）；或 http.request.*
  降级为纯计数器不入 outbox（业务事件才入）；M2 的 governance retention 策略验证
  同步进行。
- 验证：清理 job 上线后 published 行数曲线下降；`/readyz` 暴露 outbox 行数。

**H4 — 边界配置 fail-open + 无过载防护（Verified；security F3/F8 的 SRE 面）**
- 证据：env.ts:61-68——`IDENTITY_MODE` 任意值静默落 snaplink 模式、`AUTH_STORE`
  回退 memory、`COOKIE_SECURE` 默认 false；BFF 无限流/无并发上限/无队列边界；
  `maxRequestBodySize: 256MB`（index.ts）；SQLite busy_timeout 5s 是唯一护栏；
  presign `expires` 客户端可控最长 7 天（F4）；vault `AUTH_PRESIGN_SECRET` 空 =
  每次重启随机（config.go:205）→ 在途能力 URL 全部失效（可用性）。
- 影响：配置漂移时**静默降级**（内存会话/静态 token 回退，devops H5 同源）；饱和时
  无背压信号；vault 重启 = 在途上传/下载链接集体失效。
- 修复：1.2.2 配置 fail-closed（启动即拒未知/演示键）；BFF 或 nginx 层限流（LAN 量级
  简单计数即可）+ 上传走 presign 后收紧 body 上限；F4 的 expires 钳制；AUTH_PRESIGN_SECRET
  固定 32+ 字节并入 4.2 密钥清单（F9）。
- 验证：错误配置启动 → 进程拒绝；`expires=999999` → 被钳制；vault 重启后旧 presign
  URL 仍有效（新断言）。

### Medium

**M1 — 运维资产未版本化：恢复工具本身不可复现（Verified；devops C1 的恢复面）**
- 证据：`/tmp/deploy-build/test-platform.sh`（151 行）与 sync-sso-key.sh 只活在服务器；
  本仓无 ops/deploy/；kickoff 3.3#8 自认。**恢复 runbook 的每一步都依赖服务器上未版本化
  的脚本**——机器重建时这些资产随机器一起消失。
- 修复：入 suite 仓库（ops/deploy/ + scripts/），Change Manifest 门禁；`systemd-analyze
  verify` 一致性校验。
- 验证：从版本库检出脚本在服务器运行全绿。

**M2 — console 无探针无自愈（Verified；kickoff 2.5 确认）**：曾 inactive 一次且根因未记录
（learn 循环没生效）；nginx SPA 天然 200。修复见 C1② + Restart=on-failure；验证：
`systemctl kill -s KILL snaplink-console` 后 5s 内恢复 + status 含 console 断言。

**M3 — 容量包络缺失，饱和演练无从设计（Unknown→需测量）**：无负载证据（DB 评审 §5
同源）；governance 整快照重写 O(ledger) 的写放大与 outbox 行数、媒体字节量均未测量；
BenchmarkIngest 未跑。修复：发布包络（10³/10⁴ 事件量级 ingest p99 + state.json 字节/事件），
定义磁盘/内存增长预算；验证：容量报告 + 一次人为写放大观察。

**M4 — 7×24 巡检无运行时健康阶段（Verified）**：platform-247.sh 全文只做分析+重试；
"平台 status 看板"与"告警 sink"均为 kickoff 4.1 Proposed。修复即 4.1；验证：连续 3 轮
无 SKIP + 人为停 governance 2 轮内 summary 告警（kickoff 验收原样）。

### Low / Info

- **L1**：脚本单元清单与 kickoff"8 服务"计数漂移——脚本检查 9 个单元（含 `sverpweb`，
  疑为旧实例残留）+ nats，DEPLOYMENT §3 也列 9 个名字却写"8 个"。资产清单需对齐，
  多余运行单元 = 未管理暴露面（Info，需服务器核实 `sverpweb` 单元是否在用）。
- **L2**：governance /metrics 已现成（计数器+直方图，有界基数）——一行 curl 抓取即可
  获得错误率/延迟/去重/冲突/完整性计数，是当前性价比最高的观测增量（无 Prometheus 也
  可落 summary）。
- **L3**：BFF 审计信封已有 operationId/requestId/traceId（contracts.ts:181-183，
  X-Request-Id/X-Trace-Id/traceparent 透传）但**无跨服务传播**（下游只收服务账号 token，
  不接收 trace 头）——单跳调试靠 requestId 足够，跨服务根因定位需在接 vault/governance
  时补传播或明确不支持的决策（Proposed）。
- **L4**：snaplink minimal 有界输入（maxBody 1MB / token 16KB / scope ≤32，Verified
  app.go:30-35）+ 完整超时（ReadHeader 10s/Read 30s/Write 30s/Idle 120s）+ 优雅停机
  15s（server.go）——做对项，作为新增服务的超时/停机基线。

## 4. 故障演练（五个必做场景，全部可脚本化入 test-platform.sh）

| # | 演练 | 注入 | 期望行为（withstand） | 恢复验证（recover） | 当前状态 |
|---|---|---|---|---|---|
| D1 | **outage：governance 停机** | `systemctl stop audit-governance` | 业务写不阻塞（e2e 绿）；outbox 退避积压（2s→5min 上限 Verified）；**无告警**（C1 缺口） | 重启 governance → **需周期 drainer 或人工 flush**（H1）→ pending=0 且账本完整（幂等键去重） | 不满足（无 drainer 无告警） |
| D2 | **saturation：写放大与磁盘** | 批量创建质检单 + 观察 app.sqlite 增长 / governance state.json 字节/事件；`dd` 占满数据分区 | 现有状态：无背压、无告警、无容量包络（M3） | 磁盘满 → 备份/清理预案（4.3 + H3 清理 job 前置） | 缺口（M3） |
| D3 | **bad rollout：旧 dist 部署** | 部署 8/4 旧构建（DEPLOYMENT §6 真实事故复演） | preflight sha256 + git describe 校验**应拒绝**（devops H3，未实现） | 切 previous 回滚 → e2e 复验；**回滚本身触发密钥轮换窗口**（C3，1.2.1 前 ≤2min 预算） | 缺口（preflight 无、回滚手动） |
| D4 | **stale state：密钥轮换 + 吊销** | `systemctl restart snaplink-sverp`（1.2.1 前） | 客户端 401 强制重试 + 幂等安全网兜底（Verified）；2min timer 收敛 | timer 收敛后三套件全绿；**1.2.1 后**：kid 不变 + 吊销 token 重启后仍被拒（F2 断言） | 部分（timer 兜底在，语义缺陷 C3） |
| D5 | **restore：账本/会话恢复** | 备份 governance state.json → 删文件 → 从备份恢复；auth.sqlite 重建 | — | 查询/E2E 全绿；账本行数与字节一致（校验脚本）；用户重登（会话丢失可接受，文档化） | 缺口（零备份零演练，C2） |

## 5. 发布阻塞项、回滚触发条件、监控缺口、残余风险

### 5.1 发布阻塞项（与 devops B1–B8 对齐，SRE 视角）

| # | 阻塞项 | SRE 理由（本评审） | 出处 |
|---|---|---|---|
| B1 | 1.2.1 密钥轮换语义 + 吊销持久化（F2） | 回滚/重启=故障注入（C3）；吊销复活是新引入的可用性/安全退化 | C3 |
| B2 | 4.3 备份 + fsync + 恢复演练 | RPO 事实上 ∞；L2 的"审计不丢"无恢复面（C2） | C2 |
| B3 | C1 检测面（探针修正 + console/snaplink 探针 + 周期 drainer + 告警 sink） | 探针假阳性是错误自信的来源；审计静默中断不可接受 | C1/H1 |
| B4 | 1.2.2 配置 fail-closed + presign 钳制 + AUTH_PRESIGN_SECRET 固定 | 静默降级路径 + 在途 URL 失效（H4） | H4 |
| B5 | SLO/RTO/RPO 决策（H2） | 恢复目标不可度量，演练无验收标准 | H2 |

### 5.2 回滚触发条件（Proposed，与 devops 4.6 合并）

- 部署后 `test-platform.sh e2e` 红且 2 轮重试不过；
- outbox pending>0 超过 30min（H1 drainer 就位后可断言）；
- governance 完整性验证失败（SLO_ALERTS.md 的 P0 语义可借用：integrity invalid）；
- BFF 5xx 率 >1% 持续 5min（需 /metrics 或探针先行，C1）；
- console 探针失败（M2 就位后）。

### 5.3 监控缺口清单（现状 → 目标）

| 缺口 | 现状 | 最小目标（Proposed） |
|---|---|---|
| BFF 探针假阳性 | :13014/healthz 落 SPA fallback | /readyz（DB 可写 + pending 年龄）+ 脚本断言 JSON |
| BFF 指标 | 无 | /metrics 或 status 抓取：请求数/5xx/outbox pending/最大滞留 |
| snaplink 可观测性 | 无端点 | 真实协议探针（POST /token 期望 401）+ 端口双保险 |
| console | 无探针 | HTTP 200 + Restart=on-failure |
| governance | /metrics 现成未采集 | 巡检每轮抓取计数（错误率/冲突/完整性/延迟） |
| 告警 | 零 | aero-im 频道 sink（仅告警，kickoff 4.1#4） |
| drainer 指标 | 无周期机制 | pending=0 24h 观察（L2 指标，依赖 H1） |
| 备份新鲜度 | 无 | backup 子命令 + 上次成功时间入 summary |
| 容量 | 无 | 账本字节/事件、outbox 行数、媒体字节量包络（M3） |

### 5.4 残余风险（接受需显式声明）

1. **单主机 = 单点**：8 服务 + PG/Redis/NATS 全在一台 15Gi 机器；主机故障 = 全平台
   停机，DR = 从零重建（无版本化资产 M1 下不可复现）——HTTPS/JWKS 触发条件（多主机）
   与本项同源，触发时即需评估。
2. **LAN 信任模型**：HTTP 明文 + cookie 非 Secure（security F6）——接受即记录；
   "LAN 出现不受信主机"触发 TLS。
3. **minimal 二进制的默认值**（memory 后端/默认凭据，security F3）——部署覆盖正确但
   二进制未 fail-closed（DB H3）。
4. **容量与保留未知**：无负载证据、retention 未验证、outbox 无界（M3/H3）——L2 宣布
   前需发布包络与保留策略。
5. **恢复承诺的全部依赖未版本化脚本**（M1）——"可恢复"在机器重建场景不成立，直到
   ops 资产入库。

**裁决意见（Proposed）**：部署运行面（8 服务 + 优雅停机 + 有界超时 + 幂等安全网 + 逐事件
receipts + 有界退避重试）具备良好的 withstand 基础；**detect 层缺失（C1）与 recover 层
缺失（C2/H1）构成 L2 宣布的硬前置**——在 C1（探针修正 + drainer + 告警）+ B2（备份/恢复
演练）落地前，不建议宣布 L2。1.2.1 的验收必须并入吊销持久化断言（F2）；"drainer 60s"
的文档承诺需由代码实现（H1）或降级为人工 flush runbook。
